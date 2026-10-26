"""Tk integration checks using simulated hardware and no saved-state writes."""
import gc
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from monkeydrive.config import Config
from monkeydrive.configuration import read_config, validate_workspace, write_config
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
        if self.app._config_worker:
            self.app._config_worker.close()
            self.app._config_worker._thread.join(2)
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
            self.assertEqual(app._stop_all_btn.cget('text'), 'Pause All')
            self.assertGreater(app._stop_all_btn.winfo_rooty(), frame.winfo_rooty() + frame.winfo_height())
            self.assertAlmostEqual(app._stop_all_btn.winfo_rootx() + app._stop_all_btn.winfo_width() / 2,
                                   app.root.winfo_rootx() + app.root.winfo_width() / 2, delta=1)
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

    def test_config_save_load_restores_workspace_and_existing_autosave(self):
        app = self.app
        app._add_motor()
        app.motor_states[0].absolute = 6.125
        app.motor_states[0].relative = -.5
        app.motor_states[0].relative_zero_point = 6.625
        app.motor_states[0].instructions = [InstructionStep(9, .01, 'planned')]
        app.motor_states[0].trajectory.z = 3.5
        app.motor_states[1].notes = 'Drive two'
        app._explorer_state = dict(x=2, y=3, z=4, ap_rotation=12)
        app._step_size_var.set('.001')
        app._select_mode(Config.MODE_PROGRAM)
        app._saved_anatomy_path = '/missing/subject.nii'
        app._on_notes_click(1)
        app._notes_windows[1]._text.insert('1.0', 'Unsaved note text')
        with tempfile.TemporaryDirectory() as folder, patch('monkeydrive_gui.messagebox.showinfo'), \
                patch('monkeydrive_gui.messagebox.showerror') as error:
            path = Path(folder) / 'config.json'
            with patch('monkeydrive_gui.filedialog.asksaveasfilename', return_value=str(path)):
                app._on_save_config()
                self.pump_until(lambda: not app._config_busy)
            saved = read_config(path)
            self.assertEqual(saved['motor_states'][0]['notes'], 'Unsaved note text')
            app._motor_states[0].absolute = 20
            app._motor_states[0].trajectory.z = 90
            app._select_mode(Config.MODE_TARGET)
            with patch('monkeydrive_gui.filedialog.askopenfilename', return_value=str(path)):
                app._on_load_config()
                self.pump_until(lambda: not app._config_busy)
            self.assertEqual(app._workspace_snapshot(), {**saved, 'step_speed': '0.01', 'step_size': '0.001'})
            self.assertEqual(app._motor_widgets[0]['absolute_var'].get(), '6.125')
            self.assertEqual(app._mode_var.get(), Config.MODE_PROGRAM)
            self.assertEqual(len(app._motor_widgets), 2)
            self.assertEqual(app._notes_windows, {})
            error.assert_not_called()
            # Loading also becomes the next automatic startup state.
            auto_path = Path(folder) / 'automatic.json'
            with patch.object(app, '_get_data_path', return_value=auto_path):
                self.patches[1].temp_original(app)
                app._motor_states.clear()
                self.patches[0].temp_original(app)
            self.assertEqual(app.motor_states[0].absolute, 6.125)
            self.assertEqual(app.motor_states[0].trajectory.z, 3.5)
            self.assertEqual(app.motor_states[0].notes, 'Unsaved note text')

    def test_config_load_disconnects_before_restoring_positions_and_never_runs(self):
        app = self.app
        app._on_connect()
        self.pump_until(lambda: app.serial_manager.is_connected)
        port = self.ports[-1]
        workspace = app._workspace_snapshot()
        workspace['motor_states'][0].update(absolute=7.25, relative=7.25, is_running=True)
        with tempfile.TemporaryDirectory() as folder, patch('monkeydrive_gui.messagebox.showinfo'), \
                patch('monkeydrive_gui.messagebox.showerror') as error:
            path = Path(folder) / 'config.json'
            write_config(path, workspace)
            original_apply = app._apply_workspace
            def apply(data):
                self.assertFalse(app.serial_manager.is_connected)
                self.assertFalse(app.serial_manager.is_busy)
                original_apply(data)
            with patch.object(app, '_apply_workspace', side_effect=apply), \
                    patch('monkeydrive_gui.filedialog.askopenfilename', return_value=str(path)):
                app._on_load_config()
                self.pump_until(lambda: not app._config_busy)
            error.assert_not_called()
        self.assertTrue(port.closed)
        self.assertEqual(app.motor_states[0].absolute, 7.25)
        self.assertFalse(app.motor_states[0].is_running)
        self.assertFalse(any(P.parse_response(command)[0] in ('RUN', 'TARGET') for command in port.writes))
        self.assertEqual(app._motion_targets, {})
        self.assertEqual(app._connect_btn.cget('text'), 'Connect')

    def test_invalid_config_leaves_current_workspace_intact(self):
        app = self.app
        before = app._workspace_snapshot()
        with tempfile.TemporaryDirectory() as folder, patch('monkeydrive_gui.messagebox.showerror') as error:
            path = Path(folder) / 'invalid.json'
            path.write_text('{"format":"monkeydrive-config","version":99}', encoding='utf-8')
            with patch('monkeydrive_gui.filedialog.askopenfilename', return_value=str(path)), \
                    patch.object(app.serial_manager, 'disconnect') as disconnect:
                app._on_load_config()
                self.pump_until(lambda: not app._config_busy)
                disconnect.assert_not_called()
            error.assert_called_once()
        self.assertEqual(app._workspace_snapshot(), before)
        self.assertFalse(app._config_loading)

    def test_config_load_reopens_viewers_without_old_workspace_events(self):
        app = self.app
        anatomy, trajectories = Mock(), Mock()
        anatomy.is_running.return_value = trajectories.is_running.return_value = True
        anatomy.poll_events.return_value = trajectories.poll_events.return_value = []
        app._anatomy_window, app._trajectory_summary_window = anatomy, trajectories
        with tempfile.TemporaryDirectory() as folder, patch('monkeydrive_gui.messagebox.showinfo'):
            mri = Path(folder) / 'subject.nii'
            mri.touch()
            workspace = app._workspace_snapshot()
            workspace['anatomy_path'] = str(mri)
            workspace['motor_states'][0]['trajectory']['x'] = 22
            workspace['explorer_state'] = dict(x=2, y=3, z=4)
            path = Path(folder) / 'config.json'
            write_config(path, workspace)
            with patch('monkeydrive_gui.filedialog.askopenfilename', return_value=str(path)), \
                    patch('monkeydrive_gui.JsonViewerProcess') as process:
                process.return_value.poll_events.return_value = []
                app._on_load_config()
                self.pump_until(lambda: not app._config_busy)
                self.assertEqual(process.call_count, 2)
                launches = {call.args[0]: call.args[1] for call in process.call_args_list}
                self.assertIn(str(mri), launches['monkeydrive.anatomy_process'])
                self.assertIn(str(mri), launches['monkeydrive.trajectories_process'])
                self.assertIn('--explorer-json', launches['monkeydrive.trajectories_process'])
                trajectories.poll_events.reset_mock()
                app._poll_viewer_processes()
            anatomy.close.assert_called_once()
            trajectories.close.assert_called_once()
            trajectories.poll_events.assert_not_called()
            app._anatomy_window = app._trajectory_summary_window = None
        self.assertEqual(app.motor_states[0].trajectory.x, 22)

    def test_failed_config_save_preserves_existing_file_and_reports_error(self):
        app = self.app
        with tempfile.TemporaryDirectory() as folder, patch('monkeydrive_gui.messagebox.showerror') as error:
            path = Path(folder) / 'config.json'
            path.write_text('original', encoding='utf-8')
            with patch('monkeydrive_gui.filedialog.asksaveasfilename', return_value=str(path)), \
                    patch('monkeydrive_gui.write_config', side_effect=OSError('Disk full')):
                app._on_save_config()
                self.pump_until(lambda: not app._config_busy)
            error.assert_called_once()
            self.assertEqual(path.read_text(), 'original')

    def test_cancelled_config_dialogs_leave_workspace_intact(self):
        app = self.app
        before = app._workspace_snapshot()
        with patch('monkeydrive_gui.filedialog.askopenfilename', return_value=''), \
                patch('monkeydrive_gui.filedialog.asksaveasfilename', return_value=''):
            app._on_save_config()
            app._on_load_config()
        self.assertEqual(app._workspace_snapshot(), before)
        self.assertFalse(app._config_busy)
        self.assertIsNone(app._config_worker)

    def test_slow_config_read_keeps_tk_responsive(self):
        app = self.app
        release = threading.Event()
        workspace = app._workspace_snapshot()
        def read(_path):
            release.wait(2)
            return validate_workspace(workspace)
        ticks = []
        with patch('monkeydrive_gui.read_config', side_effect=read), \
                patch('monkeydrive_gui.filedialog.askopenfilename', return_value='config.json'), \
                patch('monkeydrive_gui.messagebox.showinfo'):
            try:
                app._on_load_config()
                app.root.after(10, lambda: ticks.append(True))
                self.pump_until(lambda: bool(ticks))
                self.assertTrue(app._config_busy)
                app._on_connect()
                self.assertFalse(app.serial_manager.is_busy)
            finally:
                release.set()
            self.pump_until(lambda: not app._config_busy)

    @contextmanager
    def jog_session(self):
        app = self.app
        app._on_keyrelease(SimpleNamespace(keysym='plus', widget=app.root))
        self.pump_until(lambda: not app._blocked_step_keys)
        app._add_motor()
        app._select_mode(Config.MODE_STEP)
        app._select_only_motor(1)
        with patch.object(type(app.serial_manager), 'is_connected', new_callable=PropertyMock, return_value=True), \
                patch.object(app.serial_manager, 'send', return_value=True) as send:
            app._handle_step_keypress('down', 1)
            app._cancel_step_key_hold_job()
            app._start_step_jog()
            self.assertTrue(app._step_key_is_jogging)
            send.reset_mock()
            yield send

    def paused_ids(self, send):
        commands = [P.parse_response(call.args[0]) for call in send.call_args_list]
        return {int(args[0]) for name, args in commands if name == 'PAUSE'}

    def test_jog_release_stops_original_owner_even_if_selection_and_flags_changed(self):
        app = self.app
        with self.jog_session() as send:
            app.motor_states[0].is_selected = False
            app.motor_states[0].is_running = False
            app.motor_states[1].is_selected = True
            app._finish_step_keyrelease()
            self.assertEqual(self.paused_ids(send), {1})
            self.assertEqual(app._step_key_motor_ids, ())
            self.assertNotIn(1, app._motion_targets)

    def test_digit_selection_during_jog_stops_old_drive_and_cancels_gesture(self):
        app = self.app
        with self.jog_session() as send:
            app._on_keypress(SimpleNamespace(keysym='2', widget=app.root))
            self.assertEqual(self.paused_ids(send), {1})
            self.assertTrue(app.motor_states[1].is_selected)
            self.assertIsNone(app._active_step_key)
            send.reset_mock()
            app._start_step_jog()
            send.assert_not_called()

    def test_checkbox_selection_change_stops_jog_owner(self):
        app = self.app
        with self.jog_session() as send:
            app._motor_widgets[1]['selected_var'].set(True)
            app._on_selection_change(2)
            self.assertEqual(self.paused_ids(send), {1})
            self.assertFalse(app._step_key_is_jogging)

    def test_jog_stops_on_focus_loss_text_focus_minimize_and_mode_change(self):
        app = self.app
        for trigger in ('focus_loss', 'text', 'minimize', 'mode'):
            with self.subTest(trigger=trigger), self.jog_session() as send:
                if trigger == 'focus_loss':
                    with patch.object(app.root, 'focus_get', return_value=None):
                        app._check_jog_focus()
                elif trigger == 'text':
                    with patch.object(app.root, 'focus_get', return_value=app._step_size_entry), \
                            patch.object(app._step_size_entry, 'cget', return_value='normal'):
                        app._check_jog_focus()
                elif trigger == 'minimize':
                    app._on_jog_unmap(SimpleNamespace(widget=app.root))
                else:
                    app._select_mode(Config.MODE_TARGET)
                self.assertEqual(self.paused_ids(send), {1})
                self.assertIsNone(app._active_step_key)
                self.assertFalse(app._step_key_is_jogging)

    def test_release_received_in_another_window_is_not_ignored(self):
        app = self.app
        with self.jog_session() as send:
            other = tk.Toplevel(app.root)
            entry = tk.Entry(other)
            app._on_keyrelease(SimpleNamespace(keysym='plus', widget=entry))
            self.pump_until(lambda: app._active_step_key is None)
            self.assertEqual(self.paused_ids(send), {1})
            other.destroy()

    def test_focus_loss_before_hold_threshold_cancels_pending_jog(self):
        app = self.app
        app._select_mode(Config.MODE_STEP)
        app._select_only_motor(1)
        with patch.object(type(app.serial_manager), 'is_connected', new_callable=PropertyMock, return_value=True), \
                patch.object(app.serial_manager, 'send', return_value=True) as send:
            app._handle_step_keypress('down', 1)
            with patch.object(app.root, 'focus_get', return_value=None):
                app._check_jog_focus()
            self.assertIsNone(app._step_key_hold_job)
            send.reset_mock()
            app._start_step_jog()
            send.assert_not_called()

    def test_root_focus_out_binding_stops_jog(self):
        app = self.app
        with self.jog_session() as send, patch.object(app.root, 'focus_get', return_value=None):
            app.root.event_generate('<FocusOut>')
            self.pump_until(lambda: app._active_step_key is None)
            self.assertEqual(self.paused_ids(send), {1})

    def test_release_near_hold_threshold_does_not_start_continuous_motion(self):
        app = self.app
        app._select_mode(Config.MODE_STEP)
        app._select_only_motor(1)
        with patch.object(type(app.serial_manager), 'is_connected', new_callable=PropertyMock, return_value=True), \
                patch.object(app.serial_manager, 'send', return_value=True) as send:
            app._handle_step_keypress('down', 1)
            app._cancel_step_key_hold_job()
            app._handle_step_keyrelease('down')
            send.reset_mock()
            app._start_step_jog()
            self.pump_until(lambda: app._active_step_key is None)
            self.assertFalse(app._step_key_is_jogging)
            self.assertIsNone(app._step_key_hold_job)
            send.assert_not_called()

    def test_stop_all_blocks_held_key_repeat_until_a_real_release(self):
        app = self.app
        with self.jog_session() as send:
            app._on_stop_all()
            send.reset_mock()
            # Includes the release/press pairs generated by some Tk builds.
            for _ in range(3):
                app._on_keyrelease(SimpleNamespace(keysym='plus', widget=app.root))
                app._handle_step_keypress('down', 1)
            send.assert_not_called()
            self.assertIsNone(app._active_step_key)
            app._on_keyrelease(SimpleNamespace(keysym='plus', widget=app.root))
            self.pump_until(lambda: not app._blocked_step_keys)
            app._handle_step_keypress('down', 1)
            self.assertEqual(P.parse_response(send.call_args.args[0])[0], 'RUN')
            app._reset_step_key_state()

    def test_stop_all_ignores_selection_running_flags_and_clears_motion(self):
        app = self.app
        app._add_motor()
        app._remove_motor()
        app._select_only_motor(1)
        app.motor_states[0].is_running = False
        app._hidden_motor_states[2].instruction_status = 'running'
        app._step_targets[2] = 40
        app._motion_targets[1] = 30
        with patch.object(app.serial_manager, 'send', return_value=True) as send:
            for mode in (Config.MODE_TARGET, Config.MODE_STEP, Config.MODE_PROGRAM):
                app._select_mode(mode)
                send.reset_mock()
                app._stop_all_btn.invoke()
                self.assertEqual(P.parse_response(send.call_args_list[0].args[0])[0], 'PAUSE_ALL')
        self.assertEqual(app._step_targets, {})
        self.assertEqual(app._motion_targets, {})
        self.assertEqual(app._hidden_motor_states[2].instruction_status, 'paused')

    def test_hidden_drive_preserves_state_receives_final_position_and_survives_restart(self):
        app = self.app
        app._add_motor()
        state = app.motor_states[1]
        state.absolute, state.relative, state.relative_zero_point = 20, 2, 18
        state.notes = 'Preserve this calibration'
        state.instructions = [InstructionStep(22, .01, 'planned')]
        state.trajectory.z = -3
        state.is_running = True
        with patch.object(app.serial_manager, 'send', return_value=True) as send:
            app._remove_motor()
            self.assertEqual(self.paused_ids(send), {2})
        app._on_position_update(2, 20.125)
        self.assertEqual(app._hidden_motor_states[2].relative, 2.125)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'automatic.json'
            with patch.object(app, '_get_data_path', return_value=path):
                self.patches[1].temp_original(app)
                app._hidden_motor_states.clear()
                self.patches[0].temp_original(app)
            self.assertEqual(len(app.motor_states), 1)
            config_path = Path(folder) / 'named.json'
            write_config(config_path, app._workspace_snapshot())
            restored = read_config(config_path)
            app._hidden_motor_states.clear()
            app._apply_workspace(restored)
        app._add_motor()
        state = app.motor_states[1]
        self.assertEqual((state.absolute, state.relative, state.relative_zero_point), (20.125, 2.125, 18))
        self.assertEqual(state.notes, 'Preserve this calibration')
        self.assertEqual(state.instructions[0].target, 22)
        self.assertEqual(state.trajectory.z, -3)
        self.assertFalse(state.is_selected)
        self.assertFalse(state.is_running)

    def test_plain_enter_never_starts_motion_even_on_focused_buttons(self):
        app = self.app
        app.root.deiconify()
        app._select_only_motor(1)
        app.motor_states[0].speed = .01
        app.motor_states[0].target = 5
        with patch.object(type(app.serial_manager), 'is_connected', new_callable=PropertyMock, return_value=True), \
                patch.object(app.serial_manager, 'send', return_value=True) as send:
            for mode in (Config.MODE_TARGET, Config.MODE_STEP, Config.MODE_PROGRAM):
                app._select_mode(mode)
                frame = {Config.MODE_TARGET: app._target_controls_frame,
                         Config.MODE_STEP: app._step_controls_frame,
                         Config.MODE_PROGRAM: app._program_controls_frame}[mode]
                buttons = [button for group in frame.winfo_children() for button in group.winfo_children()
                           if button.cget('text') in ('Run Selected', 'Step Up', 'Step Down')]
                for widget in [app.root, app._motor_widgets[0]['absolute_entry'], *buttons]:
                    widget.focus_force()
                    app.root.update()
                    for key in ('Return', 'KP_Enter'):
                        native_key = 'Return' if app.root.tk.call('tk', 'windowingsystem') == 'win32' else key
                        required = 0x8 if app.root.tk.call('tk', 'windowingsystem') == 'aqua' else 0x4
                        for state in (0, 1, 2, 8, 16, 128):
                            if state & required:
                                continue
                            widget.event_generate(f'<KeyPress-{native_key}>', state=state)
                            widget.event_generate(f'<KeyRelease-{native_key}>', state=state)
                            app.root.update()
            send.assert_not_called()
            app._enable_absolute_edit(1)
            entry = app._motor_widgets[0]['absolute_entry']
            app._motor_widgets[0]['absolute_var'].set('3.125')
            entry.focus_force()
            entry.event_generate('<Return>')
            app.root.update()
            self.assertEqual(app.motor_states[0].absolute, 3.125)
            self.assertEqual(entry.cget('state'), 'readonly')
            send.assert_not_called()

    def test_modified_enter_runs_selected_in_target_and_program_but_not_step_or_popup(self):
        app = self.app
        app.root.deiconify()
        with patch.object(app, '_on_run') as run:
            for mode in (Config.MODE_TARGET, Config.MODE_PROGRAM, Config.MODE_STEP):
                app._select_mode(mode)
                frame = {Config.MODE_TARGET: app._target_controls_frame,
                         Config.MODE_STEP: app._step_controls_frame,
                         Config.MODE_PROGRAM: app._program_controls_frame}[mode]
                buttons = [button for group in frame.winfo_children() for button in group.winfo_children()
                           if button.cget('text') in ('Run Selected', 'Step Up', 'Step Down')]
                for widget in [app.root, app._motor_widgets[0]['absolute_entry'], *buttons]:
                    widget.focus_force()
                    app.root.update()
                    modifier = 'Command' if app.root.tk.call('tk', 'windowingsystem') == 'aqua' else 'Control'
                    for key in (f'{modifier}-Return', f'{modifier}-KP_Enter'):
                        run.reset_mock()
                        # Windows reports both physical Enter keys as Return;
                        # generating KP_Enter there produces an unmapped keycode.
                        native_key = key.replace('KP_Enter', 'Return') if app.root.tk.call('tk', 'windowingsystem') == 'win32' else key
                        widget.event_generate(f'<{native_key}>')
                        widget.event_generate(f'<{native_key.replace("-", "-KeyRelease-", 1)}>')
                        app.root.update()
                        if mode == Config.MODE_STEP:
                            run.assert_not_called()
                        else:
                            self.assertEqual(run.call_count, 1, (mode, str(widget), key))
            app._select_mode(Config.MODE_TARGET)
            popup = tk.Toplevel(app.root)
            entry = tk.Entry(popup)
            entry.pack()
            entry.focus_force()
            app.root.update()
            run.reset_mock()
            entry.event_generate('<Control-Return>')
            app.root.update()
            run.assert_not_called()
            popup.destroy()

    def test_run_shortcut_requires_actual_native_modifier_even_if_dispatched(self):
        app = self.app
        required = 0x8 if app.root.tk.call('tk', 'windowingsystem') == 'aqua' else 0x4
        with patch.object(app, '_on_run') as run:
            for state in (0, 1, 2, 8, 16, 128):
                if not state & required:
                    app._on_run_shortcut(SimpleNamespace(state=state))
            run.assert_not_called()
            app._on_run_shortcut(SimpleNamespace(state=required))
            run.assert_called_once_with()

    def test_compact_view_shrinks_six_drive_console_and_restores_full_size(self):
        app = self.app
        for _ in range(5):
            app._add_motor()
        app.root.deiconify()
        app.root.update()
        full_width = app.root.winfo_width()
        for _ in range(2):
            app._set_compact_view(True)
            app.root.update()
            self.assertTrue(app._compact_frame.winfo_ismapped())
            self.assertFalse(app._full_console_frame.winfo_ismapped())
            self.assertEqual(len(app._compact_widgets), 6)
            self.assertLessEqual(app.root.winfo_width(), 400)
            self.assertLessEqual(app.root.winfo_height(), 400)
            self.assertTrue(app._compact_pause_all_btn.winfo_ismapped())
            app._set_compact_view(False)
            app.root.update()
            self.assertTrue(app._full_console_frame.winfo_ismapped())
            self.assertFalse(app._compact_frame.winfo_ismapped())
            self.assertGreaterEqual(app.root.winfo_width(), full_width)

    def test_compact_readouts_share_live_position_variables_and_pause_all(self):
        app = self.app
        app._add_motor()
        app._set_compact_view(True)
        app.motor_states[1].relative_zero_point = 3
        app._on_position_update(2, 5.125)
        readouts = app._compact_widgets[2]
        self.assertEqual(app.root.getvar(readouts['absolute'].cget('textvariable')), '5.125')
        self.assertEqual(app.root.getvar(readouts['relative'].cget('textvariable')), '2.125')
        with patch.object(app.serial_manager, 'send', return_value=True) as send:
            app._compact_pause_all_btn.invoke()
            self.assertEqual(P.parse_response(send.call_args.args[0])[0], 'PAUSE_ALL')
            send.reset_mock()
            app._on_pause_shortcut(SimpleNamespace())
            self.assertEqual(P.parse_response(send.call_args.args[0])[0], 'PAUSE_ALL')

    def test_compact_view_blocks_run_and_jog_shortcuts_and_cancels_current_jog(self):
        app = self.app
        with self.jog_session() as send:
            app._set_compact_view(True)
            self.assertEqual(self.paused_ids(send), {1})
            self.assertIsNone(app._active_step_key)
            send.reset_mock()
            app._on_keypress(SimpleNamespace(keysym='plus', widget=app.root))
            send.assert_not_called()
        required = 8 if app.root.tk.call('tk', 'windowingsystem') == 'aqua' else 4
        with patch.object(app, '_on_run') as run:
            app._select_mode(Config.MODE_TARGET)
            app._on_run_shortcut(SimpleNamespace(state=required))
            run.assert_not_called()
        self.assertTrue(app._compact_active)

    def test_compact_view_preserves_running_program_and_returns_to_its_mode(self):
        app = self.app
        app._select_mode(Config.MODE_PROGRAM)
        state = app.motor_states[0]
        state.instructions = [InstructionStep(1, .01), InstructionStep(2, .01)]
        state.is_running = True
        state.instruction_status = 'running'
        with patch.object(type(app.serial_manager), 'is_connected', new_callable=PropertyMock, return_value=True), \
                patch.object(app.serial_manager, 'send', return_value=True) as send:
            app._set_compact_view(True)
            self.assertTrue(state.is_running)
            app._on_position_update(1, 1)
            self.assertEqual(state.instruction_index, 1)
            self.assertEqual(P.parse_response(send.call_args.args[0])[0], 'RUN')
            self.assertEqual(app.root.getvar(app._compact_widgets[1]['absolute'].cget('textvariable')), '1.000')
            app._set_compact_view(False)
            self.assertEqual(app._mode_var.get(), Config.MODE_PROGRAM)
            self.assertTrue(state.is_running)

    def test_compact_view_persists_in_configs_and_automatic_restore(self):
        app = self.app
        app._set_compact_view(True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'compact.json'
            write_config(path, app._workspace_snapshot())
            saved = read_config(path)
            self.assertTrue(saved['compact_view'])
            app._set_compact_view(False)
            app._apply_workspace(saved)
            self.assertTrue(app._compact_active)
            automatic = Path(folder) / 'automatic.json'
            with patch.object(app, '_get_data_path', return_value=automatic):
                self.patches[1].temp_original(app)
                app._saved_compact_view = False
                self.patches[0].temp_original(app)
            self.assertTrue(app._saved_compact_view)

    def test_compact_run_uses_visible_selection_and_existing_targets(self):
        app = self.app
        app._add_motor()
        app._select_only_motor(1)
        app._motor_widgets[1]['speed_var'].set('.01')
        app._motor_widgets[1]['target_var'].set('5.125')
        app._set_compact_view(True)
        app._compact_widgets[1]['selection'].invoke()
        app._compact_widgets[2]['selection'].invoke()
        self.assertFalse(app.motor_states[0].is_selected)
        self.assertTrue(app.motor_states[1].is_selected)
        with patch.object(type(app.serial_manager), 'is_connected', new_callable=PropertyMock, return_value=True), \
                patch.object(app.serial_manager, 'send', return_value=True) as send:
            app._compact_run_btn.invoke()
            name, args = P.parse_response(send.call_args.args[0])
            self.assertEqual(name, 'RUN')
            self.assertEqual(int(args[0]), 2)
            self.assertEqual(float(args[2]), 5.125)
        self.assertTrue(app._compact_active)

    def test_compact_run_program_and_step_mode_state(self):
        app = self.app
        app._select_only_motor(1)
        app.motor_states[0].instructions = [InstructionStep(3, .01)]
        app._select_mode(Config.MODE_PROGRAM)
        app._set_compact_view(True)
        with patch.object(type(app.serial_manager), 'is_connected', new_callable=PropertyMock, return_value=True), \
                patch.object(app.serial_manager, 'send', return_value=True) as send:
            app._compact_run_btn.invoke()
            self.assertEqual(float(P.parse_response(send.call_args.args[0])[1][2]), 3)
            app._on_stop_all()
            app._select_mode(Config.MODE_STEP)
            send.reset_mock()
            app._compact_run_btn.invoke()
            send.assert_not_called()
            self.assertTrue(app._compact_run_btn.instate(['disabled']))
            app._select_mode(Config.MODE_TARGET)
            self.assertFalse(app._compact_run_btn.instate(['disabled']))

    def test_compact_run_cannot_be_activated_by_enter(self):
        app = self.app
        app._select_only_motor(1)
        app._motor_widgets[0]['speed_var'].set('.01')
        app._motor_widgets[0]['target_var'].set('5')
        app._set_compact_view(True)
        app.root.deiconify()
        app._compact_run_btn.focus_force()
        app.root.update()
        with patch.object(type(app.serial_manager), 'is_connected', new_callable=PropertyMock, return_value=True), \
                patch.object(app.serial_manager, 'send', return_value=True) as send:
            for state in (0, 4, 8, 16):
                app._compact_run_btn.event_generate('<KeyPress-Return>', state=state)
                app._compact_run_btn.event_generate('<KeyRelease-Return>', state=state)
                app.root.update()
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

