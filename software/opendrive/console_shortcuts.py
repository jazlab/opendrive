"""Keyboard shortcuts and the lifetime of a held jog gesture.

The original drive IDs stay attached to a gesture until release or cancellation.
Focus/selection changes cancel that gesture; held-key repeats remain blocked
until release. Motor commands go through the application's existing actions.
This controller owns Tk bindings and timers, but never opens or writes a port.
"""
from typing import Optional, TYPE_CHECKING
import tkinter as tk

from .config import Config
from .motion_controller import MotionError

if TYPE_CHECKING:
    from opendrive_gui import OpenDriveApp


class ConsoleShortcuts:
    """Route keys on Tk's thread without depending on the current selection."""

    def __init__(self, root: tk.Tk, app: 'OpenDriveApp'):
        self.root = root
        self._app = app
        self.active_step_key = None
        self.active_step_direction = 0
        self.step_key_motor_ids = ()
        self.blocked_step_keys = set()
        self.step_unblock_jobs = {}
        self.focus_check_job = None
        self.step_key_is_jogging = False
        self.step_key_hold_job = None
        self.step_key_release_job = None

    def bind(self):
        """Bind global keyboard shortcuts for movement commands."""
        self.root.bind_all('<KeyPress>', self.on_keypress)
        self.root.bind_all('<KeyRelease>', self.on_keyrelease)
        for shortcut in self.run_shortcuts():
            self.root.bind(shortcut, self.on_run_shortcut)
        self.root.bind_all('<End>', self.on_pause_shortcut)
        self.root.bind('<FocusOut>', self.on_jog_focus_out, add='+')
        self.root.bind('<Unmap>', self.on_jog_unmap, add='+')

    def on_jog_focus_out(self, _event):
        if self.focus_check_job is None and not self._app._closing:
            self.focus_check_job = self.root.after_idle(self.check_jog_focus)

    def check_jog_focus(self):
        self.focus_check_job = None
        focus = self.root.focus_get()
        if focus is None or focus.winfo_toplevel() != self.root or self.is_editable_text_widget(focus):
            self.reset_step_key_state()

    def on_jog_unmap(self, event):
        if event.widget == self.root:
            self.reset_step_key_state()

    def on_pause_shortcut(self, event):
        """Pause selected running motors from the End key."""
        if self._app.view.compact_active:
            self._app._on_stop_all()
        else:
            self._app._on_pause()
        return "break"

    def run_shortcuts(self):
        # Command is a macOS modifier. Its Tk alias on other platforms can
        # overlap unrelated modifier states (for example keyboard lock keys).
        modifier = 'Command' if self.root.tk.call('tk', 'windowingsystem') == 'aqua' else 'Control'
        return (f'<{modifier}-Return>', f'<{modifier}-KP_Enter>')

    def on_run_shortcut(self, event):
        """Match Run Selected in Target/Program; Step mode has no Run action."""
        required_modifier = 0x0008 if self.root.tk.call('tk', 'windowingsystem') == 'aqua' else 0x0004
        if event.state & required_modifier and not self._app.view.compact_active and not self._app.view.is_step_mode():
            self._app._on_run()
        return 'break'

    def on_keypress(self, event):
        """Handle step-mode keyboard shortcuts."""
        key = 'down' if self.is_step_down_key(event) else 'up' if self.is_step_up_key(event) else None
        if key in self.blocked_step_keys:
            return self.handle_step_keypress(key, 1 if key == 'down' else -1)
        if self._app.view.compact_active:
            return None
        if not self._app.view.is_step_mode():
            return None

        widget = self.event_widget(event)
        if widget and widget.winfo_toplevel() != self.root:
            return None

        if widget and self.is_editable_text_widget(widget):
            return None

        if self.is_step_down_key(event):
            return self.handle_step_keypress("down", direction=1)

        if self.is_step_up_key(event):
            return self.handle_step_keypress("up", direction=-1)

        digit = self.shortcut_digit(event)
        if digit is None:
            return None

        self._app._select_only_motor(digit)
        return "break"

    def on_keyrelease(self, event):
        """Stop step-mode jog motion when a step shortcut key is released."""
        key = 'down' if self.is_step_down_key(event) else 'up' if self.is_step_up_key(event) else None
        if key in self.blocked_step_keys:
            previous = self.step_unblock_jobs.pop(key, None)
            if previous is not None:
                self.root.after_cancel(previous)
            def unblock():
                self.step_unblock_jobs.pop(key, None)
                self.blocked_step_keys.discard(key)
            self.step_unblock_jobs[key] = self.root.after(Config.STEP_KEY_RELEASE_DEBOUNCE_MS, unblock)
            return 'break'
        # The release belongs to the original gesture, even if focus changed.
        if not self.active_step_key:
            return None

        if self.is_step_down_key(event):
            return self.handle_step_keyrelease("down")

        if self.is_step_up_key(event):
            return self.handle_step_keyrelease("up")

        return None

    def handle_step_keypress(self, key_name: str, direction: int):
        """Handle initial step shortcut press and ignore key-repeat repeats."""
        if key_name in self.blocked_step_keys:
            pending = self.step_unblock_jobs.pop(key_name, None)
            if pending is not None:
                self.root.after_cancel(pending)
            return 'break'
        self.cancel_step_key_release_job()

        if self.active_step_key == key_name:
            return "break"

        self.reset_step_key_state()
        self.active_step_key = key_name
        self.active_step_direction = direction
        self.step_key_motor_ids = tuple(state.motor_id for state in self._app._selected_states())
        self._app._on_step(direction=direction)
        self.step_key_hold_job = self.root.after(
            Config.STEP_JOG_HOLD_DELAY_MS, self.start_step_jog
        )
        return "break"

    def handle_step_keyrelease(self, key_name: str):
        """Handle release of a step shortcut key."""
        if self.active_step_key != key_name:
            return "break"

        self.cancel_step_key_release_job()
        self.step_key_release_job = self.root.after(
            Config.STEP_KEY_RELEASE_DEBOUNCE_MS, self.finish_step_keyrelease
        )
        return "break"

    def finish_step_keyrelease(self):
        """Finish a step shortcut release after key-repeat debounce."""
        self.step_key_release_job = None
        self.cancel_step_key_hold_job()
        if self.step_key_is_jogging:
            self._app._pause_motors(self.step_key_motor_ids)
        self.active_step_key = None
        self.active_step_direction = 0
        self.step_key_is_jogging = False
        self.step_key_motor_ids = ()

    def start_step_jog(self):
        """Convert a held step shortcut into continuous jog motion."""
        self.step_key_hold_job = None
        if not self.active_step_key or self.active_step_direction == 0:
            return
        if self.step_key_release_job is not None:
            # Do not enter continuous motion while a release is being debounced.
            # A repeat press can cancel the release and this retry can then jog.
            self.step_key_hold_job = self.root.after(Config.STEP_KEY_RELEASE_DEBOUNCE_MS, self.start_step_jog)
            return

        if not self._app.serial_manager.is_connected:
            return

        selected = [self._app._get_state(mid) for mid in self.step_key_motor_ids if self._app._get_state(mid)]
        if not selected or not self._app._validate_absolute_edits_closed(selected):
            return

        step_settings = self._app._parse_step_settings()
        if not step_settings:
            return

        speed, _ = step_settings
        try:
            self._app.motion.jog(selected, self.active_step_direction, speed)
        except MotionError as error:
            self._app._show_motion_error(error)
            return

        self.step_key_is_jogging = True

    def cancel_step_key_release_job(self):
        """Cancel a pending jog stop."""
        if self.step_key_release_job is not None:
            self.root.after_cancel(self.step_key_release_job)
            self.step_key_release_job = None

    def cancel_step_key_hold_job(self):
        """Cancel a pending transition from single-step to jog."""
        if self.step_key_hold_job is not None:
            self.root.after_cancel(self.step_key_hold_job)
            self.step_key_hold_job = None

    def is_step_down_key(self, event) -> bool:
        """Return True for keys that trigger Step Down."""
        return event.keysym in ('KP_Add', 'plus', 'equal')

    def is_step_up_key(self, event) -> bool:
        """Return True for keys that trigger Step Up."""
        return event.keysym in ('KP_Subtract', 'minus', 'underscore')

    def reset_step_key_state(self):
        """Clear step shortcut repeat state."""
        if self.active_step_key:
            self.blocked_step_keys.add(self.active_step_key)
        self.cancel_step_key_release_job()
        self.cancel_step_key_hold_job()
        self._app._pause_motors(self.step_key_motor_ids)
        self.active_step_key = None
        self.active_step_direction = 0
        self.step_key_is_jogging = False
        self.step_key_motor_ids = ()

    def event_widget(self, event):
        """Return the Tk widget object for an event."""
        if isinstance(event.widget, str):
            try:
                return self.root.nametowidget(event.widget)
            except KeyError:
                return None
        return event.widget

    def is_editable_text_widget(self, widget) -> bool:
        """Return True when a keypress should be treated as text input."""
        if isinstance(widget, tk.Text):
            return True
        if isinstance(widget, tk.Entry):
            try:
                return widget.cget('state') != 'readonly'
            except tk.TclError:
                return True
        return False

    def shortcut_digit(self, event) -> Optional[int]:
        """Return a motor shortcut digit for normal or keypad number keys."""
        key = event.keysym
        if key.startswith('KP_'):
            key = key[3:]
        if key.isdigit():
            digit = int(key)
            if 1 <= digit <= Config.MAX_MOTORS:
                return digit
        return None
