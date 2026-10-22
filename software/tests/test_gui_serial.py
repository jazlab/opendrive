"""Tk integration checks using simulated hardware and no saved-state writes."""
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from monkeydrive.config import Config
from monkeydrive.protocol import SerialProtocol as P
from monkeydrive_gui import MonkeyDriveApp
from test_serial_manager import FakePort


class GuiSerialTests(unittest.TestCase):
    def setUp(self):
        try:
            root = tk.Tk()
            root.update_idletasks()
            root.destroy()
        except tk.TclError as error:
            self.skipTest(f'Tk desktop unavailable: {error}')
        self.ports = []
        self.open_gate = None
        self.patches = [
            patch.object(MonkeyDriveApp, '_load_state', lambda app: None),
            patch.object(MonkeyDriveApp, '_save_state', lambda app: None),
            patch('monkeydrive.serial_manager.SerialManager.list_ports', return_value=[]),
            patch('monkeydrive.serial_manager.serial.Serial', side_effect=self.open_port),
            patch('monkeydrive_gui.messagebox.showwarning'),
        ]
        for p in self.patches:
            p.start()
        self.app = MonkeyDriveApp()
        self.callback_errors = []
        self.app.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.app.root.withdraw()
        self.app._port_var.set('fake')

    def open_port(self, **kwargs):
        if self.open_gate:
            self.open_gate.wait(2)
        port = FakePort(**kwargs)
        self.ports.append(port)
        return port

    def pump_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('GUI condition timed out')
            self.app.root.update()
            time.sleep(.003)

    def tearDown(self):
        if not hasattr(self, 'app'):
            return
        if self.open_gate:
            self.open_gate.set()
        for port in self.ports:
            port.release.set()
        self.app.serial_manager.disconnect()
        if self.app.serial_manager._session:
            self.app.serial_manager._session.thread.join(2)
        self.app._closing = True
        try:
            for job in self.app.root.tk.call('after', 'info'):
                self.app.root.after_cancel(job)
            self.app.root.destroy()
        except tk.TclError:
            pass
        for p in reversed(self.patches):
            p.stop()
        self.assertEqual(self.callback_errors, [])

    def test_console_modes_keep_controls_visible_and_preserve_drive_state(self):
        app = self.app
        for _ in range(5):
            app._add_motor()
        app.motor_states[0].absolute = 2.375
        app.motor_states[0].target = 4.125
        app._select_only_motor(2)
        app.root.deiconify()
        for mode in (Config.MODE_TARGET, Config.MODE_STEP, Config.MODE_PROGRAM, Config.MODE_TARGET):
            app._select_mode(mode)
            app.root.update()
            self.assertEqual(len(app._motor_widgets), 6)
            self.assertEqual(app.motor_states[0].absolute, 2.375)
            self.assertEqual(app._motor_widgets[0]['absolute_var'].get(), '2.375')
            self.assertTrue(app.motor_states[1].is_selected)
            self.assertTrue(app._selection_text.get().startswith('1 of 6'))
            frame = (app._step_controls_frame if mode == Config.MODE_STEP else
                     app._program_controls_frame if mode == Config.MODE_PROGRAM else app._target_controls_frame)
            self.assertTrue(frame.winfo_ismapped())
            bottom = app._motors_container.winfo_rooty() + app._motors_container.winfo_height()
            self.assertGreater(frame.winfo_rooty(), bottom)
            for row in app._motor_widgets:
                for widget in row['all_widgets']:
                    self.assertGreater(widget.winfo_width(), 1)
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                         app.root.winfo_rootx() + app.root.winfo_width())
        app._enable_absolute_edit(2)
        app._motor_widgets[1]['absolute_var'].set('3.250')
        app._on_absolute_confirm(2)
        self.assertEqual(app.motor_states[1].absolute, 3.25)
        self.assertEqual(app._motor_widgets[1]['absolute_entry'].cget('state'), 'readonly')

    def test_small_step_input_is_preserved_and_sent_to_controller(self):
        app = self.app
        app._select_mode(Config.MODE_STEP)
        app._on_connect()
        self.pump_until(lambda: app.serial_manager.is_connected)
        state = app.motor_states[0]
        state.absolute = 10.0
        state.is_selected = True
        for value in ('0.005', '0.001'):
            with self.subTest(value=value):
                app._enable_step_setting_edit('size')
                app._step_size_var.set(value)
                app._confirm_step_setting_edit('size')
                self.assertEqual(app._step_size_var.get(), value)
                app._on_step(direction=1)
                expected = P.cmd_run(1, float(app._step_speed_var.get()),
                                     10.0 + float(value), 10.0)
                self.pump_until(lambda: expected in self.ports[-1].writes)

    def test_step_size_minimum_is_independent_of_arrow_increment(self):
        app = self.app
        for value in ('0', '-0.005', '0.0001', 'nan', 'inf'):
            with self.subTest(value=value):
                app._enable_step_setting_edit('size')
                app._step_size_var.set(value)
                app._confirm_step_setting_edit('size')
                self.assertEqual(app._step_size_var.get(), '0.001')
        app._step_size_var.set('0.005')
        # Exercise the same adjustment handler without depending on desktop focus.
        app._adjust_numeric_entry(app._step_size_var, Config.STEP_SIZE_KEY_INCREMENT,
                                  3, min_value=Config.MIN_STEP_SIZE)
        self.assertEqual(app._step_size_var.get(), '0.015')

    def test_slow_connection_keeps_tk_responsive(self):
        self.open_gate = threading.Event()
        self.app._on_connect()
        self.assertEqual(self.app._connect_btn.cget('text'), 'Connecting...')
        ticks = []
        self.app.root.after(10, lambda: ticks.append(True))
        self.pump_until(lambda: ticks)
        self.assertFalse(self.app.serial_manager.is_connected)
        self.open_gate.set()
        self.pump_until(lambda: self.app._connect_btn.cget('text') == 'Disconnect')
        self.assertTrue(self.app.serial_manager.is_connected)

    def test_positions_and_disconnect_update_gui(self):
        self.app._on_connect()
        self.pump_until(lambda: self.app._connect_btn.cget('text') == 'Disconnect')
        port = self.ports[-1]
        port.inject(P.create_command('POS', 1, 2.5))
        self.pump_until(lambda: self.app.motor_states[0].absolute == 2.5)
        self.app._on_connect()
        self.pump_until(lambda: self.app._connect_btn.cget('text') == 'Connect')
        self.assertTrue(port.closed)

    def test_gui_freeze_pauses_program_and_does_not_resume(self):
        self.app._on_connect()
        self.pump_until(lambda: self.app._connect_btn.cget('text') == 'Disconnect')
        state = self.app.motor_states[0]
        state.is_running = True
        state.instruction_status = 'running'
        time.sleep(Config.GUI_WATCHDOG_TIMEOUT + .15)
        self.pump_until(lambda: state.instruction_status == 'paused')
        self.assertFalse(state.is_running)
        self.assertFalse(self.app.serial_manager.is_connected)
        self.assertTrue(self.ports[-1].closed)

    def test_close_waits_asynchronously_for_final_stop(self):
        self.app._on_connect()
        self.pump_until(lambda: self.app._connect_btn.cget('text') == 'Disconnect')
        port = self.ports[-1]
        port.block_command = 'RUN'
        self.app.serial_manager.send(P.cmd_run(1, .01, 1, 0))
        self.assertTrue(port.entered.wait(.1))
        start = time.monotonic()
        self.app._on_close()
        self.assertLess(time.monotonic() - start, .1)
        port.release.set()
        self.pump_until(lambda: not self.app.serial_manager.is_busy)
        self.assertTrue(port.closed)
        self.assertTrue(port.writes[-1].startswith('PAUSE_ALL'))


if __name__ == '__main__':
    unittest.main()

