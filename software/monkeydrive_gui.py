#!/usr/bin/env python3
"""
MonkeyDrive GUI - Control interface for MonkeyDrive microdrive system.

This GUI allows users to control up to 6 stepper motor-driven microdrives
for precise positioning of neural probes.

Author: MonkeyDrive Project
License: MIT
"""

import csv
import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

import tkinter as tk
from tkinter import messagebox, filedialog

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


from monkeydrive.config import Config
from monkeydrive.console_view import ConsoleView
from monkeydrive.console_shortcuts import ConsoleShortcuts
from monkeydrive.configuration import atomic_write_json, read_config, write_config
from monkeydrive.console_theme import ConsoleTheme as Theme
from monkeydrive.dialogs import InstructionsWindow, NotesWindow, ProgramLoadWindow, SettingsWindow
from monkeydrive.models import InstructionStep, MotorState, TrajectoryState
from monkeydrive.motion_controller import MotionController, MotionError, TargetMove
from monkeydrive.protocol import SerialProtocol
from monkeydrive.serial_manager import SerialManager
from monkeydrive.utils import format_mm
from monkeydrive.viewer_process import JsonViewerProcess
from monkeydrive.viewer_tasks import LatestWorker


# =============================================================================
# Main Application
# =============================================================================

class MonkeyDriveApp:
    """Coordinate drive state, controller commands, persistence, and viewers.

    ConsoleView owns presentation; ConsoleShortcuts owns keyboard gestures.
    MotionController owns movement rules and programs. SerialManager owns device
    I/O; this class routes their notifications and user actions on Tk's thread.
    """

    def __init__(self):
        """Initialize the MonkeyDrive application."""
        self._root = tk.Tk()
        self._root.title(Config.WINDOW_TITLE)

        # State
        self._motor_states: list = []
        self._hidden_motor_states: dict[int, MotorState] = {}
        self._notes_windows: dict = {}
        self._trajectory_windows: dict = {}
        self._trajectory_revisions: dict[int, int] = {}
        self._trajectory_roster = ()
        self._trajectory_summary_window: Optional[JsonViewerProcess] = None
        self._probe_summary_window: Optional[JsonViewerProcess] = None
        self._saved_mode: str = Config.MODE_TARGET
        self._saved_compact_view = False
        self._saved_port: str = ""
        self._saved_step_speed: float = Config.DEFAULT_STEP_SPEED
        self._saved_step_size: float = Config.DEFAULT_STEP_SIZE
        self._saved_anatomy_path: str = ""
        self._explorer_state: dict = {}
        self._anatomy_orientation_actual: str = ""
        self._anatomy_orientation_scanner: str = ""
        self._recent_programs: list[str] = []
        self._min_speed: float = Config.MIN_SPEED
        self._max_speed: float = Config.MAX_SPEED
        self._backlash: float = Config.DEFAULT_BACKLASH_MM
        self._closing = False
        self._config_worker = None
        self._config_busy = False
        self._config_loading = False
        self._config_load_job = None

        # Serial manager
        self._serial_manager = SerialManager(
            on_position_update=self._on_position_update,
            on_connection_lost=self._on_connection_lost,
            on_connected=self._on_connected,
        )

        # Movement rules receive plain models and values, never Tk widgets.
        self.motion = MotionController(
            send=lambda command: self._serial_manager.send(command),
            connected=lambda: self._serial_manager.is_connected,
            on_target_parsed=self._format_target_input,
            on_instruction_activated=self._on_program_instruction_activated,
            on_program_changed=lambda state: self._update_program_row(state.motor_id),
        )

        # Dialog and viewer handles
        self._settings_window: Optional[SettingsWindow] = None
        self._anatomy_window: Optional[JsonViewerProcess] = None

        # Load saved state
        self._load_state()

        # The view owns Tk widgets; application callbacks own behavior.
        self.view = ConsoleView(self._root, self)
        self.shortcuts = ConsoleShortcuts(self._root, self)
        self.view.build()
        self.shortcuts.bind()

        # Ensure at least one motor
        if len(self._motor_states) == 0:
            self._add_motor()
        else:
            self._rebuild_motor_rows()

        self.view.style_widget_tree()
        self.view.set_compact_view(self._saved_compact_view, remember=False)

        # Keep the console usable with platform font and display scaling.
        self.view.fit_window_to_content()
        self._root.resizable(True, True)

        # Start auto-save timer
        self._schedule_auto_save()
        self._schedule_viewer_process_poll()
        self._poll_serial_events()

        # Handle window close
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

    @property
    def root(self) -> tk.Tk:
        """Return the root Tk window."""
        return self._root

    @property
    def serial_manager(self) -> SerialManager:
        """Return the serial manager instance."""
        return self._serial_manager

    @property
    def motor_states(self) -> list:
        """Return list of motor states."""
        return self._motor_states

    def run(self):
        """Start the main event loop."""
        self._root.mainloop()

    def _rebuild_motor_rows(self):
        """Refresh the console and publish changed drive membership to viewers."""
        self.view.rebuild_motor_rows()
        roster = tuple(state.motor_id for state in self._motor_states)
        if self._trajectory_summary_window and roster != self._trajectory_roster:
            self._trajectory_summary_window.send({
                'type': 'motor_states',
                'motor_states': [state.to_dict() for state in self._motor_states],
                'revisions': dict(self._trajectory_revisions),
            })
        self._trajectory_roster = roster

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _select_mode(self, mode: str):
        """Select a movement mode from the segmented button control."""
        if self.view.mode_var:
            self.view.mode_var.set(mode)
        self._on_mode_change()

    def _on_mode_change(self):
        """Handle switching between movement modes."""
        self.shortcuts.reset_step_key_state()
        self.view.refresh_mode_buttons()
        self.view.update_mode_visibility()
        self._rebuild_motor_rows()
        self.view.fit_window_to_content()

    def _instruction_button_text(self, state: MotorState) -> str:
        """Return instruction editor button text for one motor."""
        return "Edit Instructions"

    def _program_step_text(self, state: MotorState) -> str:
        """Return program progress text for one motor."""
        if not state.instructions:
            return ""
        if state.instruction_status == "complete":
            return "Complete"
        index = max(0, min(state.instruction_index, len(state.instructions) - 1))
        return f"{index + 1}/{len(state.instructions)}"

    def _update_program_row(self, motor_id: int):
        """Refresh program-mode readouts for one motor."""
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if not state or not widgets:
            return

        if 'instructions_button' in widgets:
            widgets['instructions_button'].configure(text=self._instruction_button_text(state))
        if 'program_step_var' in widgets:
            widgets['program_step_var'].set(self._program_step_text(state))

        instruction = self.motion.current_instruction(state)
        if 'speed_var' in widgets:
            widgets['speed_var'].set(f"{instruction.speed:g}" if instruction else "")
        if 'target_var' in widgets:
            widgets['target_var'].set(format_mm(instruction.target) if instruction else "")

    def _on_program_instruction_activated(self, state: MotorState):
        """Reflect the controller's active instruction in trajectory views."""
        self._sync_trajectory_from_target(state)
        self._update_trajectory_summary_motor_state(state)

    def _pause_motors(self, motor_ids):
        """Resolve visible/retained models for an explicit controller pause."""
        self.motion.pause(motor_ids, [*self._motor_states, *self._hidden_motor_states.values()])

    def _focus_main_window(self):
        """Return keyboard focus to the main application window."""
        if self.shortcuts.active_step_key:
            return
        self._root.after_idle(self._root.focus_force)

    def _select_only_motor(self, motor_id: int):
        """Select one motor and deselect all others."""
        if motor_id < 1 or motor_id > len(self._motor_states):
            return
        if self.shortcuts.step_key_motor_ids and self.shortcuts.step_key_motor_ids != (motor_id,):
            self.shortcuts.reset_step_key_state()

        for state in self._motor_states:
            state.is_selected = (state.motor_id == motor_id)
            widgets = self.view.get_widgets(state.motor_id)
            if widgets:
                widgets['selected_var'].set(state.is_selected)
                self.view.apply_row_selection_style(widgets, state.is_selected)

    def _enable_step_setting_edit(self, setting: str):
        """Enable editing for a step setting field."""
        entry = self.view.step_speed_entry if setting == 'speed' else self.view.step_size_entry
        if not entry:
            return

        entry.config(state='normal', background=Theme.COLOR_ENTRY_BG)
        entry.focus_set()
        entry.select_range(0, tk.END)
        entry.bind('<Return>', lambda e, s=setting: self._confirm_step_setting_edit(s))
        entry.bind('<FocusOut>', lambda e, s=setting: self._confirm_step_setting_edit(s))

    def _confirm_step_setting_edit(self, setting: str):
        """Validate and lock a step setting field after editing."""
        entry = self.view.step_speed_entry if setting == 'speed' else self.view.step_size_entry
        variable = self.view.step_speed_var if setting == 'speed' else self.view.step_size_var
        if not entry or not variable or entry['state'] == 'readonly':
            return

        try:
            value = float(variable.get())
            if not math.isfinite(value):
                raise ValueError("Step setting must be finite")
            if setting == 'speed':
                value = min(max(value, self._min_speed), self._max_speed)
            elif value < Config.MIN_STEP_SIZE:
                value = Config.MIN_STEP_SIZE
        except ValueError:
            value = self._min_speed if setting == 'speed' else Config.MIN_STEP_SIZE

        variable.set(f"{value:.3f}")
        entry.config(state='readonly', readonlybackground=Theme.COLOR_ENTRY_BG)
        self._root.focus_set()

    def _get_state(self, motor_id: int) -> Optional[MotorState]:
        """Get MotorState for motor by ID."""
        idx = motor_id - 1
        if 0 <= idx < len(self._motor_states):
            return self._motor_states[idx]
        return None

    def _selected_states(self) -> list:
        """Return selected motor states."""
        return [s for s in self._motor_states if s.is_selected]

    def _validate_absolute_edits_closed(self, selected: list) -> bool:
        """Return True when selected motors are not editing absolute position."""
        for state in selected:
            widgets = self.view.get_widgets(state.motor_id)
            if widgets and widgets['absolute_entry']['state'] != 'readonly':
                self._safe_popup(
                    messagebox.showwarning, "Edit In Progress",
                    f"Motor {state.motor_id} absolute position is being edited. "
                    f"Press enter to confirm edit before moving."
                )
                return False
        return True

    def _parse_step_settings(self) -> Optional[tuple]:
        """Read Tk values; numeric rules belong to the motion controller."""
        speed = self.view.step_speed_var.get() if self.view.step_speed_var is not None else None
        size = self.view.step_size_var.get() if self.view.step_size_var is not None else None
        try:
            settings = self.motion.parse_step_settings(speed, size, self._min_speed, self._max_speed)
        except MotionError as error:
            self._show_motion_error(error)
            return None
        self.view.step_size_var.set(format_mm(settings[1]))
        return settings

    def _send_settings_to_controller(self):
        """Send current runtime settings to the connected controller."""
        if self._serial_manager.is_connected:
            self._serial_manager.send(
                SerialProtocol.cmd_config(self._min_speed, self._max_speed, self._backlash)
            )

    def _on_settings(self):
        """Open global settings window."""
        if self._settings_window:
            try:
                self._settings_window.destroy()
            except tk.TclError:
                pass

        self._settings_window = SettingsWindow(
            self._root, self._min_speed, self._max_speed, self._backlash,
            self._on_settings_save
        )
        self.view.style_widget_tree(self._settings_window)

    def _on_settings_save(self, min_speed: float, max_speed: float, backlash: float):
        """Save global settings and push them to the controller."""
        self._min_speed = min_speed
        self._max_speed = max_speed
        self._backlash = backlash
        self._send_settings_to_controller()
        self._save_state()
        self._settings_window = None

    def _on_anatomy(self):
        """Open the anatomy viewer window."""
        if self._anatomy_window and self._anatomy_window.is_running():
            self._anatomy_window.send_focus()
            return

        self._anatomy_window = JsonViewerProcess(
            "monkeydrive.anatomy_process",
            [
                "--initial-path", self._saved_anatomy_path,
                "--orientation-actual", self._anatomy_orientation_actual,
                "--orientation-scanner", self._anatomy_orientation_scanner,
            ],
            cwd=Path(__file__).parent,
        )

    def _on_anatomy_close(self):
        """Handle anatomy viewer close."""
        if self._anatomy_window:
            self._anatomy_window.close()
        self._anatomy_window = None

    def _on_anatomy_path_change(self, path: str):
        """Save the most recently loaded anatomy path."""
        changed = path != self._saved_anatomy_path
        self._saved_anatomy_path = path
        if changed:
            self._anatomy_orientation_actual = ""
            self._anatomy_orientation_scanner = ""
            self._send_anatomy_orientation_to_viewers()
        self._saved_anatomy_path = path
        self._refresh_trajectory_buttons()
        self._save_state()

    def _on_anatomy_orientation_change(self, actual_key: str, scanner_key: str):
        """Persist anatomy orientation correction and notify open trajectory viewers."""
        self._anatomy_orientation_actual = actual_key
        self._anatomy_orientation_scanner = scanner_key
        self._send_anatomy_orientation_to_viewers()
        self._save_state()

    def _send_anatomy_orientation_to_viewers(self):
        """Notify open trajectory windows that anatomy orientation changed."""
        message = {
            "type": "anatomy_orientation",
            "path": self._saved_anatomy_path,
            "actual": self._anatomy_orientation_actual,
            "scanner": self._anatomy_orientation_scanner,
        }
        for motor_id, window in list(self._trajectory_windows.items()):
            if window.is_running():
                window.send(message)
            else:
                self._trajectory_windows.pop(motor_id, None)
        if self._trajectory_summary_window:
            if self._trajectory_summary_window.is_running():
                self._trajectory_summary_window.send(message)
            else:
                self._trajectory_summary_window = None

    def _has_loaded_anatomy(self) -> bool:
        """Return whether an anatomy file/folder is available for trajectory planning."""
        return bool(self._saved_anatomy_path and Path(self._saved_anatomy_path).exists())

    def _refresh_trajectory_buttons(self):
        """Enable trajectory buttons only when anatomy data is available."""
        state = tk.NORMAL if self._has_loaded_anatomy() else tk.DISABLED
        if self.view.trajectory_summary_btn:
            self.view.trajectory_summary_btn.configure(state=state)
    def _poll_serial_events(self):
        """Only this Tk callback renews the controller's GUI responsiveness lease."""
        if self._closing:
            return
        try:
            self._serial_manager.poll_events()
            self._refresh_running_indicators()
            status = "Connected" if self._serial_manager.is_connected else "Connecting / disconnecting" if self._serial_manager.is_busy else "Disconnected"
            if self.view.connection_text.get() != status:
                self.view.connection_text.set(status)
            if not self._config_loading and not self._serial_manager.is_busy and not self._serial_manager.is_connected:
                self.view.connect_btn.config(text="Connect", state="normal")
        except Exception:
            logger.exception("Serial event handling failed; stopping connection")
            self._serial_manager.disconnect()
            self._on_connection_lost()
        finally:
            if not self._closing:
                self._root.after(Config.GUI_POLL_INTERVAL_MS, self._poll_serial_events)

    def _schedule_viewer_process_poll(self):
        """Poll subprocess viewer events without blocking the GUI."""
        if self._closing:
            return
        try:
            self._poll_viewer_processes()
        except Exception:
            logger.exception("Viewer event handling failed")
        finally:
            if not self._closing:
                self._root.after(20, self._schedule_viewer_process_poll)

    def _poll_viewer_processes(self):
        """Handle pending messages from anatomy/trajectory subprocesses."""
        if self._anatomy_window:
            for event in self._anatomy_window.poll_events():
                event_type = event.get("type")
                if event_type == "path_change":
                    self._on_anatomy_path_change(str(event.get("path", "")))
                elif event_type == "orientation_change":
                    self._on_anatomy_orientation_change(
                        str(event.get("actual", "")),
                        str(event.get("scanner", "")),
                    )
                elif event_type == "pause_requested":
                    self._on_pause()
                elif event_type in ("closed", "exited"):
                    self._anatomy_window = None
                elif event_type == "error":
                    logger.error("Anatomy viewer error: %s", event.get("message"))

        for motor_id, handle in list(self._trajectory_windows.items()):
            for event in handle.poll_events():
                event_type = event.get("type")
                if event_type == "save":
                    if event.get('revision', 0) != self._trajectory_revisions.get(motor_id, 0):
                        continue
                    trajectory = TrajectoryState.from_dict(event.get("trajectory", {}))
                    self._on_trajectory_save(motor_id, trajectory)
                elif event_type == "pause_requested":
                    self._on_pause()
                elif event_type in ("closed", "exited"):
                    self._trajectory_windows.pop(motor_id, None)
                elif event_type == "error":
                    logger.error(
                        "Trajectory viewer error for motor %s: %s",
                        motor_id, event.get("message")
                    )

        if self._trajectory_summary_window:
            workspace = self._trajectory_summary_window
            for event in workspace.poll_events():
                event_type = event.get('type')
                motor_id = event.get('motor_id')
                if event_type == 'save':
                    if event.get('revision', 0) == self._trajectory_revisions.get(motor_id, 0):
                        self._on_trajectory_save(motor_id, TrajectoryState.from_dict(event.get('trajectory', {})))
                elif event_type == 'explorer_state':
                    self._explorer_state = event.get('state', {})
                elif event_type == 'assign_trajectory':
                    success = self._assign_explorer_trajectory(motor_id, event.get('state', {}))
                    workspace.send({'type': 'assignment_result',
                        'message': f'Assigned to Drive {motor_id}' if success else 'Could not assign: check the drive and trajectory.'})
                elif event_type == 'pause_requested':
                    self._on_pause()
                elif event_type in ('closed', 'exited'):
                    self._trajectory_summary_window = None
                elif event_type == 'error':
                    logger.error('Trajectories viewer error: %s', event.get('message'))

        if self._probe_summary_window:
            for event in self._probe_summary_window.poll_events():
                event_type = event.get("type")
                if event_type == "pause_requested":
                    self._on_pause()
                elif event_type in ("closed", "exited"):
                    self._probe_summary_window = None
                elif event_type == "error":
                    logger.error("Probe summary viewer error: %s", event.get("message"))

    def _safe_popup(self, popup_func: Callable, title: str, message: str = None, **kwargs):
        """
        Pause motors before a dialog. The GUI watchdog remains active.
        """
        paused_any = False

        # Pause all running motors
        if self._serial_manager.is_connected:
            for state in self._motor_states:
                if state.is_running:
                    self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                    state.is_running = False
                    if state.instruction_status == "running":
                        state.instruction_status = "paused"
                        self._update_program_row(state.motor_id)
                    paused_any = True

        # Add safety note to message if motors were paused
        if paused_any and message:
            message += "\n\n(Stop commands have been queued for running motors.)"

        # Show the popup
        if message:
            return popup_func(title, message, **kwargs)
        else:
            return popup_func(title=title, **kwargs)

    # -------------------------------------------------------------------------
    # Event Handlers - Selection and Input
    # -------------------------------------------------------------------------

    def _on_selection_change(self, motor_id: int):
        """Handle motor selection checkbox change."""
        self.shortcuts.reset_step_key_state()
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if state and widgets:
            state.is_selected = widgets['selected_var'].get()
            self.view.apply_row_selection_style(widgets, state.is_selected)

    def _on_speed_change(self, motor_id: int):
        """Handle speed input change."""
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if state and widgets:
            try:
                state.speed = float(widgets['speed_var'].get())
            except ValueError:
                pass

    def _on_target_change(self, motor_id: int):
        """Handle target input change."""
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if state and widgets:
            try:
                new_target = float(widgets['target_var'].get())
                state.target = round(new_target, 3)
                self._sync_trajectory_from_target(state)
            except ValueError:
                pass

    def _on_target_focus_out(self, motor_id: int):
        """Handle target input confirmation."""
        self._on_target_change(motor_id)
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if state and widgets:
            widgets['target_var'].set(format_mm(state.target))

    def _enable_absolute_edit(self, motor_id: int):
        """Enable editing of absolute position field."""
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if widgets and state:
            # If motor is running, pause it before editing
            if state.is_running:
                if self._serial_manager.is_connected:
                    self._serial_manager.send(SerialProtocol.cmd_pause(motor_id))
                state.is_running = False

            entry = widgets['absolute_entry']
            entry.config(state='normal', background='white', highlightthickness=1)
            entry.focus_set()
            entry.select_range(0, tk.END)
            entry.bind('<Return>', lambda e, mid=motor_id: self._on_absolute_confirm(mid))
            entry.bind('<FocusOut>', lambda e, mid=motor_id: self._on_absolute_confirm(mid))

    def _on_absolute_confirm(self, motor_id: int):
        """Handle confirmation of absolute position edit."""
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if not state or not widgets:
            return

        entry = widgets['absolute_entry']
        if entry['state'] == 'readonly':
            return

        try:
            value = float(widgets['absolute_var'].get())
            if Config.MIN_ABSOLUTE_POS <= value <= Config.MAX_ABSOLUTE_POS:
                state.absolute = round(value, 3)
                # Reset relative position to 0 and set new zero point
                state.relative = 0.0
                state.relative_zero_point = state.absolute
                widgets['absolute_var'].set(format_mm(state.absolute))
                widgets['relative_var'].set("0.000")
                self._update_trajectory_motor_position(motor_id)
            else:
                raise ValueError("Position out of range")
        except ValueError:
            self._safe_popup(
                messagebox.showwarning, "Invalid Input",
                f"Absolute position must be between {Config.MIN_ABSOLUTE_POS}mm and "
                f"{Config.MAX_ABSOLUTE_POS}mm. See Motor {motor_id}."
            )
            widgets['absolute_var'].set(format_mm(state.absolute))

        entry.config(state='readonly', readonlybackground=Theme.COLOR_READONLY_BG)
        self.view.apply_row_selection_style(widgets, state.is_selected)

    def _enable_relative_edit(self, motor_id: int):
        """Enable editing of relative position field."""
        widgets = self.view.get_widgets(motor_id)
        if widgets:
            entry = widgets['relative_entry']
            entry.config(state='normal', background='white', highlightthickness=1)
            entry.focus_set()
            entry.select_range(0, tk.END)
            entry.bind('<Return>', lambda e, mid=motor_id: self._on_relative_confirm(mid))
            entry.bind('<FocusOut>', lambda e, mid=motor_id: self._on_relative_confirm(mid))

    def _on_relative_confirm(self, motor_id: int):
        """Handle confirmation of relative position edit."""
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if not state or not widgets:
            return

        entry = widgets['relative_entry']
        if entry['state'] == 'readonly':
            return

        try:
            value = float(widgets['relative_var'].get())
            state.relative = round(value, 3)
            state.relative_zero_point = state.absolute - state.relative
        except ValueError:
            widgets['relative_var'].set(format_mm(state.relative))

        widgets['relative_var'].set(format_mm(state.relative))

        entry.config(state='readonly', readonlybackground=Theme.COLOR_READONLY_BG)
        self.view.apply_row_selection_style(widgets, state.is_selected)

    def _on_notes_click(self, motor_id: int):
        """Handle notes field click to open notes window."""
        if motor_id in self._notes_windows:
            try:
                self._notes_windows[motor_id].destroy()
            except tk.TclError:
                pass

        state = self._get_state(motor_id)
        if state:
            window = NotesWindow(self._root, motor_id, state.notes, self._on_notes_save)
            self._notes_windows[motor_id] = window

    def _on_notes_save(self, motor_id: int, notes: str):
        """Handle notes save from notes window."""
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if state and widgets:
            state.notes = notes
            preview = notes[:15] + "..." if len(notes) > 15 else notes
            widgets['notes_var'].set(preview)

        if motor_id in self._notes_windows:
            del self._notes_windows[motor_id]

    def _on_instructions_click(self, motor_id: int):
        """Open instructions editor for one motor."""
        state = self._get_state(motor_id)
        if not state:
            return

        window = InstructionsWindow(
            self._root,
            motor_id,
            state.instructions,
            self._min_speed,
            self._max_speed,
            self._on_instructions_save,
        )
        window.grab_set()

    def _on_instructions_save(self, motor_id: int, instructions: list[InstructionStep]):
        """Save edited instructions for one motor."""
        state = self._get_state(motor_id)
        if not state:
            return

        previous_index = state.instruction_index
        previous_status = state.instruction_status
        if state.is_running:
            self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
            state.is_running = False
            previous_status = "paused"

        state.instructions = instructions
        if state.instructions:
            state.instruction_index = min(previous_index, len(state.instructions) - 1)
            state.instruction_status = previous_status if previous_status != "running" else "paused"
            if state.instruction_status in ("paused", "running"):
                instruction = self.motion.current_instruction(state)
                if instruction:
                    self.motion.activate_instruction(state, instruction)
        else:
            state.instruction_index = 0
            state.instruction_status = "idle"
        self._update_program_row(motor_id)
        self._save_state()

    def _on_trajectory_click(self, motor_id: int):
        """Select one drive in the unified Trajectories window."""
        if self._get_state(motor_id):
            self._on_trajectory_summary()
            if self._trajectory_summary_window:
                self._trajectory_summary_window.send({'type': 'select_drive', 'motor_id': motor_id})

    def _on_trajectory_summary(self):
        """Open Explorer, Summary, and drive tabs in one Trajectories window."""
        if self._trajectory_summary_window and self._trajectory_summary_window.is_running():
            self._trajectory_summary_window.send_focus()
            return
        self._trajectory_summary_window = None

        if not self._saved_anatomy_path:
            messagebox.showinfo(
                "No Anatomy Loaded",
                "Open the Anatomy Viewer and load an MRI before viewing trajectory summaries."
            )
            return

        handle = JsonViewerProcess(
            "monkeydrive.trajectories_process",
            [
                "--anatomy-path", self._saved_anatomy_path,
                "--motor-states-json", json.dumps([state.to_dict() for state in self._motor_states]),
                "--explorer-json", json.dumps(self._explorer_state),
                "--revisions-json", json.dumps(self._trajectory_revisions),
                "--orientation-actual", self._anatomy_orientation_actual,
                "--orientation-scanner", self._anatomy_orientation_scanner,
            ],
            cwd=Path(__file__).parent,
        )
        self._trajectory_summary_window = handle

    def _on_probe_summary(self):
        """Open the live SpikeGLX probe summary viewer."""
        if self._probe_summary_window and self._probe_summary_window.is_running():
            self._probe_summary_window.send_focus()
            return
        self._probe_summary_window = JsonViewerProcess(
            "monkeydrive.probe_summary_process",
            [],
            cwd=Path(__file__).parent,
        )

    def _assign_explorer_trajectory(self, motor_id: int, parameters: dict) -> bool:
        """Assign display geometry only; never issue a motor command."""
        state = self._get_state(motor_id) if isinstance(motor_id, int) else None
        if state is None:
            return False
        keys = ('x', 'y', 'z', 'ap_rotation', 'ml_rotation', 'twist_rotation', 'zoom')
        try:
            geometry = {key: float(parameters[key]) for key in keys}
        except (KeyError, TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in geometry.values()):
            return False
        if not .5 <= geometry['zoom'] <= 10 or any(abs(geometry[key]) > 180 for key in keys[3:6]):
            return False
        data = state.trajectory.to_dict()
        data.update(geometry)
        state.trajectory = TrajectoryState.from_dict(data)
        revision = self._trajectory_revisions.get(motor_id, 0) + 1
        self._trajectory_revisions[motor_id] = revision
        viewer = self._trajectory_windows.get(motor_id)
        if viewer and viewer.is_running():
            viewer.send({'type': 'trajectory_state', 'trajectory': data, 'revision': revision})
        if self._trajectory_summary_window:
            self._trajectory_summary_window.send({'type': 'trajectory_state', 'motor_id': motor_id,
                'trajectory': data, 'revision': revision, 'select': True})
        self._update_trajectory_summary_motor_state(state)
        self._save_state()
        return True

    def _on_trajectory_save(self, motor_id: int, trajectory):
        """Save trajectory parameters for one motor."""
        state = self._get_state(motor_id)
        if state:
            probe_changed = self._trajectory_probe_changed(state.trajectory, trajectory)
            state.trajectory = trajectory
            if probe_changed:
                self._sync_trajectory_from_target(state)
            else:
                self._sync_target_from_trajectory(state)
            self._update_trajectory_summary_motor_state(state)
            self._save_state()

    def _trajectory_probe_changed(self, previous: TrajectoryState, current: TrajectoryState) -> bool:
        """Return True when the probe calibration changed in a trajectory save."""
        return (
            previous.probe_location_set != current.probe_location_set
            or round(previous.probe_offset, 6) != round(current.probe_offset, 6)
            or round(previous.probe_motor_position, 6) != round(current.probe_motor_position, 6)
        )

    def _sync_target_from_trajectory(self, state: MotorState):
        """Convert a locked MRI target depth into the motor target position."""
        trajectory = state.trajectory
        if not (trajectory.probe_location_set and trajectory.target_location_set):
            return

        target = round(
            trajectory.probe_motor_position + (trajectory.probe_offset - trajectory.target_offset),
            3
        )
        if not (Config.MIN_TARGET_POS <= target <= Config.MAX_TARGET_POS):
            logger.warning(
                "Trajectory target for Motor %s is outside target bounds: %.3f mm",
                state.motor_id,
                target,
            )
            return

        state.target = target
        widgets = self.view.get_widgets(state.motor_id)
        if widgets and 'target_var' in widgets:
            widgets['target_var'].set(format_mm(state.target))

    def _sync_trajectory_from_target(self, state: MotorState):
        """Convert a motor target position into the MRI target line position."""
        trajectory = state.trajectory
        if not (trajectory.probe_location_set and trajectory.target_location_set):
            return

        target_offset = round(
            trajectory.probe_offset - (state.target - trajectory.probe_motor_position),
            3
        )
        trajectory.target_offset = target_offset

        window = self._trajectory_windows.get(state.motor_id)
        if window and window.is_running():
            window.send_target_offset(target_offset)

        self._update_trajectory_summary_motor_state(state)

    def _on_trajectory_close(self, motor_id: int):
        """Handle trajectory viewer close."""
        window = self._trajectory_windows.get(motor_id)
        if window:
            window.close()
        self._trajectory_windows.pop(motor_id, None)

    # -------------------------------------------------------------------------
    # Port Management
    # -------------------------------------------------------------------------

    def _refresh_ports(self, event=None):
        """Refresh the list of available serial ports."""
        ports = SerialManager.list_ports()
        current = self.view.port_var.get() if self.view.port_var else ""
        if current and current not in ports:
            ports.insert(0, current)
        self.view.port_combo['values'] = ports

    def _on_connect(self):
        """Handle connect/disconnect button click."""
        if self._config_loading and not self._serial_manager.is_connected:
            return
        port = self.view.port_var.get()
        if not port:
            messagebox.showwarning("No Port", "Please select a port.")
            return
        self._saved_port = port

        if self._serial_manager.is_connected:
            # Pause all running motors before disconnecting
            self.shortcuts.reset_step_key_state()
            for state in self._motor_states:
                if state.is_running:
                    self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                    state.is_running = False
                    if state.instruction_status == "running":
                        state.instruction_status = "paused"
                        self._update_program_row(state.motor_id)
            self._serial_manager.disconnect()
            self.motion.step_targets.clear()
            self.view.connect_btn.config(text="Disconnecting...", state="disabled")
            self._focus_main_window()
        else:
            if self._serial_manager.connect(port):
                self.view.connect_btn.config(text="Connecting...", state="disabled")
            else:
                self.view.connect_btn.config(text="Disconnecting...", state="disabled")

    def _on_connected(self):
        """Finish connection setup on the Tk thread after the worker opens the port."""
        self._send_settings_to_controller()
        self.view.connect_btn.config(text="Disconnect", state="normal")
        self._save_state()
        self._focus_main_window()

    def _on_connection_lost(self):
        """Handle a queued connection-loss event on the Tk thread."""
        self.shortcuts.reset_step_key_state()
        for state in self._motor_states:
            if state.instruction_status == "running":
                state.instruction_status = "paused"
                self._update_program_row(state.motor_id)
            state.is_running = False
        self.motion.step_targets.clear()
        self._root.after(0, self._show_connection_lost_dialog)

    def _show_connection_lost_dialog(self):
        """Show connection lost dialog on main thread."""
        self.view.connect_btn.config(text="Connect", state="normal")
        messagebox.showwarning(
            "Connection Lost",
            "The connection was stopped and a stop command was attempted. "
            "If communication failed, the controller's heartbeat timeout stops motion.\n\n"
            f"{self._serial_manager.failure_reason}\n\n"
            "Check the controller before reconnecting. Movement will not resume automatically."
        )

    # -------------------------------------------------------------------------
    # Motor Management
    # -------------------------------------------------------------------------

    def _add_motor(self):
        """Add a new motor row."""
        if len(self._motor_states) >= Config.MAX_MOTORS:
            self._safe_popup(
                messagebox.showinfo, "Maximum Motors",
                f"Cannot add more than {Config.MAX_MOTORS} motor(s)."
            )
            return

        motor_id = len(self._motor_states) + 1
        state = self._hidden_motor_states.pop(motor_id, None) or MotorState(motor_id=motor_id)
        self._motor_states.append(state)

        self._rebuild_motor_rows()

    def _remove_motor(self):
        """Hide the last drive, retaining its calibration and configuration."""
        if len(self._motor_states) <= 1:
            return

        last_state = self._motor_states[-1]
        self.shortcuts.reset_step_key_state()
        self._pause_motors([last_state.motor_id])
        if last_state.motor_id in self._notes_windows:
            self._notes_windows[last_state.motor_id]._on_close()
        last_state.is_selected = False
        self._hidden_motor_states[last_state.motor_id] = last_state

        self._motor_states.pop()
        self._rebuild_motor_rows()
        self._save_state()

    # -------------------------------------------------------------------------
    # Control Actions
    # -------------------------------------------------------------------------

    def _refresh_running_indicators(self):
        """Render the controller's transient destination status on the Tk timer."""
        for state, widgets in zip(self._motor_states, self.view.motor_widgets):
            running = self.motion.is_moving(state)
            if widgets.get('motion_status_running') != running:
                widgets['motion_status'].configure(
                    text='Running' if running else 'Idle',
                    foreground='#15734a' if running else '#626e7d')
                widgets['motion_status_running'] = running

    def _show_motion_error(self, error: MotionError):
        popup = messagebox.showinfo if error.informational else messagebox.showwarning
        self._safe_popup(popup, error.title, str(error))

    def _movement_selection(self) -> Optional[list[MotorState]]:
        """Check action availability and uncommitted position edits in the UI."""
        selected = self._selected_states()
        try:
            self.motion.require_ready(selected)
        except MotionError as error:
            self._show_motion_error(error)
            return None
        return selected if self._validate_absolute_edits_closed(selected) else None

    def _format_target_input(self, state: MotorState):
        widgets = self.view.get_widgets(state.motor_id)
        if widgets:
            widgets['target_var'].set(format_mm(state.target))

    def _on_run(self):
        """Collect fields for Run Selected; the controller validates and queues."""
        selected = self._movement_selection()
        if selected is None:
            return
        try:
            if self.view.is_program_mode():
                self.motion.run_program(selected, self._min_speed, self._max_speed)
            else:
                moves = []
                for state in selected:
                    widgets = self.view.get_widgets(state.motor_id)
                    speed = widgets['speed_var'].get() if widgets else state.speed
                    target = widgets['target_var'].get() if widgets else state.target
                    moves.append(TargetMove(state, speed, target))
                self.motion.run_targets(moves, self._min_speed, self._max_speed)
        except MotionError as error:
            self._show_motion_error(error)

    def _on_step(self, direction: int):
        """Read shared step fields and request a selected-drive increment."""
        selected = self._movement_selection()
        if selected is None:
            return
        settings = self._parse_step_settings()
        if settings is None:
            return
        try:
            self.motion.step(selected, direction, *settings)
        except MotionError as error:
            self._show_motion_error(error)
            return
        self._focus_main_window()

    def _on_pause(self):
        """Handle Pause button click."""
        self.shortcuts.reset_step_key_state()
        self._pause_motors(state.motor_id for state in self._selected_states())

    def _on_stop_all(self):
        """Pause every controller drive and cancel any held keyboard gesture."""
        self.motion.pause_all([*self._motor_states, *self._hidden_motor_states.values()])
        self.shortcuts.reset_step_key_state()
        self._refresh_running_indicators()

    def _on_zero(self):
        """Zero relative position for selected motors."""
        for state in self._motor_states:
            if state.is_selected:
                self._zero_motor(state.motor_id)

    def _zero_motor(self, motor_id: int):
        """Set this drive's relative origin without changing its absolute position."""
        state = self._get_state(motor_id)
        if state is None:
            return
        state.relative_zero_point = state.absolute
        state.relative = 0.0
        widgets = self.view.get_widgets(motor_id)
        if widgets:
            widgets['relative_var'].set("0.000")

    def _on_export(self):
        """Export data to CSV file."""
        # Pause any running motors before opening file dialog
        if self._serial_manager.is_connected:
            for state in self._motor_states:
                if state.is_running:
                    self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                    state.is_running = False
                    if state.instruction_status == "running":
                        state.instruction_status = "paused"
                        self._update_program_row(state.motor_id)

        try:
            filepath = filedialog.asksaveasfilename(
                parent=self._root,
                title="Export Data",
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
                initialfile=f"monkeydrive_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )

            if not filepath:
                return

            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Date and Time", "Motor", "Speed (mm/s)", "Target (mm)",
                    "Absolute (mm)", "Relative (mm)", "Notes"
                ])

                timestamp = datetime.now().strftime("%m/%d/%y %H:%M")
                for state in self._motor_states:
                    writer.writerow([
                        timestamp,
                        f"Motor {state.motor_id}",
                        f"{state.speed:.3f}",
                        f"{state.target:.3f}",
                        f"{state.absolute:.3f}",
                        f"{state.relative:.3f}",
                        state.notes
                    ])

            messagebox.showinfo("Export Complete", f"Data exported to:\n{filepath}")

        except IOError as e:
            messagebox.showerror("Export Error", f"Failed to export: {e}")

    def _on_load_program(self):
        """Open the program loader dialog."""
        recent_programs = [path for path in self._recent_programs if Path(path).exists()]
        if recent_programs != self._recent_programs:
            self._recent_programs = recent_programs
            self._save_state()

        window = ProgramLoadWindow(
            self._root,
            recent_programs,
            self._load_program_file,
        )
        self.view.style_widget_tree(window)
        window.grab_set()

    def _load_program_file(self, filepath: str):
        """Load a CSV program containing instructions for one or more motors."""
        if not filepath:
            return

        try:
            grouped: dict[int, list[tuple[int, InstructionStep]]] = {}
            with open(filepath, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                required = {'motor', 'target_mm', 'speed_mm_s'}
                if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                    raise ValueError("Program CSV must include motor, target_mm, and speed_mm_s columns.")

                for row_number, row in enumerate(reader, start=2):
                    motor_id = int(row['motor'])
                    step_number = int(row.get('step') or len(grouped.get(motor_id, [])) + 1)
                    target = round(float(row['target_mm']), 3)
                    speed = float(row['speed_mm_s'])
                    notes = row.get('notes', '').strip()

                    if not self._get_state(motor_id):
                        raise ValueError(f"Row {row_number}: Motor {motor_id} does not exist in the GUI.")
                    if not (Config.MIN_TARGET_POS <= target <= Config.MAX_TARGET_POS):
                        raise ValueError(f"Row {row_number}: target {target:.3f} mm is out of range.")
                    if not (self._min_speed <= speed <= self._max_speed):
                        raise ValueError(f"Row {row_number}: speed {speed:g} mm/s is out of range.")

                    grouped.setdefault(motor_id, []).append((
                        step_number,
                        InstructionStep(target=target, speed=speed, notes=notes),
                    ))

            for state in self._motor_states:
                if state.is_running:
                    self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                    state.is_running = False
                state.instructions = []
                state.instruction_index = 0
                state.instruction_status = "idle"

            for motor_id, entries in grouped.items():
                state = self._get_state(motor_id)
                if not state:
                    continue
                entries.sort(key=lambda item: item[0])
                state.instructions = [step for _step_number, step in entries]
                state.instruction_index = 0
                state.instruction_status = "idle"

            for state in self._motor_states:
                self._update_program_row(state.motor_id)

            self._remember_program_path(filepath)
            self._save_state()
            messagebox.showinfo("Program Loaded", f"Loaded program:\n{filepath}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Load Program Failed", str(exc))

    def _remember_program_path(self, filepath: str):
        """Add a successfully loaded program path to recent programs."""
        path = str(Path(filepath).expanduser())
        self._recent_programs = [
            existing for existing in self._recent_programs
            if existing != path and Path(existing).exists()
        ]
        self._recent_programs.insert(0, path)
        self._recent_programs = self._recent_programs[:10]

    def _on_save_program(self):
        """Save current per-motor instructions as one CSV program."""
        filepath = filedialog.asksaveasfilename(
            parent=self._root,
            title="Save Program",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"monkeydrive_program_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        if not filepath:
            return

        try:
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["motor", "step", "target_mm", "speed_mm_s", "notes"])
                for state in self._motor_states:
                    for index, instruction in enumerate(state.instructions, start=1):
                        writer.writerow([
                            state.motor_id,
                            index,
                            f"{instruction.target:.3f}",
                            f"{instruction.speed:.6f}",
                            instruction.notes,
                        ])
            messagebox.showinfo("Program Saved", f"Saved program to:\n{filepath}")
        except OSError as exc:
            messagebox.showerror("Save Program Failed", str(exc))

    def _on_reset_program(self):
        """Reset selected motors' instruction progress to the first step."""
        selected = [s for s in self._motor_states if s.is_selected]
        if not selected:
            self._safe_popup(messagebox.showinfo, "No Selection", "Select at least one motor.")
            return

        for state in selected:
            if state.is_running:
                self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                state.is_running = False
            state.instruction_index = 0
            state.instruction_status = "idle"
            self.motion.step_targets.pop(state.motor_id, None)
            self._update_program_row(state.motor_id)

        self._save_state()

    def _on_clear(self):
        """Clear selected motors - pause if running, then wipe values."""
        selected = [s for s in self._motor_states if s.is_selected]

        if not selected:
            self._safe_popup(messagebox.showinfo, "No Selection", "Select at least one motor.")
            return

        clears_instructions = any(state.instructions for state in selected)
        clear_message = (
            f"Clear {len(selected)} selected motor row(s)?\n\n"
            "This will pause selected motors and reset speed, target, position, relative zero, and notes."
        )
        if clears_instructions:
            clear_message += "\n\nProgram instructions for selected motors will also be deleted."
        if not messagebox.askyesno("Clear Selected", clear_message, parent=self._root):
            return

        # Pause selected motors that are running
        for state in selected:
            if state.is_running:
                self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
            self.motion.step_targets.pop(state.motor_id, None)

        # Clear values for selected motors
        for state in selected:
            state.speed = 0.0
            state.target = 0.0
            state.absolute = 0.0
            state.relative = 0.0
            state.relative_zero_point = 0.0
            state.notes = ""
            state.is_running = False
            state.instructions = []
            state.instruction_index = 0
            state.instruction_status = "idle"

            widgets = self.view.get_widgets(state.motor_id)
            if widgets:
                if 'speed_var' in widgets:
                    widgets['speed_var'].set("")
                if 'target_var' in widgets:
                    widgets['target_var'].set("")
                if 'program_step_var' in widgets:
                    widgets['program_step_var'].set("")
                if 'instructions_button' in widgets:
                    widgets['instructions_button'].configure(text=self._instruction_button_text(state))
                widgets['absolute_var'].set("0.000")
                widgets['relative_var'].set("0.000")
                widgets['notes_var'].set("")

        self._root.update_idletasks()

    # -------------------------------------------------------------------------
    # Position Updates
    # -------------------------------------------------------------------------

    def _on_position_update(self, motor_id: int, absolute: float):
        """Apply reported position through the controller, then refresh the view."""
        state = self._get_state(motor_id) or self._hidden_motor_states.get(motor_id)
        if state:
            self.motion.apply_position(state, absolute)
        self._update_position_display(motor_id, absolute)

    def _update_position_display(self, motor_id: int, absolute: float):
        """Update position display widgets on main thread."""
        state = self._get_state(motor_id)
        widgets = self.view.get_widgets(motor_id)
        if state and widgets:
            self.motion.advance_program(state)
            if widgets['absolute_entry']['state'] == 'readonly':
                widgets['absolute_var'].set(format_mm(state.absolute))
            if widgets['relative_entry']['state'] == 'readonly':
                widgets['relative_var'].set(format_mm(state.relative))
            self._update_trajectory_motor_position(motor_id)

    def _update_trajectory_motor_position(self, motor_id: int):
        """Notify an open trajectory viewer that its motor position changed."""
        state = self._get_state(motor_id)
        window = self._trajectory_windows.get(motor_id)
        self._update_trajectory_summary_motor_position(motor_id)
        if not state or not window:
            return
        try:
            if window.is_running():
                window.send_motor_position(state.absolute)
            else:
                self._trajectory_windows.pop(motor_id, None)
        except OSError:
            pass

    def _update_trajectory_summary_motor_position(self, motor_id: int):
        """Notify the trajectory summary viewer that one motor position changed."""
        state = self._get_state(motor_id)
        window = self._trajectory_summary_window
        if not state or not window:
            return
        try:
            if window.is_running():
                window.send({
                    "type": "motor_position",
                    "motor_id": motor_id,
                    "position": state.absolute,
                })
            else:
                self._trajectory_summary_window = None
        except OSError:
            pass

    def _update_trajectory_summary_motor_state(self, state: MotorState):
        """Notify the trajectory summary viewer that one motor state changed."""
        window = self._trajectory_summary_window
        if not window:
            return
        try:
            if window.is_running():
                window.send({
                    "type": "motor_state",
                    "motor_state": state.to_dict(),
                    "revision": self._trajectory_revisions.get(state.motor_id, 0),
                })
            else:
                self._trajectory_summary_window = None
        except OSError:
            pass

    # -------------------------------------------------------------------------
    # State Persistence
    # -------------------------------------------------------------------------

    def _get_data_path(self) -> Path:
        """Get path to state data file."""
        return Path(__file__).parent / Config.DATA_FILE

    def _save_state(self):
        """Save current state to JSON file."""
        try:
            atomic_write_json(self._get_data_path(), self._workspace_snapshot())
        except (OSError, ValueError, TypeError) as e:
            logger.error("Failed to save state: %s", e)

    def _workspace_snapshot(self):
        """Shared contents for automatic restore and named configurations."""
        data = {
            'motor_states': [s.to_dict() for s in self._motor_states],
            'hidden_motor_states': [s.to_dict() for s in self._hidden_motor_states.values()],
            'port': self.view.port_var.get() if self.view.port_var else self._saved_port,
            'mode': self.view.mode_var.get() if self.view.mode_var else self._saved_mode,
            'compact_view': self.view.compact_view_var.get(),
            'step_speed': self.view.step_speed_var.get() if self.view.step_speed_var else f"{self._saved_step_speed:g}",
            'step_size': self.view.step_size_var.get() if self.view.step_size_var else format_mm(self._saved_step_size),
            'min_speed': self._min_speed,
            'max_speed': self._max_speed,
            'backlash': round(self._backlash, 3),
            'anatomy_path': self._saved_anatomy_path,
            'explorer_state': dict(self._explorer_state),
            'anatomy_orientation_actual': self._anatomy_orientation_actual,
            'anatomy_orientation_scanner': self._anatomy_orientation_scanner,
            'recent_programs': self._recent_programs[:10],
        }
        # Notes editors save on close; a config also includes their current text.
        for motor_id, window in self._notes_windows.items():
            if window.winfo_exists() and 1 <= motor_id <= len(data['motor_states']):
                data['motor_states'][motor_id - 1]['notes'] = window._text.get('1.0', 'end-1c').strip()
        return data

    def _set_config_busy(self, busy):
        self._config_busy = busy
        for index in (0, 1):
            self.view.config_menu.entryconfigure(index, state='disabled' if busy else 'normal')

    def _config_task(self, compute, complete):
        if self._config_worker is None:
            self._config_worker = LatestWorker(self._root)
        self._set_config_busy(True)
        self._config_worker.submit('config', compute, complete)

    def _on_save_config(self):
        if self._config_busy:
            return
        self.shortcuts.reset_step_key_state()
        path = self._safe_popup(
            filedialog.asksaveasfilename, 'Save Config', parent=self._root,
            defaultextension='.json', filetypes=[('MonkeyDrive config', '*.json')],
            initialfile=f'monkeydrive_config_{datetime.now():%Y%m%d_%H%M%S}.json')
        if not path:
            return
        self._poll_viewer_processes()
        snapshot = self._workspace_snapshot()
        if snapshot['anatomy_path']:
            snapshot['anatomy_path'] = str(Path(snapshot['anatomy_path']).resolve())

        def complete(_result, error):
            self._set_config_busy(False)
            if error:
                self._safe_popup(messagebox.showerror, 'Save Config Failed', str(error), parent=self._root)
            else:
                self._safe_popup(messagebox.showinfo, 'Config Saved', f'Saved config to:\n{path}', parent=self._root)
        self._config_task(lambda: write_config(path, snapshot), complete)

    def _on_load_config(self):
        if self._config_busy:
            return
        self.shortcuts.reset_step_key_state()
        path = self._safe_popup(
            filedialog.askopenfilename, 'Load Config', parent=self._root,
            filetypes=[('MonkeyDrive config', '*.json')])
        if not path:
            return
        self._config_loading = True

        def complete(workspace, error):
            if error:
                self._config_loading = False
                self._set_config_busy(False)
                self._safe_popup(messagebox.showerror, 'Load Config Failed', str(error), parent=self._root)
                return
            # Disconnect clears queued positions and commands; do not allow a
            # late hardware update to overwrite restored coordinates.
            self.shortcuts.reset_step_key_state()
            self._serial_manager.disconnect()
            self.view.connect_btn.configure(text='Disconnecting...', state='disabled')
            self._finish_config_load(workspace)
        self._config_task(lambda: read_config(path), complete)

    def _finish_config_load(self, workspace):
        self._config_load_job = None
        if self._closing:
            return
        if self._serial_manager.is_busy:
            self._config_load_job = self._root.after(20, lambda: self._finish_config_load(workspace))
            return
        self._apply_workspace(workspace)
        self._config_loading = False
        self._set_config_busy(False)
        message = 'Configuration loaded. The controller is disconnected; no movement was started.'
        if self._saved_anatomy_path and not self._has_loaded_anatomy():
            message += '\n\nThe MRI could not be found at its saved location. Open Anatomy to select the file or folder again.'
        self._safe_popup(messagebox.showinfo, 'Config Loaded', message, parent=self._root)

    def _apply_workspace(self, data):
        """Apply already-validated state only after the serial worker has closed."""
        reopen_anatomy = bool(self._anatomy_window and self._anatomy_window.is_running())
        reopen_trajectories = bool(self._trajectory_summary_window and self._trajectory_summary_window.is_running())
        # Detach old handles before replacing state, so queued viewer edits
        # cannot leak from the previous workspace into the new one.
        viewers = [self._anatomy_window, self._trajectory_summary_window, self._probe_summary_window,
                   *self._trajectory_windows.values()]
        self._anatomy_window = self._trajectory_summary_window = self._probe_summary_window = None
        self._trajectory_windows.clear()
        for window in viewers:
            if window:
                window.close()
        for window in self._notes_windows.values():
            window.destroy()
        self._notes_windows.clear()
        if self._settings_window:
            self._settings_window.destroy()
            self._settings_window = None
        for row in self.view.motor_widgets:
            for widget in row['all_widgets']:
                widget.unbind('<FocusOut>')
        self._motor_states = [MotorState.from_dict(item) for item in data['motor_states']]
        self._hidden_motor_states = {item['motor_id']: MotorState.from_dict(item)
                                     for item in data.get('hidden_motor_states', [])}
        self._trajectory_revisions.clear()
        self._trajectory_roster = ()
        self.motion.step_targets.clear()
        self.motion.motion_targets.clear()
        self._saved_mode, self._saved_port = data['mode'], data['port']
        self._saved_compact_view = data.get('compact_view', False)
        self._saved_step_speed, self._saved_step_size = data['step_speed'], data['step_size']
        self._min_speed, self._max_speed, self._backlash = data['min_speed'], data['max_speed'], data['backlash']
        self._saved_anatomy_path = data['anatomy_path']
        self._explorer_state = data['explorer_state']
        self._anatomy_orientation_actual = data['anatomy_orientation_actual']
        self._anatomy_orientation_scanner = data['anatomy_orientation_scanner']
        self._recent_programs = data['recent_programs']
        self.view.port_var.set(self._saved_port)
        self.view.step_speed_var.set(f'{self._saved_step_speed:g}')
        self.view.step_size_var.set(format_mm(self._saved_step_size))
        self.view.connect_btn.configure(text='Connect', state='normal')
        self.view.connection_text.set('Disconnected')
        self._select_mode(self._saved_mode)
        self.view.set_compact_view(self._saved_compact_view, remember=False)
        self._refresh_trajectory_buttons()
        self._refresh_running_indicators()
        self._save_state()
        if reopen_anatomy:
            self._on_anatomy()
        if reopen_trajectories and self._has_loaded_anatomy():
            self._on_trajectory_summary()

    def _load_state(self):
        """Load state from JSON file."""
        try:
            path = self._get_data_path()
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                self._motor_states = [
                    MotorState.from_dict(s) for s in data.get('motor_states', [])
                ]
                self._hidden_motor_states = {
                    item['motor_id']: MotorState.from_dict(item)
                    for item in data.get('hidden_motor_states', [])
                }

                for state in [*self._motor_states, *self._hidden_motor_states.values()]:
                    state.is_running = False
                    if state.instruction_status == "running":
                        state.instruction_status = "paused"
                    state.target = round(state.target, 3)
                    state.absolute = round(state.absolute, 3)
                    state.relative = round(state.relative, 3)
                    state.relative_zero_point = round(state.relative_zero_point, 3)

                mode = data.get('mode', Config.MODE_TARGET)
                valid_modes = (Config.MODE_TARGET, Config.MODE_STEP, Config.MODE_PROGRAM)
                self._saved_mode = mode if mode in valid_modes else Config.MODE_TARGET
                self._saved_compact_view = bool(data.get('compact_view', False))
                self._saved_port = str(data.get('port', ""))
                self._saved_step_speed = float(data.get('step_speed', Config.DEFAULT_STEP_SPEED))
                self._saved_step_size = round(float(data.get('step_size', Config.DEFAULT_STEP_SIZE)), 3)
                self._min_speed = float(data.get('min_speed', Config.MIN_SPEED))
                self._max_speed = float(data.get('max_speed', Config.MAX_SPEED))
                self._backlash = round(float(data.get('backlash', Config.DEFAULT_BACKLASH_MM)), 3)
                self._saved_anatomy_path = str(data.get('anatomy_path', ""))
                explorer = data.get('explorer_state', {})
                self._explorer_state = explorer if isinstance(explorer, dict) else {}
                self._anatomy_orientation_actual = str(data.get('anatomy_orientation_actual', ""))
                self._anatomy_orientation_scanner = str(data.get('anatomy_orientation_scanner', ""))
                recent_programs = data.get('recent_programs', [])
                if isinstance(recent_programs, list):
                    self._recent_programs = [
                        str(path) for path in recent_programs
                        if isinstance(path, str) and Path(path).exists()
                    ][:10]
                else:
                    self._recent_programs = []

                if self._min_speed <= 0 or self._max_speed < self._min_speed or self._backlash < 0:
                    raise ValueError("Invalid persisted settings")
        except (IOError, json.JSONDecodeError) as e:
            logger.error("Failed to load state: %s", e)
            self._motor_states = []
        except ValueError as e:
            logger.error("Failed to load step settings: %s", e)
            self._saved_mode = Config.MODE_TARGET
            self._saved_port = ""
            self._saved_step_speed = Config.DEFAULT_STEP_SPEED
            self._saved_step_size = Config.DEFAULT_STEP_SIZE
            self._min_speed = Config.MIN_SPEED
            self._max_speed = Config.MAX_SPEED
            self._backlash = Config.DEFAULT_BACKLASH_MM
            self._saved_anatomy_path = ""
            self._anatomy_orientation_actual = ""
            self._anatomy_orientation_scanner = ""
            self._recent_programs = []

    def _schedule_auto_save(self):
        """Schedule periodic auto-save of state."""
        self._save_state()
        self._root.after(int(Config.AUTO_SAVE_INTERVAL * 1000), self._schedule_auto_save)

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def _on_close(self):
        """Handle window close event."""
        if self._closing:
            return
        self._closing = True
        if self._config_worker:
            self._config_worker.close()
        if self._config_load_job is not None:
            self._root.after_cancel(self._config_load_job)
            self._config_load_job = None
        self.shortcuts.reset_step_key_state()
        self._serial_manager.disconnect()
        self._shutdown_deadline = time.monotonic() + Config.SERIAL_SHUTDOWN_TIMEOUT
        self._finish_close()

    def _finish_close(self):
        """Give the worker time to send its final stop without blocking Tk."""
        if self._serial_manager.is_busy and time.monotonic() < self._shutdown_deadline:
            self._root.after(Config.GUI_POLL_INTERVAL_MS, self._finish_close)
            return
        self._save_state()

        for window in self._notes_windows.values():
            try:
                window.destroy()
            except tk.TclError:
                pass

        if self._anatomy_window:
            self._anatomy_window.terminate()

        if self._trajectory_summary_window:
            self._trajectory_summary_window.terminate()
        if self._probe_summary_window:
            self._probe_summary_window.terminate()

        for window in self._trajectory_windows.values():
            window.terminate()

        self._root.destroy()


# =============================================================================
# Entry Point
# =============================================================================

def main():
    """Main entry point for MonkeyDrive GUI."""
    app = MonkeyDriveApp()
    app.run()


if __name__ == "__main__":
    main()
