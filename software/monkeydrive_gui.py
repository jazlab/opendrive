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
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


from monkeydrive.config import Config
from monkeydrive.dialogs import NotesWindow, SettingsWindow
from monkeydrive.models import MotorState, TrajectoryState
from monkeydrive.protocol import SerialProtocol
from monkeydrive.serial_manager import SerialManager
from monkeydrive.utils import format_mm
from monkeydrive.viewer_process import JsonViewerProcess


# =============================================================================
# Main Application
# =============================================================================

class MonkeyDriveApp:
    """Main MonkeyDrive GUI application."""

    def __init__(self):
        """Initialize the MonkeyDrive application."""
        self._root = tk.Tk()
        self._root.title(Config.WINDOW_TITLE)
        self._configure_light_theme()

        # State
        self._motor_states: list = []
        self._motor_widgets: list = []
        self._notes_windows: dict = {}
        self._trajectory_windows: dict = {}
        self._trajectory_summary_window: Optional[JsonViewerProcess] = None
        self._saved_mode: str = Config.MODE_TARGET
        self._saved_step_speed: float = Config.DEFAULT_STEP_SPEED
        self._saved_step_size: float = Config.DEFAULT_STEP_SIZE
        self._saved_anatomy_path: str = ""
        self._min_speed: float = Config.MIN_SPEED
        self._max_speed: float = Config.MAX_SPEED
        self._backlash: float = Config.DEFAULT_BACKLASH_MM
        self._step_targets: dict = {}
        self._active_step_key: Optional[str] = None
        self._active_step_direction: int = 0
        self._step_key_is_jogging: bool = False
        self._step_key_hold_job = None
        self._step_key_release_job = None

        # Serial manager
        self._serial_manager = SerialManager(
            on_position_update=self._on_position_update,
            on_connection_lost=self._on_connection_lost
        )

        # GUI variables (initialized in _build_ui)
        self._port_var: Optional[tk.StringVar] = None
        self._port_combo: Optional[ttk.Combobox] = None
        self._connect_btn: Optional[tk.Button] = None
        self._mode_var: Optional[tk.StringVar] = None
        self._step_speed_var: Optional[tk.StringVar] = None
        self._step_size_var: Optional[tk.StringVar] = None
        self._step_speed_entry: Optional[tk.Entry] = None
        self._step_size_entry: Optional[tk.Entry] = None
        self._target_controls_frame: Optional[tk.Frame] = None
        self._step_controls_frame: Optional[tk.Frame] = None
        self._step_settings_frame: Optional[tk.Frame] = None
        self._settings_window: Optional[SettingsWindow] = None
        self._anatomy_window: Optional[JsonViewerProcess] = None
        self._trajectory_summary_btn: Optional[tk.Button] = None
        self._motors_container: Optional[tk.Frame] = None

        # Load saved state
        self._load_state()

        # Build UI
        self._build_ui()
        self._bind_step_mode_shortcuts()

        # Ensure at least one motor
        if len(self._motor_states) == 0:
            self._add_motor()
        else:
            self._rebuild_motor_rows()

        self._style_widget_tree()

        # Disable resizing after UI is built
        self._fit_window_to_content()
        self._root.resizable(False, False)

        # Start auto-save timer
        self._schedule_auto_save()
        self._schedule_viewer_process_poll()

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

    def _configure_light_theme(self):
        """Force readable colors on older macOS Tk builds."""
        for option, value in (
            ("*Background", Config.COLOR_BG),
            ("*Foreground", Config.COLOR_FG),
            ("*Entry.Background", Config.COLOR_ENTRY_BG),
            ("*Entry.Foreground", Config.COLOR_FG),
            ("*Text.Background", Config.COLOR_ENTRY_BG),
            ("*Text.Foreground", Config.COLOR_FG),
            ("*Button.Background", Config.COLOR_ENTRY_BG),
            ("*Button.Foreground", Config.COLOR_FG),
        ):
            self._root.option_add(option, value)

        self._root.tk_setPalette(
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
            activeBackground=Config.COLOR_ACTIVE_BG,
            activeForeground=Config.COLOR_FG,
            highlightColor=Config.COLOR_SELECT_BG,
            selectBackground=Config.COLOR_SELECT_BG,
            selectForeground=Config.COLOR_ENTRY_BG,
        )
        self._root.configure(background=Config.COLOR_BG)

        style = ttk.Style(self._root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TCombobox",
            fieldbackground=Config.COLOR_ENTRY_BG,
            background=Config.COLOR_ENTRY_BG,
            foreground=Config.COLOR_FG,
            selectbackground=Config.COLOR_SELECT_BG,
            selectforeground=Config.COLOR_ENTRY_BG,
        )

    def _style_widget_tree(self, widget=None):
        """Apply explicit colors to Tk widgets that may ignore the app palette."""
        widget = widget or self._root

        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
                widget.configure(background=Config.COLOR_BG)
            elif isinstance(widget, tk.Label):
                widget.configure(background=Config.COLOR_BG, foreground=Config.COLOR_FG)
            elif isinstance(widget, tk.Checkbutton):
                widget.configure(
                    background=Config.COLOR_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    selectcolor=Config.COLOR_ENTRY_BG,
                )
            elif isinstance(widget, tk.Radiobutton):
                widget.configure(
                    background=Config.COLOR_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    selectcolor=Config.COLOR_ENTRY_BG,
                )
            elif isinstance(widget, tk.Button):
                widget.configure(
                    background=Config.COLOR_ENTRY_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    highlightbackground=Config.COLOR_BG,
                )
            elif isinstance(widget, tk.Entry):
                readonly_bg = widget.cget("readonlybackground")
                widget.configure(
                    background=Config.COLOR_ENTRY_BG,
                    foreground=Config.COLOR_FG,
                    insertbackground=Config.COLOR_FG,
                    readonlybackground=readonly_bg or Config.COLOR_READONLY_BG,
                    disabledforeground=Config.COLOR_FG,
                )
            elif isinstance(widget, tk.Text):
                widget.configure(
                    background=Config.COLOR_ENTRY_BG,
                    foreground=Config.COLOR_FG,
                    insertbackground=Config.COLOR_FG,
                )
        except tk.TclError:
            pass

        for child in widget.winfo_children():
            self._style_widget_tree(child)

    def _fit_window_to_content(self):
        """Resize the fixed-size window to match the current layout."""
        self._root.update_idletasks()
        self._root.geometry(f"{self._root.winfo_reqwidth()}x{self._root.winfo_reqheight()}")

    # -------------------------------------------------------------------------
    # UI Construction
    # -------------------------------------------------------------------------

    def _build_ui(self):
        """Build the main UI."""
        # Main container
        main_frame = tk.Frame(self._root, padx=15, pady=15)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Row 0: Port selection
        self._build_port_selection(main_frame)

        # Row 1: Main buttons (centered)
        self._build_control_buttons(main_frame)

        # Container for Headers and Motor Rows (Grid Layout)
        self._motors_container = tk.Frame(main_frame)
        self._motors_container.pack(fill=tk.BOTH, expand=True)

    def _build_port_selection(self, parent: tk.Frame):
        """Build the port selection UI section."""
        port_frame = tk.Frame(parent)
        port_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(port_frame, text="Port:").pack(side=tk.LEFT)

        self._port_var = tk.StringVar()
        self._port_combo = ttk.Combobox(
            port_frame, textvariable=self._port_var,
            width=40, state='readonly'
        )
        self._port_combo.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self._port_combo.bind('<Button-1>', self._refresh_ports)

        action_frame = tk.Frame(port_frame)
        action_frame.pack(side=tk.RIGHT)

        self._connect_btn = tk.Button(
            port_frame, text="Connect", command=self._on_connect
        )
        self._connect_btn.pack(in_=action_frame, fill=tk.X)

        tk.Button(
            action_frame, text="Settings", command=self._on_settings
        ).pack(fill=tk.X, pady=(4, 0))

        tk.Button(
            action_frame, text="Anatomy", command=self._on_anatomy
        ).pack(fill=tk.X, pady=(4, 0))

        self._trajectory_summary_btn = tk.Button(
            action_frame, text="Trajectory Summary", command=self._on_trajectory_summary,
            state=tk.NORMAL if self._has_loaded_anatomy() else tk.DISABLED
        )
        self._trajectory_summary_btn.pack(fill=tk.X, pady=(4, 0))

    def _build_control_buttons(self, parent: tk.Frame):
        """Build the main control buttons."""
        mode_frame = tk.Frame(parent)
        mode_frame.pack(pady=(0, 8))

        tk.Label(mode_frame, text="Mode:").pack(side=tk.LEFT, padx=(0, 8))

        self._mode_var = tk.StringVar(value=self._saved_mode)
        tk.Radiobutton(
            mode_frame, text="Target", variable=self._mode_var,
            value=Config.MODE_TARGET, command=self._on_mode_change
        ).pack(side=tk.LEFT, padx=4)
        tk.Radiobutton(
            mode_frame, text="Step", variable=self._mode_var,
            value=Config.MODE_STEP, command=self._on_mode_change
        ).pack(side=tk.LEFT, padx=4)

        self._step_settings_frame = tk.Frame(parent)
        self._step_settings_frame.pack(pady=(0, 10))

        self._step_speed_var = tk.StringVar(value=f"{self._saved_step_speed:g}")
        self._step_size_var = tk.StringVar(value=format_mm(self._saved_step_size))

        tk.Label(self._step_settings_frame, text="Step speed (mm/s):").pack(side=tk.LEFT, padx=(0, 5))
        step_speed_entry = tk.Entry(
            self._step_settings_frame, textvariable=self._step_speed_var,
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground=Config.COLOR_ENTRY_BG
        )
        self._step_speed_entry = step_speed_entry
        step_speed_entry.pack(side=tk.LEFT, padx=(0, 15))
        step_speed_entry.bind('<Double-Button-1>', lambda e: self._enable_step_setting_edit('speed'))
        self._bind_numeric_stepper(
            step_speed_entry, self._step_speed_var,
            Config.STEP_SPEED_KEY_INCREMENT, decimals=3,
            min_value=lambda: self._min_speed,
            max_value=lambda: self._max_speed
        )

        tk.Label(self._step_settings_frame, text="Step size (mm):").pack(side=tk.LEFT, padx=(0, 5))
        step_size_entry = tk.Entry(
            self._step_settings_frame, textvariable=self._step_size_var,
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground=Config.COLOR_ENTRY_BG
        )
        self._step_size_entry = step_size_entry
        step_size_entry.pack(side=tk.LEFT)
        step_size_entry.bind('<Double-Button-1>', lambda e: self._enable_step_setting_edit('size'))
        self._bind_numeric_stepper(
            step_size_entry, self._step_size_var,
            Config.STEP_SIZE_KEY_INCREMENT, decimals=3,
            min_value=Config.STEP_SIZE_KEY_INCREMENT
        )

        self._target_controls_frame = tk.Frame(parent)
        self._target_controls_frame.pack(pady=(0, 15))

        target_buttons = [
            ("Run", self._on_run),
            ("Pause", self._on_pause),
            ("Zero", self._on_zero),
            ("Export", self._on_export),
            ("Clear", self._on_clear),
        ]

        for text, command in target_buttons:
            tk.Button(
                self._target_controls_frame, text=text, command=command, width=8
            ).pack(side=tk.LEFT, padx=2)

        self._step_controls_frame = tk.Frame(parent)
        self._step_controls_frame.pack(pady=(0, 15))

        step_buttons = [
            ("Step Up", lambda: self._on_step(direction=-1)),
            ("Step Down", lambda: self._on_step(direction=1)),
            ("Pause", self._on_pause),
            ("Zero", self._on_zero),
            ("Export", self._on_export),
            ("Clear", self._on_clear),
        ]

        for text, command in step_buttons:
            tk.Button(
                self._step_controls_frame, text=text, command=command, width=9
            ).pack(side=tk.LEFT, padx=2)

        self._update_mode_visibility()

    def _build_header_row(self):
        """Build the header row with column labels."""
        # Col 0: Buttons (+/-)
        adjust_frame = tk.Frame(self._motors_container)
        adjust_frame.grid(row=0, column=0, sticky='w', padx=(0, 5), pady=Config.HEADER_PAD_Y)
        
        tk.Button(
            adjust_frame, text="+", command=self._add_motor,
            font=('TkDefaultFont', 9), width=2
        ).pack(side=tk.LEFT, padx=1)
        
        tk.Button(
            adjust_frame, text="-", command=self._remove_motor,
            font=('TkDefaultFont', 9), width=2
        ).pack(side=tk.LEFT, padx=1)

        if self._is_step_mode():
            labels = [
                (1, "Absolute (mm):"),
                (2, "Relative (mm):"),
                (3, "Notes:"),
            ]
        else:
            labels = [
                (1, "Speed (mm/s):"),
                (2, "Target (mm):"),
                (3, "Absolute (mm):"),
                (4, "Relative (mm):"),
                (5, "Notes:"),
            ]

        for col, text in labels:
            tk.Label(
                self._motors_container, text=text, anchor='w'
            ).grid(row=0, column=col, sticky='w', padx=5, pady=Config.HEADER_PAD_Y)

    def _create_motor_row(self, state: MotorState, row_index: int) -> dict:
        """
        Create widgets for a motor row.
        
        Args:
            state: MotorState for this row
            row_index: Grid row index (1-based, 0 is header)
            
        Returns:
            Dictionary of widget references.
        """
        widgets = {}

        # Col 0: Motor ID Label + Checkbox
        id_frame = tk.Frame(self._motors_container)
        id_frame.grid(row=row_index, column=0, sticky='w', padx=(0, 5), pady=Config.ROW_PAD_Y)

        tk.Label(id_frame, text=f"Motor {state.motor_id}:", width=8, anchor='w').pack(side=tk.LEFT)

        widgets['selected_var'] = tk.BooleanVar(value=state.is_selected)
        widgets['checkbox'] = tk.Checkbutton(
            id_frame,
            variable=widgets['selected_var'],
            command=lambda mid=state.motor_id: self._on_selection_change(mid)
        )
        widgets['checkbox'].pack(side=tk.LEFT)

        widgets['trajectory_button'] = tk.Button(
            id_frame, text="Trajectory",
            command=lambda mid=state.motor_id: self._on_trajectory_click(mid),
            state=tk.NORMAL if self._has_loaded_anatomy() else tk.DISABLED
        )
        widgets['trajectory_button'].pack(side=tk.LEFT, padx=(4, 0))

        visible_widgets = [id_frame]

        if self._is_step_mode():
            absolute_col = 1
            relative_col = 2
            notes_col = 3
        else:
            absolute_col = 3
            relative_col = 4
            notes_col = 5

            # Col 1: Speed entry
            widgets['speed_var'] = tk.StringVar(value=f"{state.speed}" if state.speed else "")
            widgets['speed_entry'] = tk.Entry(
                self._motors_container, textvariable=widgets['speed_var'],
                width=Config.ENTRY_WIDTH
            )
            widgets['speed_entry'].grid(row=row_index, column=1, sticky='ew', padx=5, pady=Config.ROW_PAD_Y)
            widgets['speed_entry'].bind('<KeyRelease>', lambda e, mid=state.motor_id: self._on_speed_change(mid))
            widgets['speed_entry'].bind('<FocusOut>', lambda e, mid=state.motor_id: self._on_speed_change(mid))
            self._bind_numeric_stepper(
                widgets['speed_entry'], widgets['speed_var'],
                Config.SPEED_KEY_INCREMENT, decimals=3,
                min_value=lambda: self._min_speed,
                max_value=lambda: self._max_speed,
                on_change=lambda mid=state.motor_id: self._on_speed_change(mid)
            )
            self._bind_vertical_tab(widgets['speed_entry'], state.motor_id, 'speed_entry')
            visible_widgets.append(widgets['speed_entry'])

            # Col 2: Target entry
            widgets['target_var'] = tk.StringVar(value=format_mm(state.target) if state.target else "")
            widgets['target_entry'] = tk.Entry(
                self._motors_container, textvariable=widgets['target_var'],
                width=Config.ENTRY_WIDTH
            )
            widgets['target_entry'].grid(row=row_index, column=2, sticky='ew', padx=5, pady=Config.ROW_PAD_Y)
            widgets['target_entry'].bind('<KeyRelease>', lambda e, mid=state.motor_id: self._on_target_change(mid))
            widgets['target_entry'].bind('<FocusOut>', lambda e, mid=state.motor_id: self._on_target_focus_out(mid))
            self._bind_numeric_stepper(
                widgets['target_entry'], widgets['target_var'],
                Config.TARGET_KEY_INCREMENT, decimals=3,
                min_value=Config.MIN_TARGET_POS,
                max_value=Config.MAX_TARGET_POS,
                on_change=lambda mid=state.motor_id: self._on_target_change(mid)
            )
            self._bind_vertical_tab(widgets['target_entry'], state.motor_id, 'target_entry')
            visible_widgets.append(widgets['target_entry'])

        # Absolute position
        widgets['absolute_var'] = tk.StringVar(value=format_mm(state.absolute))
        widgets['absolute_entry'] = tk.Entry(
            self._motors_container, textvariable=widgets['absolute_var'],
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground='lightgray'
        )
        widgets['absolute_entry'].grid(row=row_index, column=absolute_col, sticky='ew', padx=5, pady=Config.ROW_PAD_Y)
        widgets['absolute_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._enable_absolute_edit(mid))

        # Relative position
        widgets['relative_var'] = tk.StringVar(value=format_mm(state.relative))
        widgets['relative_entry'] = tk.Entry(
            self._motors_container, textvariable=widgets['relative_var'],
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground='lightgray'
        )
        widgets['relative_entry'].grid(row=row_index, column=relative_col, sticky='ew', padx=5, pady=Config.ROW_PAD_Y)
        widgets['relative_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._enable_relative_edit(mid))

        # Notes
        notes_preview = state.notes[:15] + "..." if len(state.notes) > 15 else state.notes
        widgets['notes_var'] = tk.StringVar(value=notes_preview)
        widgets['notes_entry'] = tk.Entry(
            self._motors_container, textvariable=widgets['notes_var'],
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground='white', cursor='hand2'
        )
        widgets['notes_entry'].grid(row=row_index, column=notes_col, sticky='ew', padx=5, pady=Config.ROW_PAD_Y)
        widgets['notes_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._on_notes_click(mid))

        # Store references to widgets for cleanup
        visible_widgets.extend([
            widgets['absolute_entry'], widgets['relative_entry'], widgets['notes_entry']
        ])
        widgets['all_widgets'] = visible_widgets

        return widgets

    def _rebuild_motor_rows(self):
        """Rebuild all motor row widgets."""
        for child in self._motors_container.winfo_children():
            child.destroy()
        self._motor_widgets.clear()

        min_widths = Config.STEP_COL_MIN_WIDTHS if self._is_step_mode() else Config.COL_MIN_WIDTHS
        for i in range(max(len(Config.COL_MIN_WIDTHS), len(Config.STEP_COL_MIN_WIDTHS))):
            self._motors_container.grid_columnconfigure(i, weight=0, minsize=0)
        for i, min_w in enumerate(min_widths):
            self._motors_container.grid_columnconfigure(i, weight=1, minsize=min_w)

        self._build_header_row()

        # Recreate rows starting from index 1 (0 is header)
        for i, state in enumerate(self._motor_states):
            widgets = self._create_motor_row(state, row_index=i + 1)
            self._motor_widgets.append(widgets)

        self._style_widget_tree(self._motors_container)
        self._fit_window_to_content()

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _is_step_mode(self) -> bool:
        """Return whether the GUI is currently in step mode."""
        return bool(self._mode_var and self._mode_var.get() == Config.MODE_STEP)

    def _on_mode_change(self):
        """Handle switching between target and step modes."""
        self._update_mode_visibility()
        self._rebuild_motor_rows()
        self._fit_window_to_content()

    def _update_mode_visibility(self):
        """Show controls for the selected movement mode."""
        if not (self._target_controls_frame and self._step_controls_frame and self._step_settings_frame):
            return

        if self._is_step_mode():
            self._target_controls_frame.pack_forget()
            self._pack_above_motor_table(self._step_settings_frame, pady=(0, 10))
            self._pack_above_motor_table(self._step_controls_frame, pady=(0, 15))
        else:
            self._step_settings_frame.pack_forget()
            self._step_controls_frame.pack_forget()
            self._pack_above_motor_table(self._target_controls_frame, pady=(0, 15))

    def _pack_above_motor_table(self, frame: tk.Frame, **pack_options):
        """Pack a control frame before the motor table, even after mode toggles."""
        if self._motors_container:
            frame.pack(before=self._motors_container, **pack_options)
        else:
            frame.pack(**pack_options)

    def _bind_numeric_stepper(self, entry: tk.Entry, variable: tk.StringVar,
                              increment: float, decimals: int,
                              min_value=None, max_value=None,
                              on_change: Callable = None):
        """Bind Up/Down keys to increment a numeric entry."""
        entry.bind(
            '<Up>',
            lambda event: self._adjust_numeric_entry(
                variable, increment, decimals, min_value, max_value, on_change
            )
        )
        entry.bind(
            '<Down>',
            lambda event: self._adjust_numeric_entry(
                variable, -increment, decimals, min_value, max_value, on_change
            )
        )

    def _adjust_numeric_entry(self, variable: tk.StringVar, delta: float,
                              decimals: int, min_value=None, max_value=None,
                              on_change: Callable = None):
        """Adjust a numeric entry value and keep focus in the edited field."""
        try:
            value = float(variable.get())
        except ValueError:
            value = 0.0

        value += delta

        if callable(min_value):
            min_value = min_value()
        if callable(max_value):
            max_value = max_value()

        if min_value is not None and value < min_value:
            value = min_value
        if max_value is not None and value > max_value:
            value = max_value

        variable.set(f"{value:.{decimals}f}")
        if on_change:
            on_change()
        return "break"

    def _bind_vertical_tab(self, entry: tk.Entry, motor_id: int, widget_key: str):
        """Bind Tab and Shift-Tab to move vertically within a table column."""
        entry.bind(
            '<Tab>',
            lambda event: self._focus_vertical_entry(motor_id, widget_key, direction=1)
        )
        entry.bind(
            '<Shift-Tab>',
            lambda event: self._focus_vertical_entry(motor_id, widget_key, direction=-1)
        )

    def _focus_vertical_entry(self, motor_id: int, widget_key: str, direction: int):
        """Move focus vertically, then horizontally at column boundaries."""
        next_id = motor_id + direction

        while 1 <= next_id <= len(self._motor_widgets):
            widgets = self._get_widgets(next_id)
            if widgets and widget_key in widgets:
                widgets[widget_key].focus_set()
                widgets[widget_key].select_range(0, tk.END)
                return "break"
            next_id += direction

        columns = ['speed_entry', 'target_entry']
        try:
            column_index = columns.index(widget_key)
        except ValueError:
            return "break"

        next_column_index = column_index + direction
        if next_column_index < 0 or next_column_index >= len(columns):
            next_column_index %= len(columns)

        next_widget_key = columns[next_column_index]
        next_id = 1 if direction > 0 else len(self._motor_widgets)

        while 1 <= next_id <= len(self._motor_widgets):
            widgets = self._get_widgets(next_id)
            if widgets and next_widget_key in widgets:
                widgets[next_widget_key].focus_set()
                widgets[next_widget_key].select_range(0, tk.END)
                return "break"
            next_id += direction

        return "break"

    def _bind_step_mode_shortcuts(self):
        """Bind keyboard shortcuts used in step mode."""
        self._root.bind_all('<KeyPress>', self._on_keypress)
        self._root.bind_all('<KeyRelease>', self._on_keyrelease)

    def _on_keypress(self, event):
        """Handle step-mode keyboard shortcuts."""
        if not self._is_step_mode():
            return None

        widget = self._event_widget(event)
        if widget and widget.winfo_toplevel() != self._root:
            return None

        if widget and self._is_editable_text_widget(widget):
            return None

        if self._is_step_down_key(event):
            return self._handle_step_keypress("down", direction=1)

        if self._is_step_up_key(event):
            return self._handle_step_keypress("up", direction=-1)

        digit = self._shortcut_digit(event)
        if digit is None:
            return None

        self._select_only_motor(digit)
        return "break"

    def _on_keyrelease(self, event):
        """Stop step-mode jog motion when a step shortcut key is released."""
        if not self._is_step_mode():
            return None

        widget = self._event_widget(event)
        if widget and widget.winfo_toplevel() != self._root:
            return None

        if widget and self._is_editable_text_widget(widget):
            return None

        if self._is_step_down_key(event):
            return self._handle_step_keyrelease("down")

        if self._is_step_up_key(event):
            return self._handle_step_keyrelease("up")

        return None

    def _handle_step_keypress(self, key_name: str, direction: int):
        """Handle initial step shortcut press and ignore key-repeat repeats."""
        self._cancel_step_key_release_job()

        if self._active_step_key == key_name:
            return "break"

        self._reset_step_key_state()
        self._active_step_key = key_name
        self._active_step_direction = direction
        self._on_step(direction=direction)
        self._step_key_hold_job = self._root.after(
            Config.STEP_JOG_HOLD_DELAY_MS, self._start_step_jog
        )
        return "break"

    def _handle_step_keyrelease(self, key_name: str):
        """Handle release of a step shortcut key."""
        if self._active_step_key != key_name:
            return "break"

        self._cancel_step_key_release_job()
        self._step_key_release_job = self._root.after(
            Config.STEP_KEY_RELEASE_DEBOUNCE_MS, self._finish_step_keyrelease
        )
        return "break"

    def _finish_step_keyrelease(self):
        """Finish a step shortcut release after key-repeat debounce."""
        self._step_key_release_job = None
        self._cancel_step_key_hold_job()
        if self._step_key_is_jogging:
            self._pause_selected_step_motors()
        self._active_step_key = None
        self._active_step_direction = 0
        self._step_key_is_jogging = False

    def _start_step_jog(self):
        """Convert a held step shortcut into continuous jog motion."""
        self._step_key_hold_job = None
        if not self._active_step_key or self._active_step_direction == 0:
            return

        if not self._serial_manager.is_connected:
            return

        selected = self._selected_states()
        if not selected or not self._validate_absolute_edits_closed(selected):
            return

        step_settings = self._parse_step_settings()
        if not step_settings:
            return

        speed, _ = step_settings
        target = Config.MAX_TARGET_POS if self._active_step_direction > 0 else Config.MIN_TARGET_POS

        for state in selected:
            state.speed = speed
            state.target = target
            self._step_targets.pop(state.motor_id, None)
            cmd = SerialProtocol.cmd_run(state.motor_id, speed, target, state.absolute)
            self._serial_manager.send(cmd)
            state.is_running = True

        self._step_key_is_jogging = True
        self._focus_main_window()

    def _cancel_step_key_release_job(self):
        """Cancel a pending jog stop."""
        if self._step_key_release_job is not None:
            self._root.after_cancel(self._step_key_release_job)
            self._step_key_release_job = None

    def _cancel_step_key_hold_job(self):
        """Cancel a pending transition from single-step to jog."""
        if self._step_key_hold_job is not None:
            self._root.after_cancel(self._step_key_hold_job)
            self._step_key_hold_job = None

    def _is_step_down_key(self, event) -> bool:
        """Return True for keys that trigger Step Down."""
        return event.keysym in ('KP_Add', 'plus', 'equal')

    def _is_step_up_key(self, event) -> bool:
        """Return True for keys that trigger Step Up."""
        return event.keysym in ('KP_Subtract', 'minus', 'underscore')

    def _pause_selected_step_motors(self):
        """Pause selected motors after releasing a step shortcut."""
        if not self._serial_manager.is_connected:
            return

        for state in self._selected_states():
            if state.is_running:
                self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                state.is_running = False
            self._step_targets.pop(state.motor_id, None)

        self._focus_main_window()

    def _reset_step_key_state(self):
        """Clear step shortcut repeat state."""
        self._cancel_step_key_release_job()
        self._cancel_step_key_hold_job()
        self._active_step_key = None
        self._active_step_direction = 0
        self._step_key_is_jogging = False

    def _event_widget(self, event):
        """Return the Tk widget object for an event."""
        if isinstance(event.widget, str):
            try:
                return self._root.nametowidget(event.widget)
            except KeyError:
                return None
        return event.widget

    def _focus_main_window(self):
        """Return keyboard focus to the main application window."""
        self._root.after_idle(self._root.focus_force)

    def _is_editable_text_widget(self, widget) -> bool:
        """Return True when a keypress should be treated as text input."""
        if isinstance(widget, tk.Text):
            return True
        if isinstance(widget, tk.Entry):
            try:
                return widget.cget('state') != 'readonly'
            except tk.TclError:
                return True
        return False

    def _shortcut_digit(self, event) -> Optional[int]:
        """Return a motor shortcut digit for normal or keypad number keys."""
        key = event.keysym
        if key.startswith('KP_'):
            key = key[3:]
        if key.isdigit():
            digit = int(key)
            if 1 <= digit <= Config.MAX_MOTORS:
                return digit
        return None

    def _select_only_motor(self, motor_id: int):
        """Select one motor and deselect all others."""
        if motor_id < 1 or motor_id > len(self._motor_states):
            return

        for state in self._motor_states:
            state.is_selected = (state.motor_id == motor_id)
            widgets = self._get_widgets(state.motor_id)
            if widgets:
                widgets['selected_var'].set(state.is_selected)

    def _enable_step_setting_edit(self, setting: str):
        """Enable editing for a step setting field."""
        entry = self._step_speed_entry if setting == 'speed' else self._step_size_entry
        if not entry:
            return

        entry.config(state='normal', background=Config.COLOR_ENTRY_BG)
        entry.focus_set()
        entry.select_range(0, tk.END)
        entry.bind('<Return>', lambda e, s=setting: self._confirm_step_setting_edit(s))
        entry.bind('<FocusOut>', lambda e, s=setting: self._confirm_step_setting_edit(s))

    def _confirm_step_setting_edit(self, setting: str):
        """Validate and lock a step setting field after editing."""
        entry = self._step_speed_entry if setting == 'speed' else self._step_size_entry
        variable = self._step_speed_var if setting == 'speed' else self._step_size_var
        if not entry or not variable or entry['state'] == 'readonly':
            return

        try:
            value = float(variable.get())
            if setting == 'speed':
                value = min(max(value, self._min_speed), self._max_speed)
            elif value < Config.STEP_SIZE_KEY_INCREMENT:
                value = Config.STEP_SIZE_KEY_INCREMENT
        except ValueError:
            value = self._min_speed if setting == 'speed' else Config.STEP_SIZE_KEY_INCREMENT

        variable.set(f"{value:.3f}")
        entry.config(state='readonly', readonlybackground=Config.COLOR_ENTRY_BG)
        self._root.focus_set()

    def _get_widgets(self, motor_id: int) -> Optional[dict]:
        """Get widget dictionary for motor by ID."""
        idx = motor_id - 1
        if 0 <= idx < len(self._motor_widgets):
            return self._motor_widgets[idx]
        return None

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
            widgets = self._get_widgets(state.motor_id)
            if widgets and widgets['absolute_entry']['state'] != 'readonly':
                self._safe_popup(
                    messagebox.showwarning, "Edit In Progress",
                    f"Motor {state.motor_id} absolute position is being edited. "
                    f"Press enter to confirm edit before moving."
                )
                return False
        return True

    def _parse_step_settings(self) -> Optional[tuple]:
        """Validate and return step speed and size in mm."""
        try:
            speed = float(self._step_speed_var.get())
        except (AttributeError, ValueError):
            self._safe_popup(messagebox.showwarning, "Invalid Input", "Invalid step speed input.")
            return None

        try:
            step_size = float(self._step_size_var.get())
        except (AttributeError, ValueError):
            self._safe_popup(messagebox.showwarning, "Invalid Input", "Invalid step size input.")
            return None

        if not (self._min_speed <= speed <= self._max_speed):
            self._safe_popup(
                messagebox.showwarning, "Invalid Input",
                f"Step speed must be between {self._min_speed}mm/s and {self._max_speed}mm/s."
            )
            return None

        if step_size <= 0:
            self._safe_popup(messagebox.showwarning, "Invalid Input", "Step size must be greater than 0mm.")
            return None

        step_size = round(step_size, 3)
        self._step_size_var.set(format_mm(step_size))
        return speed, step_size

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
        self._style_widget_tree(self._settings_window)

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
            ["--initial-path", self._saved_anatomy_path],
            cwd=Path(__file__).parent,
        )

    def _on_anatomy_close(self):
        """Handle anatomy viewer close."""
        if self._anatomy_window:
            self._anatomy_window.close()
        self._anatomy_window = None

    def _on_anatomy_path_change(self, path: str):
        """Save the most recently loaded anatomy path."""
        self._saved_anatomy_path = path
        self._refresh_trajectory_buttons()
        self._save_state()

    def _has_loaded_anatomy(self) -> bool:
        """Return whether an anatomy file/folder is available for trajectory planning."""
        return bool(self._saved_anatomy_path and Path(self._saved_anatomy_path).exists())

    def _refresh_trajectory_buttons(self):
        """Enable trajectory buttons only when anatomy data is available."""
        state = tk.NORMAL if self._has_loaded_anatomy() else tk.DISABLED
        if self._trajectory_summary_btn:
            self._trajectory_summary_btn.configure(state=state)
        for widgets in self._motor_widgets:
            button = widgets.get('trajectory_button')
            if button:
                button.configure(state=state)

    def _schedule_viewer_process_poll(self):
        """Poll subprocess viewer events without blocking the GUI."""
        self._poll_viewer_processes()
        self._root.after(100, self._schedule_viewer_process_poll)

    def _poll_viewer_processes(self):
        """Handle pending messages from anatomy/trajectory subprocesses."""
        if self._anatomy_window:
            for event in self._anatomy_window.poll_events():
                event_type = event.get("type")
                if event_type == "path_change":
                    self._on_anatomy_path_change(str(event.get("path", "")))
                elif event_type in ("closed", "exited"):
                    self._anatomy_window = None
                elif event_type == "error":
                    logger.error("Anatomy viewer error: %s", event.get("message"))

        for motor_id, handle in list(self._trajectory_windows.items()):
            for event in handle.poll_events():
                event_type = event.get("type")
                if event_type == "save":
                    trajectory = TrajectoryState.from_dict(event.get("trajectory", {}))
                    self._on_trajectory_save(motor_id, trajectory)
                elif event_type in ("closed", "exited"):
                    self._trajectory_windows.pop(motor_id, None)
                elif event_type == "error":
                    logger.error(
                        "Trajectory viewer error for motor %s: %s",
                        motor_id, event.get("message")
                    )

        if self._trajectory_summary_window:
            for event in self._trajectory_summary_window.poll_events():
                event_type = event.get("type")
                if event_type in ("closed", "exited"):
                    self._trajectory_summary_window = None
                elif event_type == "error":
                    logger.error("Trajectory summary error: %s", event.get("message"))

    def _safe_popup(self, popup_func: Callable, title: str, message: str = None, **kwargs):
        """
        Show a popup dialog with automatic motor pause and heartbeat suspension.
        
        Suspends heartbeat checking and pauses all running motors before showing 
        the dialog to prevent connection timeout during blocking dialogs.
        """
        paused_any = False

        # Suspend heartbeat checking during the blocking dialog
        self._serial_manager.heartbeat_suspended = True

        try:
            # Pause all running motors
            if self._serial_manager.is_connected:
                for state in self._motor_states:
                    if state.is_running:
                        self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                        state.is_running = False
                        paused_any = True

            # Add safety note to message if motors were paused
            if paused_any and message:
                message += "\n\n(Safety: Running motors have been automatically paused)"

            # Show the popup
            if message:
                return popup_func(title, message, **kwargs)
            else:
                return popup_func(title=title, **kwargs)
        
        finally:
            # Resume heartbeat checking and reset the timer
            self._serial_manager.heartbeat_suspended = False
            if self._serial_manager.is_connected:
                self._serial_manager.last_heartbeat_received = time.time()

    # -------------------------------------------------------------------------
    # Event Handlers - Selection and Input
    # -------------------------------------------------------------------------

    def _on_selection_change(self, motor_id: int):
        """Handle motor selection checkbox change."""
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
        if state and widgets:
            state.is_selected = widgets['selected_var'].get()

    def _on_speed_change(self, motor_id: int):
        """Handle speed input change."""
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
        if state and widgets:
            try:
                state.speed = float(widgets['speed_var'].get())
            except ValueError:
                pass

    def _on_target_change(self, motor_id: int):
        """Handle target input change."""
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
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
        widgets = self._get_widgets(motor_id)
        if state and widgets:
            widgets['target_var'].set(format_mm(state.target) if state.target else "")

    def _enable_absolute_edit(self, motor_id: int):
        """Enable editing of absolute position field."""
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
        if widgets and state:
            # If motor is running, pause it before editing
            if state.is_running:
                if self._serial_manager.is_connected:
                    self._serial_manager.send(SerialProtocol.cmd_pause(motor_id))
                state.is_running = False

            entry = widgets['absolute_entry']
            entry.config(state='normal', background='white')
            entry.focus_set()
            entry.select_range(0, tk.END)
            entry.bind('<Return>', lambda e, mid=motor_id: self._on_absolute_confirm(mid))
            entry.bind('<FocusOut>', lambda e, mid=motor_id: self._on_absolute_confirm(mid))

    def _on_absolute_confirm(self, motor_id: int):
        """Handle confirmation of absolute position edit."""
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
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

        entry.config(state='readonly', readonlybackground='lightgray')

    def _enable_relative_edit(self, motor_id: int):
        """Enable editing of relative position field."""
        widgets = self._get_widgets(motor_id)
        if widgets:
            entry = widgets['relative_entry']
            entry.config(state='normal', background='white')
            entry.focus_set()
            entry.select_range(0, tk.END)
            entry.bind('<Return>', lambda e, mid=motor_id: self._on_relative_confirm(mid))
            entry.bind('<FocusOut>', lambda e, mid=motor_id: self._on_relative_confirm(mid))

    def _on_relative_confirm(self, motor_id: int):
        """Handle confirmation of relative position edit."""
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
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

        entry.config(state='readonly', readonlybackground='lightgray')

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
        widgets = self._get_widgets(motor_id)
        if state and widgets:
            state.notes = notes
            preview = notes[:15] + "..." if len(notes) > 15 else notes
            widgets['notes_var'].set(preview)

        if motor_id in self._notes_windows:
            del self._notes_windows[motor_id]

    def _on_trajectory_click(self, motor_id: int):
        """Open the trajectory viewer for one motor."""
        state = self._get_state(motor_id)
        if not state:
            return

        if motor_id in self._trajectory_windows:
            handle = self._trajectory_windows[motor_id]
            if handle.is_running():
                handle.send_focus()
                return
            self._trajectory_windows.pop(motor_id, None)

        if not self._saved_anatomy_path:
            messagebox.showinfo(
                "No Anatomy Loaded",
                "Open the Anatomy Viewer and load an MRI before defining trajectories."
            )
            return

        handle = JsonViewerProcess(
            "monkeydrive.trajectory_process",
            [
                "--motor-id", str(motor_id),
                "--motor-position", f"{state.absolute:g}",
                "--anatomy-path", self._saved_anatomy_path,
                "--trajectory-json", json.dumps(state.trajectory.to_dict()),
            ],
            cwd=Path(__file__).parent,
        )
        self._trajectory_windows[motor_id] = handle

    def _on_trajectory_summary(self):
        """Open the trajectory summary viewer."""
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
            "monkeydrive.trajectory_summary_process",
            [
                "--anatomy-path", self._saved_anatomy_path,
                "--motor-states-json", json.dumps([state.to_dict() for state in self._motor_states]),
            ],
            cwd=Path(__file__).parent,
        )
        self._trajectory_summary_window = handle

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
        widgets = self._get_widgets(state.motor_id)
        if widgets and 'target_var' in widgets:
            widgets['target_var'].set(format_mm(state.target) if state.target else "")

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
        self._port_combo['values'] = ports

    def _on_connect(self):
        """Handle connect/disconnect button click."""
        port = self._port_var.get()
        if not port:
            messagebox.showwarning("No Port", "Please select a port.")
            return

        if self._serial_manager.is_connected:
            # Pause all running motors before disconnecting
            self._reset_step_key_state()
            for state in self._motor_states:
                if state.is_running:
                    self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                    state.is_running = False
            self._serial_manager.disconnect()
            self._step_targets.clear()
            self._connect_btn.config(text="Connect")
            self._focus_main_window()
        else:
            if self._serial_manager.connect(port):
                self._send_settings_to_controller()
                self._connect_btn.config(text="Disconnect")
                messagebox.showinfo("Connected", f"Connected to {port}")
                self._focus_main_window()
            else:
                messagebox.showerror("Connection Failed", f"Could not connect to {port}")
                self._focus_main_window()

    def _on_connection_lost(self):
        """Handle connection lost callback from serial manager."""
        self._reset_step_key_state()
        for state in self._motor_states:
            state.is_running = False
        self._step_targets.clear()
        self._root.after(0, self._show_connection_lost_dialog)

    def _show_connection_lost_dialog(self):
        """Show connection lost dialog on main thread."""
        self._connect_btn.config(text="Connect")
        messagebox.showwarning(
            "Connection Lost",
            "Connection to console was lost. Any running motors have been paused. "
            "Check physical connection and attempt to reconnect."
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
        state = MotorState(motor_id=motor_id)
        self._motor_states.append(state)

        self._rebuild_motor_rows()

    def _remove_motor(self):
        """Remove the last motor row."""
        if len(self._motor_states) <= 1:
            return

        last_state = self._motor_states[-1]
        if last_state.is_running:
            self._serial_manager.send(SerialProtocol.cmd_pause(last_state.motor_id))
        self._step_targets.pop(last_state.motor_id, None)

        self._motor_states.pop()
        self._rebuild_motor_rows()

    # -------------------------------------------------------------------------
    # Control Actions
    # -------------------------------------------------------------------------

    def _on_run(self):
        """Handle Run button click."""
        if not self._serial_manager.is_connected:
            self._safe_popup(messagebox.showwarning, "Not Connected", "Console not connected.")
            return

        selected = self._selected_states()
        if not selected:
            self._safe_popup(messagebox.showinfo, "No Selection", "Select at least one motor.")
            return

        if not self._validate_absolute_edits_closed(selected):
            return

        # Validation pass
        for state in selected:
            widgets = self._get_widgets(state.motor_id)
            if not widgets:
                continue

            # Validate Speed Input
            try:
                s_val = float(widgets['speed_var'].get())
                state.speed = s_val
            except ValueError:
                self._safe_popup(
                    messagebox.showwarning, "Invalid Input",
                    f"Invalid speed input for Motor {state.motor_id}."
                )
                return

            # Validate Target Input
            try:
                t_val = float(widgets['target_var'].get())
                state.target = round(t_val, 3)
                widgets['target_var'].set(format_mm(state.target) if state.target else "")
            except ValueError:
                self._safe_popup(
                    messagebox.showwarning, "Invalid Input",
                    f"Invalid target input for Motor {state.motor_id}."
                )
                return

            # Range checks
            if not (self._min_speed <= state.speed <= self._max_speed):
                self._safe_popup(
                    messagebox.showwarning, "Invalid Input",
                    f"Speed must be between {self._min_speed}mm/s and "
                    f"{self._max_speed}mm/s. See Motor {state.motor_id}."
                )
                return

            if not (Config.MIN_TARGET_POS <= state.target <= Config.MAX_TARGET_POS):
                self._safe_popup(
                    messagebox.showwarning, "Invalid Input",
                    f"Target must be between {Config.MIN_TARGET_POS}mm and "
                    f"{Config.MAX_TARGET_POS}mm. See Motor {state.motor_id}."
                )
                return

            if not (Config.MIN_ABSOLUTE_POS <= state.absolute <= Config.MAX_ABSOLUTE_POS):
                self._safe_popup(
                    messagebox.showwarning, "Invalid Input",
                    f"Absolute position must be between {Config.MIN_ABSOLUTE_POS}mm and "
                    f"{Config.MAX_ABSOLUTE_POS}mm. See Motor {state.motor_id}."
                )
                return

        # Execution pass
        for state in selected:
            cmd = SerialProtocol.cmd_run(
                state.motor_id, state.speed, state.target, state.absolute
            )
            self._serial_manager.send(cmd)
            state.is_running = True

    def _on_step(self, direction: int):
        """Move selected motors by one global step."""
        if not self._serial_manager.is_connected:
            self._safe_popup(messagebox.showwarning, "Not Connected", "Console not connected.")
            return

        selected = self._selected_states()
        if not selected:
            self._safe_popup(messagebox.showinfo, "No Selection", "Select at least one motor.")
            return

        if not self._validate_absolute_edits_closed(selected):
            return

        step_settings = self._parse_step_settings()
        if not step_settings:
            return

        speed, step_size = step_settings
        target_updates = []

        for state in selected:
            target = round(state.absolute + (direction * step_size), 3)
            if not (Config.MIN_TARGET_POS <= target <= Config.MAX_TARGET_POS):
                self._safe_popup(
                    messagebox.showwarning, "Invalid Step",
                    f"Motor {state.motor_id} step target would be {target:.3f}mm, "
                    f"outside the {Config.MIN_TARGET_POS}-{Config.MAX_TARGET_POS}mm range."
                )
                return
            target_updates.append((state, target))

        for state, target in target_updates:
            state.speed = speed
            state.target = target
            self._step_targets.pop(state.motor_id, None)
            cmd = SerialProtocol.cmd_run(state.motor_id, speed, target, state.absolute)
            self._serial_manager.send(cmd)
            state.is_running = True

        self._focus_main_window()

    def _on_pause(self):
        """Handle Pause button click."""
        self._reset_step_key_state()
        selected = [s for s in self._motor_states if s.is_selected and s.is_running]
        for state in selected:
            self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
            state.is_running = False
            self._step_targets.pop(state.motor_id, None)

    def _on_zero(self):
        """Zero relative position for selected motors."""
        selected = [s for s in self._motor_states if s.is_selected]

        for state in selected:
            state.relative_zero_point = state.absolute
            state.relative = 0.0

            widgets = self._get_widgets(state.motor_id)
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

        # Suspend heartbeat checking during the blocking file dialog
        self._serial_manager.heartbeat_suspended = True

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

        finally:
            # Resume heartbeat checking and reset the timer
            self._serial_manager.heartbeat_suspended = False
            if self._serial_manager.is_connected:
                self._serial_manager.last_heartbeat_received = time.time()

    def _on_clear(self):
        """Clear selected motors - pause if running, then wipe values."""
        selected = [s for s in self._motor_states if s.is_selected]

        if not selected:
            return

        # Pause selected motors that are running
        for state in selected:
            if state.is_running:
                self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
            self._step_targets.pop(state.motor_id, None)

        # Clear values for selected motors
        for state in selected:
            state.speed = 0.0
            state.target = 0.0
            state.absolute = 0.0
            state.relative = 0.0
            state.relative_zero_point = 0.0
            state.notes = ""
            state.is_running = False

            widgets = self._get_widgets(state.motor_id)
            if widgets:
                if 'speed_var' in widgets:
                    widgets['speed_var'].set("")
                if 'target_var' in widgets:
                    widgets['target_var'].set("")
                widgets['absolute_var'].set("0.000")
                widgets['relative_var'].set("0.000")
                widgets['notes_var'].set("")

        self._root.update_idletasks()

    # -------------------------------------------------------------------------
    # Position Updates
    # -------------------------------------------------------------------------

    def _on_position_update(self, motor_id: int, absolute: float):
        """Handle position update from serial manager."""
        state = self._get_state(motor_id)
        if state:
            state.absolute = round(absolute, 3)
            state.relative = round(state.absolute - state.relative_zero_point, 3)
            if not state.is_running:
                self._step_targets.pop(motor_id, None)

        self._root.after(0, lambda: self._update_position_display(motor_id, absolute))

    def _update_position_display(self, motor_id: int, absolute: float):
        """Update position display widgets on main thread."""
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
        if state and widgets:
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
            data = {
                'motor_states': [s.to_dict() for s in self._motor_states],
                'port': self._port_var.get() if self._port_var else "",
                'mode': self._mode_var.get() if self._mode_var else self._saved_mode,
                'step_speed': self._step_speed_var.get() if self._step_speed_var else f"{self._saved_step_speed:g}",
                'step_size': self._step_size_var.get() if self._step_size_var else format_mm(self._saved_step_size),
                'min_speed': self._min_speed,
                'max_speed': self._max_speed,
                'backlash': round(self._backlash, 3),
                'anatomy_path': self._saved_anatomy_path,
            }
            with open(self._get_data_path(), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error("Failed to save state: %s", e)

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

                for state in self._motor_states:
                    state.is_running = False
                    state.target = round(state.target, 3)
                    state.absolute = round(state.absolute, 3)
                    state.relative = round(state.relative, 3)
                    state.relative_zero_point = round(state.relative_zero_point, 3)

                mode = data.get('mode', Config.MODE_TARGET)
                self._saved_mode = mode if mode in (Config.MODE_TARGET, Config.MODE_STEP) else Config.MODE_TARGET
                self._saved_step_speed = float(data.get('step_speed', Config.DEFAULT_STEP_SPEED))
                self._saved_step_size = round(float(data.get('step_size', Config.DEFAULT_STEP_SIZE)), 3)
                self._min_speed = float(data.get('min_speed', Config.MIN_SPEED))
                self._max_speed = float(data.get('max_speed', Config.MAX_SPEED))
                self._backlash = round(float(data.get('backlash', Config.DEFAULT_BACKLASH_MM)), 3)
                self._saved_anatomy_path = str(data.get('anatomy_path', ""))

                if self._min_speed <= 0 or self._max_speed < self._min_speed or self._backlash < 0:
                    raise ValueError("Invalid persisted settings")
        except (IOError, json.JSONDecodeError) as e:
            logger.error("Failed to load state: %s", e)
            self._motor_states = []
        except ValueError as e:
            logger.error("Failed to load step settings: %s", e)
            self._saved_mode = Config.MODE_TARGET
            self._saved_step_speed = Config.DEFAULT_STEP_SPEED
            self._saved_step_size = Config.DEFAULT_STEP_SIZE
            self._min_speed = Config.MIN_SPEED
            self._max_speed = Config.MAX_SPEED
            self._backlash = Config.DEFAULT_BACKLASH_MM
            self._saved_anatomy_path = ""

    def _schedule_auto_save(self):
        """Schedule periodic auto-save of state."""
        self._save_state()
        self._root.after(int(Config.AUTO_SAVE_INTERVAL * 1000), self._schedule_auto_save)

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def _on_close(self):
        """Handle window close event."""
        if self._serial_manager.is_connected:
            self._serial_manager.send(SerialProtocol.cmd_pause_all())

        self._serial_manager.disconnect()
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
