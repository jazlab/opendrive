"""Ordering, bounded work, and thread ownership for viewer background tasks."""
import queue
import threading
import time
import unittest

from opendrive.viewer_tasks import LatestWorker, ViewerCommands


class FakeWidget:
    def after(self, _delay, _callback):
        return 'job'

    def after_cancel(self, _job):
        pass


class ViewerTaskTests(unittest.TestCase):
    def test_position_burst_keeps_only_latest_per_motor(self):
        commands = ViewerCommands()
        for value in range(1000):
            for motor in range(1, 7):
                commands.put({'type': 'motor_position', 'motor_id': motor, 'position': value})
        for motor in range(1, 7):
            self.assertEqual(commands.get_nowait(), {'type': 'motor_position', 'motor_id': motor, 'position': 999})
        with self.assertRaises(queue.Empty):
            commands.get_nowait()

    def test_position_merges_into_pending_geometry_without_mutating_input(self):
        commands = ViewerCommands()
        state = {'motor_id': 1, 'absolute': 0, 'trajectory': {'x': 20}}
        commands.put({'type': 'motor_state', 'motor_state': state, 'revision': 3})
        commands.put({'type': 'motor_position', 'motor_id': 1, 'position': 5})
        message = commands.get_nowait()
        self.assertEqual(message['revision'], 3)
        result = message['motor_state']
        self.assertEqual(result['absolute'], 5)
        self.assertEqual(result['trajectory'], {'x': 20})
        self.assertEqual(state['absolute'], 0)
        commands.put({'type': 'motor_position', 'motor_id': 1, 'position': 1})
        commands.put({'type': 'motor_state', 'motor_state': result})
        self.assertEqual(commands.get_nowait()['motor_state']['absolute'], 5)

    def test_assignments_to_different_drive_tabs_are_not_coalesced_together(self):
        commands = ViewerCommands()
        for motor_id in (1, 2):
            commands.put({'type': 'trajectory_state', 'motor_id': motor_id, 'revision': 1})
        self.assertEqual(commands.get_nowait()['motor_id'], 1)
        self.assertEqual(commands.get_nowait()['motor_id'], 2)

    def test_busy_settings_do_not_starve_positions_and_close_discards_backlog(self):
        commands = ViewerCommands()
        commands.put({'type': 'motor_position', 'position': 3})
        for value in range(1000):
            commands.put({'type': 'target_offset', 'target_offset': value})
        self.assertEqual(commands.get_nowait()['type'], 'motor_position')
        commands.put({'type': 'close'})
        commands.put({'type': 'motor_position', 'position': 9})
        self.assertEqual(commands.get_nowait(), {'type': 'close'})
        with self.assertRaises(queue.Empty):
            commands.get_nowait()

    def test_obsolete_render_cannot_overwrite_latest_and_callbacks_run_on_caller(self):
        worker = LatestWorker(FakeWidget())
        entered, release = threading.Event(), threading.Event()
        results = []
        caller = threading.get_ident()
        def slow():
            entered.set()
            release.wait(2)
            return 'old'
        def complete(value, error):
            self.assertEqual(threading.get_ident(), caller)
            self.assertIsNone(error)
            results.append(value)
        try:
            worker.submit('plane', slow, complete)
            self.assertTrue(entered.wait(1))
            for value in range(1000):
                worker.submit('plane', lambda v=value: v, complete)
            self.assertEqual(len(worker._pending), 1)
            release.set()
            deadline = time.monotonic() + 2
            while not results and time.monotonic() < deadline:
                worker._poll()
                time.sleep(.002)
            self.assertEqual(results, [999])
        finally:
            release.set()
            worker.close()
            worker._thread.join(2)

    def test_close_during_render_drops_late_completion(self):
        worker = LatestWorker(FakeWidget())
        entered, release = threading.Event(), threading.Event()
        results = []
        def slow():
            entered.set()
            release.wait(2)
        worker.submit('plane', slow, lambda *args: results.append(args))
        self.assertTrue(entered.wait(1))
        worker.close()
        release.set()
        worker._thread.join(2)
        worker._poll()
        self.assertEqual(results, [])

    def test_progressive_worker_displays_completed_frame_before_newest_finishes(self):
        worker = LatestWorker(FakeWidget(), progressive=True)
        entered, release = threading.Event(), threading.Event()
        next_entered, next_release = threading.Event(), threading.Event()
        results = []
        def first():
            entered.set()
            release.wait(2)
            return 'first'
        def latest():
            next_entered.set()
            next_release.wait(2)
            return 'latest'
        complete = lambda value, error: results.append(value)
        try:
            worker.submit('plane', first, complete)
            self.assertTrue(entered.wait(1))
            for _ in range(100):
                worker.submit('plane', latest, complete)
            self.assertEqual(len(worker._pending), 1)
            release.set()
            self.assertTrue(next_entered.wait(1))
            worker._poll()
            self.assertEqual(results, ['first'])
            next_release.set()
            deadline = time.monotonic() + 2
            while len(results) < 2 and time.monotonic() < deadline:
                worker._poll()
                time.sleep(.002)
            self.assertEqual(results, ['first', 'latest'])
        finally:
            release.set()
            next_release.set()
            worker.close()
            worker._thread.join(2)

    def test_progressive_invalidation_discards_old_generation(self):
        worker = LatestWorker(FakeWidget(), progressive=True)
        entered, release = threading.Event(), threading.Event()
        results = []
        def first():
            entered.set()
            release.wait(2)
            return 'old'
        complete = lambda value, error: results.append(value)
        try:
            worker.submit('plane', first, complete)
            self.assertTrue(entered.wait(1))
            worker.invalidate()
            worker.submit('plane', lambda: 'new', complete)
            release.set()
            deadline = time.monotonic() + 2
            while not results and time.monotonic() < deadline:
                worker._poll()
                time.sleep(.002)
            self.assertEqual(results, ['new'])
        finally:
            release.set()
            worker.close()
            worker._thread.join(2)


if __name__ == '__main__':
    unittest.main()
