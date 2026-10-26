"""Tk integration checks using simulated hardware and no saved-state writes."""
import gc
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from monkeydrive.config import Config
from monkeydrive.models import InstructionStep
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
        # Release destroyed Tk objects here, before a later viewer worker can
        # trigger cyclic garbage collection from a background thread.
        self.app = None
        gc.collect()

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

    def test_running_indicator_tracks_target_step_and_program_completion(self):
        app = self.app
        app._on_connect()
        self.pump_until(lambda: app.serial_manager.is_connected)
        state = app.motor_states[0]
        state.is_selected = True
        for mode in (Config.MODE_TARGET, Config.MODE_STEP, Config.MODE_PROGRAM):
            with self.subTest(mode=mode):
                state.absolute = 10.0
                state.target = 10.1
                state.speed = 0.01
                state.instructions = [InstructionStep(speed=0.01, target=10.1),
                                      InstructionStep(speed=0.01, target=10.2)]
                state.instruction_index = 0
                state.instruction_status = 'idle'
                app._select_mode(mode)
                if mode == Config.MODE_STEP:
                    app._step_size_var.set('0.100')
                    app._on_step(1)
                else:
                    app._on_run()
                app._refresh_running_indicators()
                self.assertEqual(app._motor_widgets[0]['motion_status'].cget('text'), 'Running')
                # Unchanged position during take-up must not clear the indicator.
                app._on_position_update(1, 10.0)
                app._refresh_running_indicators()
                self.assertEqual(app._motor_widgets[0]['motion_status'].cget('text'), 'Running')
                # Rebuilding the table retains the transient running destination.
                app._rebuild_motor_rows()
                app._refresh_running_indicators()
                self.assertEqual(app._motor_widgets[0]['motion_status'].cget('text'), 'Running')
                app._on_position_update(1, 10.1)
                if mode == Config.MODE_PROGRAM:
                    app._refresh_running_indicators()
                    self.assertEqual(app._motor_widgets[0]['motion_status'].cget('text'), 'Running')
                    app._on_position_update(1, 10.2)
                app._refresh_running_indicators()
                self.assertEqual(app._motor_widgets[0]['motion_status'].cget('text'), 'Idle')

    def test_running_indicator_clears_on_pause_disconnect_and_failed_send(self):
        app = self.app
        app._on_connect()
        self.pump_until(lambda: app.serial_manager.is_connected)
        state = app.motor_states[0]
        state.is_selected = True
        state.absolute, state.speed, state.target = 10.0, 0.01, 11.0
        app._rebuild_motor_rows()
        for stop in (app._on_pause, app.serial_manager.disconnect):
            app._on_run()
            app._refresh_running_indicators()
            self.assertEqual(app._motor_widgets[0]['motion_status'].cget('text'), 'Running')
            stop()
            app._refresh_running_indicators()
            self.assertEqual(app._motor_widgets[0]['motion_status'].cget('text'), 'Idle')
        state.is_running = app._queue_motion(state, P.cmd_run(1, .01, 11, 10))
        app._refresh_running_indicators()
        self.assertFalse(state.is_running)
        self.assertEqual(app._motor_widgets[0]['motion_status'].cget('text'), 'Idle')

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

    def test_explorer_assignment_updates_viewers_without_moving_drive(self):
        app = self.app
        state = app.motor_states[0]
        state.absolute, state.target = 10.0, 12.0
        state.trajectory.probe_location_set = True
        state.trajectory.probe_offset = 5.0
        state.trajectory.probe_motor_position = 10.0
        state.trajectory.target_location_set = True
        state.trajectory.target_offset = 3.0
        previous = state.trajectory.to_dict()
        detail, summary = Mock(), Mock()
        detail.poll_events.return_value = []
        summary.poll_events.return_value = []
        app._trajectory_windows[1] = detail
        app._trajectory_summary_window = summary
        geometry = dict(x=1.25, y=2.5, z=3.75, ap_rotation=-27.9, ml_rotation=28.4, twist_rotation=-1.3, zoom=1.5)
        with patch.object(app.serial_manager, 'send') as send:
            self.assertTrue(app._assign_explorer_trajectory(1, geometry))
            send.assert_not_called()
        self.assertEqual(state.trajectory.z, 3.75)
        self.assertEqual((state.absolute, state.target), (10.0, 12.0))
        for key, value in previous.items():
            if key not in geometry:
                self.assertEqual(state.trajectory.to_dict()[key], value)
        self.assertEqual(detail.send.call_args.args[0]['trajectory']['z'], 3.75)
        self.assertEqual(summary.send.call_args.args[0]['motor_state']['trajectory']['z'], 3.75)
        detail.poll_events.return_value = [{'type': 'save', 'trajectory': previous, 'revision': 0}]
        app._poll_viewer_processes()
        self.assertEqual(state.trajectory.z, 3.75)
        self.assertFalse(app._assign_explorer_trajectory(6, geometry))
        self.assertFalse(app._assign_explorer_trajectory(1, {**geometry, 'z': float('nan')}))

    def test_trajectories_button_opens_one_workspace_and_rows_have_no_trajectory_button(self):
        app = self.app
        self.assertNotIn('trajectory_button', app._motor_widgets[0])
        app._saved_anatomy_path = 'synthetic.nii'
        with patch('monkeydrive_gui.JsonViewerProcess') as process:
            app._on_trajectory_summary()
            app._on_trajectory_click(1)
            self.assertEqual(process.call_count, 1)
            self.assertEqual(process.call_args.args[0], 'monkeydrive.trajectories_process')
            self.assertIn('--explorer-json', process.call_args.args[1])
            process.return_value.send.assert_called_with({'type': 'select_drive', 'motor_id': 1})

    def test_row_zero_is_independent_of_selection_and_group_zero_only_affects_selected(self):
        app = self.app
        app._add_motor()
        first, second = app.motor_states[:2]
        for mode in (Config.MODE_TARGET, Config.MODE_STEP, Config.MODE_PROGRAM):
            app._select_mode(mode)
            first.absolute, first.relative, first.relative_zero_point = 5.25, 2.25, 3
            second.absolute, second.relative, second.relative_zero_point = -2.5, -1.5, -1
            app._select_only_motor(2)
            with patch.object(app.serial_manager, 'send') as send:
                app._motor_widgets[0]['zero_button'].invoke()
                self.assertEqual((first.absolute, first.relative, first.relative_zero_point), (5.25, 0, 5.25))
                self.assertEqual((second.relative, second.relative_zero_point), (-1.5, -1))
                self.assertFalse(first.is_selected)
                self.assertTrue(second.is_selected)
                self.assertEqual(app._motor_widgets[0]['relative_var'].get(), '0.000')
                first.relative, first.relative_zero_point = 1.25, 4
                controls = {Config.MODE_TARGET: app._target_controls_frame,
                            Config.MODE_STEP: app._step_controls_frame,
                            Config.MODE_PROGRAM: app._program_controls_frame}[mode]
                buttons = [button for group in controls.winfo_children() for button in group.winfo_children()]
                zero_selected = next(button for button in buttons if button.cget('text') == 'Zero Selected')
                zero_selected.invoke()
                self.assertEqual((first.relative, first.relative_zero_point), (1.25, 4))
                self.assertEqual((second.absolute, second.relative, second.relative_zero_point), (-2.5, 0, -2.5))
                send.assert_not_called()

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

