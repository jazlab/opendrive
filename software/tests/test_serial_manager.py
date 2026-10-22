"""Hardware-free fault injection for serial ownership and GUI liveness."""
import threading
import time
import unittest
from unittest.mock import patch

import serial

from monkeydrive.config import Config
from monkeydrive.protocol import SerialProtocol as P
from monkeydrive.serial_manager import SerialManager


class FakePort:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.owner = threading.get_ident()
        self.writes = []
        self.incoming = bytearray()
        self.lock = threading.Lock()
        self.closed = False
        self.ack = True
        self.block_command = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_write = False
        self.close_gate = None

    def check_owner(self):
        assert threading.get_ident() == self.owner

    def reset_input_buffer(self):
        self.check_owner()

    def reset_output_buffer(self):
        self.check_owner()

    @property
    def in_waiting(self):
        self.check_owner()
        with self.lock:
            return len(self.incoming)

    def inject(self, data):
        with self.lock:
            self.incoming.extend(data.encode())

    def read(self, count):
        self.check_owner()
        with self.lock:
            result = bytes(self.incoming[:count])
            del self.incoming[:count]
        return result

    def write(self, payload):
        self.check_owner()
        cmd = P.parse_response(payload.decode())[0]
        if cmd == self.block_command:
            self.entered.set()
            if not self.release.wait(self.kwargs['write_timeout']):
                raise serial.SerialTimeoutException('simulated stalled write')
        if self.fail_write:
            raise serial.SerialException('simulated unplug')
        self.writes.append(payload.decode())
        if cmd == 'HEARTBEAT' and self.ack:
            self.inject(P.create_command('HEARTBEAT_ACK'))
        return len(payload)

    def flush(self):
        raise AssertionError('flush must never be called')

    def close(self):
        self.check_owner()
        if self.close_gate:
            self.close_gate.wait(1)
        self.closed = True


class SerialReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.ports = []
        self.callbacks = []
        self.main_thread = threading.get_ident()
        self.open_gate = None
        self.open_started = threading.Event()
        self.patches = [
            patch('monkeydrive.serial_manager.serial.Serial', side_effect=self.open_port),
            patch.object(Config, 'HEARTBEAT_INTERVAL', .04),
            patch.object(Config, 'HEARTBEAT_TIMEOUT', .3),
            patch.object(Config, 'GUI_WATCHDOG_TIMEOUT', .2),
            patch.object(Config, 'SERIAL_WRITE_TIMEOUT', .15),
            patch.object(Config, 'SERIAL_READ_INTERVAL', .002),
        ]
        for p in self.patches:
            p.start()
        self.manager = SerialManager(
            lambda *args: self.record('position', *args),
            lambda: self.record('lost'), lambda: self.record('connected'))

    def record(self, *args):
        self.assertEqual(threading.get_ident(), self.main_thread)
        self.callbacks.append(args)

    def open_port(self, **kwargs):
        self.open_started.set()
        if self.open_gate:
            self.open_gate.wait(1)
        port = FakePort(**kwargs)
        self.ports.append(port)
        return port

    def tearDown(self):
        if self.open_gate:
            self.open_gate.set()
        for port in self.ports:
            port.release.set()
            if port.close_gate:
                port.close_gate.set()
        self.manager.disconnect()
        if self.manager._session:
            self.manager._session.thread.join(2)
        for p in reversed(self.patches):
            p.stop()

    def wait_for(self, predicate, poll=True, timeout=1):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('Timed out waiting for condition')
            if poll:
                self.manager.poll_events()
            time.sleep(.002)
        if poll:
            self.manager.poll_events()

    def connect(self):
        self.assertTrue(self.manager.connect('fake'))
        self.wait_for(lambda: self.manager.is_connected)
        return self.ports[-1]

    def test_open_and_disconnect_never_wait_on_driver(self):
        self.open_gate = threading.Event()
        start = time.monotonic()
        self.assertTrue(self.manager.connect('fake'))
        self.assertLess(time.monotonic() - start, .1)
        self.assertTrue(self.open_started.wait(.5))
        start = time.monotonic()
        self.manager.disconnect()
        self.assertLess(time.monotonic() - start, .1)
        self.assertFalse(self.manager.connect('fake'))
        self.open_gate.set()
        self.wait_for(lambda: not self.manager.is_busy)
        self.assertFalse(self.manager.is_connected)
        self.assertNotIn(('connected',), self.callbacks)
        self.assertTrue(self.ports[-1].closed)

    def test_stalled_write_does_not_block_gui_and_fails_once(self):
        port = self.connect()
        port.block_command = 'RUN'
        start = time.monotonic()
        self.assertTrue(self.manager.send(P.cmd_run(1, .01, 1, 0)))
        self.assertLess(time.monotonic() - start, .1)
        self.assertTrue(port.entered.wait(.1))
        self.wait_for(lambda: not self.manager.is_busy)
        self.assertEqual(self.callbacks.count(('lost',)), 1)
        self.assertFalse(self.manager.is_connected)
        self.assertTrue(port.closed)

    def test_pause_discards_queued_motion_and_has_priority(self):
        port = self.connect()
        port.block_command = 'CONFIG'
        self.manager.send(P.cmd_config(.001, .2, .058))
        self.assertTrue(port.entered.wait(.1))
        self.manager.send(P.cmd_run(1, .01, 1, 0))
        self.manager.send(P.cmd_update_target(1, 2))
        self.manager.send(P.cmd_run(2, .01, 1, 0))
        self.manager.send(P.cmd_pause(1))
        port.release.set()
        self.wait_for(lambda: any(x.startswith('RUN,2,') for x in port.writes))
        self.assertFalse(any(x.startswith(('RUN,1,', 'TARGET,1,')) for x in port.writes))
        pause_index = next(i for i, x in enumerate(port.writes) if x.startswith('PAUSE,1*'))
        run_index = next(i for i, x in enumerate(port.writes) if x.startswith('RUN,2,'))
        self.assertLess(pause_index, run_index)

    def test_pause_all_discards_all_pending_motion(self):
        port = self.connect()
        port.block_command = 'CONFIG'
        self.manager.send(P.cmd_config(.001, .2, .058))
        self.assertTrue(port.entered.wait(.1))
        for motor in range(1, 7):
            self.manager.send(P.cmd_run(motor, .01, 1, 0))
        self.manager.send(P.cmd_pause_all())
        port.release.set()
        self.wait_for(lambda: sum(x.startswith('PAUSE_ALL') for x in port.writes) >= 2)
        self.assertFalse(any(x.startswith('RUN') for x in port.writes))

    def test_frozen_gui_stops_heartbeats_and_requires_reconnect(self):
        port = self.connect()
        self.wait_for(lambda: not self.manager.is_busy, poll=False)
        self.assertIn('window', self.manager.failure_reason)
        self.assertTrue(port.closed)
        writes = len(port.writes)
        self.manager.poll_events()
        self.assertEqual(self.callbacks.count(('lost',)), 1)
        self.assertFalse(self.manager.send(P.cmd_run(1, .01, 1, 0)))
        self.assertEqual(len(port.writes), writes)
        self.assertTrue(port.writes[-1].startswith('PAUSE_ALL'))

    def test_missing_controller_ack_stops_even_when_gui_is_live(self):
        port = self.connect()
        port.ack = False
        self.wait_for(lambda: not self.manager.is_busy)
        self.assertIn('heartbeat', self.manager.failure_reason)
        self.assertEqual(self.callbacks.count(('lost',)), 1)

    def test_reconnect_waits_for_old_worker_and_drops_old_events(self):
        port = self.connect()
        port.close_gate = threading.Event()
        self.manager.disconnect()
        self.assertFalse(self.manager.connect('fake'))
        port.close_gate.set()
        self.wait_for(lambda: not self.manager.is_busy)
        self.callbacks.clear()
        new_port = self.connect()
        self.assertIsNot(new_port, port)
        self.assertEqual(self.callbacks, [('connected',)])
        old_count = len(port.writes)
        self.manager.send(P.cmd_run(1, .01, 1, 0))
        self.wait_for(lambda: any(x.startswith('RUN') for x in new_port.writes))
        self.assertEqual(len(port.writes), old_count)

    def test_callbacks_only_run_during_gui_poll_and_positions_coalesce(self):
        port = self.connect()
        self.callbacks.clear()
        for value in range(100):
            port.inject(P.create_command('POS', 1, value / 10))
        self.wait_for(lambda: port.incoming == b'', poll=False)
        self.assertEqual(self.callbacks, [])
        self.manager.poll_events()
        self.assertEqual(self.callbacks, [('position', 1, 9.9)])

    def test_invalid_positions_are_ignored(self):
        port = self.connect()
        self.callbacks.clear()
        for motor, value in [(0, 1), (7, 1), (1, 'nan'), (1, 'inf'), (1, -1), (1, 51)]:
            port.inject(P.create_command('POS', motor, value))
        self.wait_for(lambda: port.incoming == b'')
        self.assertEqual(self.callbacks, [])

    def test_unplug_reports_failure_once(self):
        port = self.connect()
        port.fail_write = True
        self.manager.send(P.cmd_run(1, .01, 1, 0))
        self.wait_for(lambda: not self.manager.is_busy)
        self.assertEqual(self.callbacks.count(('lost',)), 1)
        self.assertTrue(port.closed)

    def test_queue_overflow_fails_closed(self):
        port = self.connect()
        port.block_command = 'CONFIG'
        self.manager.send(P.cmd_config(.001, .2, .058))
        self.assertTrue(port.entered.wait(.1))
        with patch.object(Config, 'SERIAL_COMMAND_QUEUE_LIMIT', 2):
            self.assertTrue(self.manager.send(P.cmd_run(1, .01, 1, 0)))
            self.assertTrue(self.manager.send(P.cmd_run(2, .01, 1, 0)))
            self.assertFalse(self.manager.send(P.cmd_run(3, .01, 1, 0)))
        port.release.set()
        self.wait_for(lambda: not self.manager.is_busy)
        self.assertFalse(any(x.startswith('RUN') for x in port.writes))
        self.assertIn('overflow', self.manager.failure_reason)

    def test_poll_from_worker_is_rejected(self):
        errors = []
        def worker():
            try:
                self.manager.poll_events()
            except RuntimeError as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)


if __name__ == '__main__':
    unittest.main()
