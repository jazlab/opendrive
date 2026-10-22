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
from tkinter import ttk, messagebox, filedialog, font as tkfont

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


from monkeydrive.config import Config
from monkeydrive.console_theme import ConsoleTheme as Theme
from monkeydrive.dialogs import InstructionsWindow, NotesWindow, ProgramLoadWindow, SettingsWindow
from monkeydrive.models import InstructionStep, MotorState, TrajectoryState
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
        self._probe_summary_window: Optional[JsonViewerProcess] = None
        self._saved_mode: str = Config.MODE_TARGET
        self._saved_port: str = ""
        self._saved_step_speed: float = Config.DEFAULT_STEP_SPEED
        self._saved_step_size: float = Config.DEFAULT_STEP_SIZE
        self._saved_anatomy_path: str = ""
        self._anatomy_orientation_actual: str = ""
        self._anatomy_orientation_scanner: str = ""
        self._recent_programs: list[str] = []
        self._min_speed: float = Config.MIN_SPEED
        self._max_speed: float = Config.MAX_SPEED
        self._backlash: float = Config.DEFAULT_BACKLASH_MM
        self._step_targets: dict = {}
        self._active_step_key: Optional[str] = None
        self._active_step_direction: int = 0
        self._step_key_is_jogging: bool = False
        self._step_key_hold_job = None
        self._step_key_release_job = None
        self._closing = False

        # Serial manager
        self._serial_manager = SerialManager(
            on_position_update=self._on_position_update,
            on_connection_lost=self._on_connection_lost,
            on_connected=self._on_connected,
        )

        # GUI variables (initialized in _build_ui)
        self._port_var: Optional[tk.StringVar] = None
        self._port_combo: Optional[ttk.Combobox] = None
        self._connect_btn: Optional[tk.Button] = None
        self._mode_var: Optional[tk.StringVar] = None
        self._mode_buttons: dict[str, tk.Button] = {}
        self._step_speed_var: Optional[tk.StringVar] = None
        self._step_size_var: Optional[tk.StringVar] = None
        self._step_speed_entry: Optional[tk.Entry] = None
        self._step_size_entry: Optional[tk.Entry] = None
        self._target_controls_frame: Optional[tk.Frame] = None
        self._step_controls_frame: Optional[tk.Frame] = None
        self._step_settings_frame: Optional[tk.Frame] = None
        self._program_controls_frame: Optional[tk.Frame] = None
        self._settings_window: Optional[SettingsWindow] = None
        self._anatomy_window: Optional[JsonViewerProcess] = None
        self._trajectory_summary_btn: Optional[tk.Button] = None
        self._motors_container: Optional[tk.Frame] = None

        # Load saved state
        self._load_state()

        # Build UI
        self._build_ui()
        self._bind_keyboard_shortcuts()

        # Ensure at least one motor
        if len(self._motor_states) == 0:
            self._add_motor()
        else:
            self._rebuild_motor_rows()

        self._style_widget_tree()

        # Keep the console usable with platform font and display scaling.
        self._fit_window_to_content()
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

    def _configure_light_theme(self):
        """Force readable colors on older macOS Tk builds."""
        for option, value in (
            ("*Background", Theme.COLOR_BG),
            ("*Foreground", Theme.COLOR_FG),
            ("*Entry.Background", Theme.COLOR_ENTRY_BG),
            ("*Entry.Foreground", Theme.COLOR_FG),
            ("*Text.Background", Theme.COLOR_ENTRY_BG),
            ("*Text.Foreground", Theme.COLOR_FG),
            ("*Button.Background", Theme.COLOR_ENTRY_BG),
            ("*Button.Foreground", Theme.COLOR_FG),
        ):
            self._root.option_add(option, value)

        self._root.tk_setPalette(
            background=Theme.COLOR_BG,
            foreground=Theme.COLOR_FG,
            activeBackground=Theme.COLOR_ACTIVE_BG,
            activeForeground=Theme.COLOR_FG,
            highlightColor=Theme.COLOR_SELECT_BG,
            selectBackground=Theme.COLOR_SELECT_BG,
            selectForeground=Theme.COLOR_ENTRY_BG,
        )
        self._root.configure(background=Theme.COLOR_BG)

        self._console_font = tkfont.nametofont("TkDefaultFont").copy()
        self._console_font.configure(size=11)
        self._readout_font = self._console_font.copy()
        self._readout_font.configure(size=13, weight="bold")
        self._root.option_add("*Font", self._console_font)
        style = ttk.Style(self._root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TCombobox",
            font=self._console_font,
            fieldbackground=Theme.COLOR_ENTRY_BG,
            background=Theme.COLOR_ENTRY_BG,
            foreground=Theme.COLOR_FG,
            selectbackground=Theme.COLOR_SELECT_BG,
            selectforeground=Theme.COLOR_ENTRY_BG,
        )

        style.configure("Console.TButton", font=self._console_font, padding=(10, 6), background="#ffffff",
                        foreground=Theme.COLOR_FG, bordercolor="#dce2e9", relief="flat")
        style.map("Console.TButton", background=[("active", "#eaf1fa")],
                  foreground=[("disabled", "#929ba7")])
        style.configure("Primary.Console.TButton", background="#225da8", foreground="white")
        style.map("Primary.Console.TButton", background=[("active", "#194b89")],
                  foreground=[("disabled", "#b7c6da"), ("!disabled", "white")])
        style.configure("Pause.Console.TButton", foreground="#a52d36")
        style.configure("Selected.Console.TButton", background="#eaf1fa", foreground="#225da8")
        style.configure("Console.TLabel", font=self._console_font, background=Theme.COLOR_BG, foreground="#626e7d")
        style.configure("Title.Console.TLabel", font=(self._console_font.actual("family"), 26, "bold"),
                        foreground=Theme.COLOR_FG)

    def _style_widget_tree(self, widget=None):
        """Apply explicit colors to Tk widgets that may ignore the app palette."""
        widget = widget or self._root

        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
                widget.configure(background=Theme.COLOR_BG)
            elif isinstance(widget, tk.Label):
                if getattr(widget, "_monkeydrive_role", "") != "mode_tab":
                    widget.configure(background=Theme.COLOR_BG, foreground=Theme.COLOR_FG)
            elif isinstance(widget, tk.Checkbutton):
                widget.configure(
                    background=Theme.COLOR_BG,
                    foreground=Theme.COLOR_FG,
                    activebackground=Theme.COLOR_ACTIVE_BG,
                    activeforeground=Theme.COLOR_FG,
                    selectcolor=Theme.COLOR_ENTRY_BG,
                )
            elif isinstance(widget, tk.Radiobutton):
                widget.configure(
                    background=Theme.COLOR_BG,
                    foreground=Theme.COLOR_FG,
                    activebackground=Theme.COLOR_ACTIVE_BG,
                    activeforeground=Theme.COLOR_FG,
                    selectcolor=Theme.COLOR_ENTRY_BG,
                )
            elif isinstance(widget, tk.Button):
                role = getattr(widget, "_monkeydrive_role", "")
                if role == "mode":
                    pass
                else:
                    widget.configure(
                        background=Theme.COLOR_ENTRY_BG,
                        foreground=Theme.COLOR_FG,
                        activebackground=Theme.COLOR_ACTIVE_BG,
                        activeforeground=Theme.COLOR_FG,
                        highlightbackground=Theme.COLOR_BG,
                        borderwidth=1,
                    )
            elif isinstance(widget, tk.Entry):
                readonly_bg = widget.cget("readonlybackground")
                widget.configure(
                    background=Theme.COLOR_ENTRY_BG,
                    foreground=Theme.COLOR_FG,
                    insertbackground=Theme.COLOR_FG,
                    readonlybackground=readonly_bg or Theme.COLOR_READONLY_BG,
                    disabledforeground=Theme.COLOR_FG,
                    relief="flat", borderwidth=0, highlightthickness=1,
                    highlightbackground="#dce2e9", highlightcolor="#225da8",
                )
            elif isinstance(widget, tk.Text):
                widget.configure(
                    background=Theme.COLOR_ENTRY_BG,
                    foreground=Theme.COLOR_FG,
                    insertbackground=Theme.COLOR_FG,
                )
        except tk.TclError:
            pass

        for child in widget.winfo_children():
            self._style_widget_tree(child)

        if widget is self._root:
            self._refresh_mode_buttons()
            for state, row in zip(self._motor_states, self._motor_widgets):
                self._apply_row_selection_style(row, state.is_selected)

    def _fit_window_to_content(self):
        """Fit mode contents while preserving space the user has added."""
        self._root.update_idletasks()
        width = max(980, self._root.winfo_reqwidth())
        height = self._root.winfo_reqheight()
        self._root.minsize(width, height)
        self._root.geometry(f"{max(width, self._root.winfo_width())}x{max(height, self._root.winfo_height())}")

    def _build_ui(self):
        main = tk.Frame(self._root, padx=24, pady=20)
        main.pack(fill=tk.BOTH, expand=True)
        header = tk.Frame(main)
        header.pack(fill=tk.X, pady=(0, 22))
        # Override the application-wide *Font option, which takes precedence over ttk styles.
        ttk.Label(header, text="MonkeyDrive", style="Title.Console.TLabel",
                  font=(self._console_font.actual("family"), 26, "bold")).pack(side=tk.LEFT)
        self._build_port_selection(header)
        toolbar = tk.Frame(main)
        toolbar.pack(fill=tk.X, pady=(0, 16))
        self._mode_toolbar = tk.Frame(toolbar)
        self._mode_toolbar.pack(side=tk.LEFT)
        utilities = tk.Frame(toolbar)
        utilities.pack(side=tk.RIGHT)
        self._build_utility_buttons(utilities)
        self._step_options = tk.Frame(main)
        self._step_options.pack(fill=tk.X)
        ttk.Separator(main).pack(fill=tk.X)
        self._motors_container = tk.Frame(main, pady=8)
        self._motors_container.pack(fill=tk.BOTH, expand=True)
        ttk.Separator(main).pack(fill=tk.X, pady=(12, 12))
        footer = tk.Frame(main)
        footer.pack(fill=tk.X)
        self._selection_text = tk.StringVar(value="")
        ttk.Label(footer, textvariable=self._selection_text, style="Console.TLabel").pack(anchor="w", pady=(0, 10))
        self._build_control_buttons(footer)
        ttk.Label(main, text="Positions in mm  /  Double-click a position or note to edit",
                  style="Console.TLabel").pack(anchor="w", pady=(4, 0))

    def _build_port_selection(self, parent):
        connection = tk.Frame(parent)
        connection.pack(side=tk.RIGHT)
        self._connection_text = tk.StringVar(value="Disconnected")
        ttk.Label(connection, textvariable=self._connection_text, style="Console.TLabel").pack(side=tk.LEFT, padx=(0, 12))
        self._port_var = tk.StringVar(value=self._saved_port)
        self._port_combo = ttk.Combobox(connection, textvariable=self._port_var, width=24, state="readonly")
        if self._saved_port:
            self._port_combo["values"] = [self._saved_port]
        self._port_combo.pack(side=tk.LEFT, padx=(0, 8))
        self._port_combo.bind("<Button-1>", self._refresh_ports)
        self._connect_btn = ttk.Button(connection, text="Connect", command=self._on_connect, style="Console.TButton")
        self._connect_btn.pack(side=tk.LEFT)

    def _build_utility_buttons(self, parent):
        for label, command in (("Anatomy", self._on_anatomy),
                               ("Trajectories", self._on_trajectory_summary),
                               ("Probes", self._on_probe_summary),
                               ("Settings", self._on_settings)):
            button = ttk.Button(parent, text=label, command=command, style="Console.TButton")
            button.pack(side=tk.LEFT, padx=(5, 0))
            if label == "Trajectories":
                self._trajectory_summary_btn = button
                button.configure(state=tk.NORMAL if self._has_loaded_anatomy() else tk.DISABLED)

    def _build_control_buttons(self, parent: tk.Frame):
        """Build the main control buttons."""
        mode_frame = self._mode_toolbar

        self._mode_var = tk.StringVar(value=self._saved_mode)
        self._mode_buttons = {}
        for label, value in (
            ("Target", Config.MODE_TARGET),
            ("Step", Config.MODE_STEP),
            ("Program", Config.MODE_PROGRAM),
        ):
            button = ttk.Button(mode_frame, text=label, width=9,
                                command=lambda mode=value: self._select_mode(mode),
                                style="Console.TButton")
            button.pack(side=tk.LEFT)
            self._mode_buttons[value] = button
        self._refresh_mode_buttons()

        self._step_settings_frame = tk.Frame(self._step_options)
        self._step_settings_frame.pack(pady=(0, 10))

        self._step_speed_var = tk.StringVar(value=f"{self._saved_step_speed:g}")
        self._step_size_var = tk.StringVar(value=format_mm(self._saved_step_size))

        tk.Label(self._step_settings_frame, text="Step speed (mm/s):").pack(side=tk.LEFT, padx=(0, 5))
        step_speed_entry = tk.Entry(
            self._step_settings_frame, textvariable=self._step_speed_var,
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground=Theme.COLOR_ENTRY_BG
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
            readonlybackground=Theme.COLOR_ENTRY_BG
        )
        self._step_size_entry = step_size_entry
        step_size_entry.pack(side=tk.LEFT)
        step_size_entry.bind('<Double-Button-1>', lambda e: self._enable_step_setting_edit('size'))
        self._bind_numeric_stepper(
            step_size_entry, self._step_size_var,
            Config.STEP_SIZE_KEY_INCREMENT, decimals=3,
            min_value=Config.MIN_STEP_SIZE
        )

        self._target_controls_frame = tk.Frame(parent)
        self._target_controls_frame.pack(pady=(0, 15))

        self._add_action_group(
            self._target_controls_frame,
            [("Run", self._on_run), ("Pause", self._on_pause), ("Zero", self._on_zero)],
            width=10,
        )
        self._add_action_group(
            self._target_controls_frame,
            [("Export", self._on_export), ("Clear Selected", self._on_clear)],
            width=13,
            padx=(14, 0),
        )

        self._step_controls_frame = tk.Frame(parent)
        self._step_controls_frame.pack(pady=(0, 15))

        self._add_action_group(
            self._step_controls_frame,
            [
                ("Step Up", lambda: self._on_step(direction=-1)),
                ("Step Down", lambda: self._on_step(direction=1)),
                ("Pause", self._on_pause),
                ("Zero", self._on_zero),
            ],
            width=11,
        )
        self._add_action_group(
            self._step_controls_frame,
            [("Export", self._on_export), ("Clear Selected", self._on_clear)],
            width=13,
            padx=(14, 0),
        )

        self._program_controls_frame = tk.Frame(parent)
        self._program_controls_frame.pack(pady=(0, 15))

        self._add_action_group(
            self._program_controls_frame,
            [
                ("Run", self._on_run),
                ("Pause", self._on_pause),
                ("Zero", self._on_zero),
                ("Reset", self._on_reset_program),
            ],
            width=10,
        )
        self._add_action_group(
            self._program_controls_frame,
            [("Load Program", self._on_load_program), ("Save Program", self._on_save_program)],
            width=13,
            padx=(14, 0),
        )
        self._add_action_group(
            self._program_controls_frame,
            [("Clear Selected", self._on_clear)],
            width=13,
            padx=(14, 0),
        )

        self._update_mode_visibility()

    def _add_action_group(self, parent: tk.Frame, buttons: list[tuple[str, Callable]],
                          width: int, padx: tuple[int, int] = (0, 0)):
        """Add a separated group of command buttons."""
        group = tk.Frame(parent)
        group.pack(side=tk.LEFT, padx=padx)
        for text, command in buttons:
            button = ttk.Button(group, text=text, command=command, width=width,
                                style="Primary.Console.TButton" if text in ("Run", "Step Up", "Step Down")
                                else "Pause.Console.TButton" if text == "Pause" else "Console.TButton")
            button.pack(side=tk.LEFT, padx=2)

    def _build_header_row(self):
        """Build the header row with column labels."""
        # Col 0: Buttons (+/-)
        adjust_frame = tk.Frame(self._motors_container)
        adjust_frame.grid(row=0, column=0, sticky='w', padx=(0, 5), pady=Config.HEADER_PAD_Y)
        
        ttk.Button(
            adjust_frame, text="+", command=self._add_motor,
            width=2, style="Console.TButton"
        ).pack(side=tk.LEFT, padx=1)
        
        ttk.Button(
            adjust_frame, text="-", command=self._remove_motor,
            width=2, style="Console.TButton"
        ).pack(side=tk.LEFT, padx=1)

        if self._is_step_mode():
            labels = [
                (1, "Absolute (mm):"),
                (2, "Relative (mm):"),
                (3, "Notes:"),
            ]
        elif self._is_program_mode():
            labels = [
                (1, "Instructions:"),
                (2, "Step:"),
                (3, "Speed (mm/s):"),
                (4, "Target (mm):"),
                (5, "Absolute (mm):"),
                (6, "Relative (mm):"),
                (7, "Notes:"),
            ]
        else:
            labels = [
                (1, "Absolute (mm)"),
                (2, "Relative (mm)"),
                (3, "Target (mm)"),
                (4, "Speed (mm/s)"),
                (5, "Notes:"),
            ]

        for col, text in labels:
            ttk.Label(
                self._motors_container, text=text.rstrip(":"), anchor='center', style="Console.TLabel"
            ).grid(row=0, column=col, sticky='ew', padx=5, pady=Config.HEADER_PAD_Y)

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
        stripe = tk.Frame(self._motors_container)
        stripe.grid(row=row_index, column=0, columnspan=self._visible_column_count(), sticky="nsew", pady=1)
        widgets["stripe"] = stripe

        # Col 0: Motor ID Label + Checkbox
        id_frame = tk.Frame(self._motors_container)
        id_frame.grid(row=row_index, column=0, sticky='w', padx=(0, 5), pady=10)
        widgets['row_frames'] = [stripe, id_frame]

        widgets['motor_label'] = tk.Label(id_frame, text=f"Drive {state.motor_id}:", width=8, anchor='w')
        widgets['motor_label'].pack(side=tk.LEFT)

        widgets['selected_var'] = tk.BooleanVar(value=state.is_selected)
        widgets['checkbox'] = tk.Checkbutton(
            id_frame,
            variable=widgets['selected_var'],
            command=lambda mid=state.motor_id: self._on_selection_change(mid)
        )
        widgets['checkbox'].pack(side=tk.LEFT)

        widgets['trajectory_button'] = ttk.Button(
            id_frame, text="Trajectory", style="Console.TButton",
            command=lambda mid=state.motor_id: self._on_trajectory_click(mid),
            state=tk.NORMAL if self._has_loaded_anatomy() else tk.DISABLED
        )
        widgets['trajectory_button'].pack(side=tk.LEFT, padx=(4, 0))

        visible_widgets = [stripe, id_frame]

        if self._is_step_mode():
            absolute_col = 1
            relative_col = 2
            notes_col = 3
        elif self._is_program_mode():
            absolute_col = 5
            relative_col = 6
            notes_col = 7

            widgets['instructions_button'] = ttk.Button(
                self._motors_container,
                text=self._instruction_button_text(state), style="Console.TButton",
                command=lambda mid=state.motor_id: self._on_instructions_click(mid),
                width=14
            )
            widgets['instructions_button'].grid(row=row_index, column=1, sticky='ew', padx=5, pady=10)

            widgets['program_step_var'] = tk.StringVar(value=self._program_step_text(state))
            widgets['program_step_entry'] = tk.Entry(
                self._motors_container, textvariable=widgets['program_step_var'],
                width=Config.ENTRY_WIDTH, state='readonly',
                readonlybackground=Theme.COLOR_READONLY_BG
            )
            widgets['program_step_entry'].grid(row=row_index, column=2, sticky='ew', padx=5, pady=10)

            instruction = self._current_instruction(state)
            widgets['speed_var'] = tk.StringVar(value=f"{instruction.speed:g}" if instruction else "")
            widgets['speed_entry'] = tk.Entry(
                self._motors_container, textvariable=widgets['speed_var'],
                width=Config.ENTRY_WIDTH, state='readonly',
                readonlybackground=Theme.COLOR_READONLY_BG
            )
            widgets['speed_entry'].grid(row=row_index, column=3, sticky='ew', padx=5, pady=10)

            widgets['target_var'] = tk.StringVar(value=format_mm(instruction.target) if instruction else "")
            widgets['target_entry'] = tk.Entry(
                self._motors_container, textvariable=widgets['target_var'],
                width=Config.ENTRY_WIDTH, state='readonly',
                readonlybackground=Theme.COLOR_READONLY_BG
            )
            widgets['target_entry'].grid(row=row_index, column=4, sticky='ew', padx=5, pady=10)

            visible_widgets.extend([
                widgets['instructions_button'],
                widgets['program_step_entry'],
                widgets['speed_entry'],
                widgets['target_entry'],
            ])
        else:
            absolute_col = 1
            relative_col = 2
            notes_col = 5

            # Col 1: Speed entry
            widgets['speed_var'] = tk.StringVar(value=f"{state.speed}" if state.speed else "")
            widgets['speed_entry'] = tk.Entry(
                self._motors_container, textvariable=widgets['speed_var'],
                width=Config.ENTRY_WIDTH
            )
            widgets['speed_entry'].grid(row=row_index, column=4, sticky='ew', padx=5, pady=10)
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
            widgets['target_entry'].grid(row=row_index, column=3, sticky='ew', padx=5, pady=10)
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
            readonlybackground=Theme.COLOR_READONLY_BG
        )
        widgets['absolute_entry'].grid(row=row_index, column=absolute_col, sticky='ew', padx=5, pady=10)
        widgets['absolute_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._enable_absolute_edit(mid))

        # Relative position
        widgets['relative_var'] = tk.StringVar(value=format_mm(state.relative))
        widgets['relative_entry'] = tk.Entry(
            self._motors_container, textvariable=widgets['relative_var'],
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground=Theme.COLOR_READONLY_BG
        )
        widgets['relative_entry'].grid(row=row_index, column=relative_col, sticky='ew', padx=5, pady=10)
        widgets['relative_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._enable_relative_edit(mid))

        # Notes
        notes_preview = state.notes[:15] + "..." if len(state.notes) > 15 else state.notes
        widgets['notes_var'] = tk.StringVar(value=notes_preview)
        widgets['notes_entry'] = tk.Entry(
            self._motors_container, textvariable=widgets['notes_var'],
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground='white', cursor='hand2'
        )
        widgets['notes_entry'].grid(row=row_index, column=notes_col, sticky='ew', padx=5, pady=10)
        widgets['notes_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._on_notes_click(mid))

        widgets["absolute_entry"].configure(font=self._readout_font, justify="center")
        widgets["relative_entry"].configure(justify="center")
        for key in ("speed_entry", "target_entry"):
            if key in widgets:
                widgets[key].configure(justify="right")

        # Store references to widgets for cleanup
        visible_widgets.extend([
            widgets['absolute_entry'], widgets['relative_entry'], widgets['notes_entry']
        ])
        widgets['all_widgets'] = visible_widgets
        self._apply_row_selection_style(widgets, state.is_selected)

        return widgets

    def _rebuild_motor_rows(self):
        """Rebuild all motor row widgets."""
        for child in self._motors_container.winfo_children():
            child.destroy()
        self._motor_widgets.clear()

        if self._is_step_mode():
            min_widths = Config.STEP_COL_MIN_WIDTHS
        elif self._is_program_mode():
            min_widths = Config.PROGRAM_COL_MIN_WIDTHS
        else:
            min_widths = Config.COL_MIN_WIDTHS
        max_columns = max(
            len(Config.COL_MIN_WIDTHS),
            len(Config.STEP_COL_MIN_WIDTHS),
            len(Config.PROGRAM_COL_MIN_WIDTHS),
        )
        for i in range(max_columns):
            self._motors_container.grid_columnconfigure(i, weight=0, minsize=0)
        for i, min_w in enumerate(min_widths):
            self._motors_container.grid_columnconfigure(i, weight=1, minsize=min_w)

        self._build_header_row()

        # Recreate rows starting from index 1 (0 is header)
        for i, state in enumerate(self._motor_states):
            widgets = self._create_motor_row(state, row_index=i + 1)
            self._motor_widgets.append(widgets)

        self._style_widget_tree(self._motors_container)
        for state, widgets in zip(self._motor_states, self._motor_widgets):
            self._apply_row_selection_style(widgets, state.is_selected)
        self._fit_window_to_content()

    def _visible_column_count(self) -> int:
        """Return the number of visible columns in the active motor table."""
        if self._is_step_mode():
            return len(Config.STEP_COL_MIN_WIDTHS)
        if self._is_program_mode():
            return len(Config.PROGRAM_COL_MIN_WIDTHS)
        return len(Config.COL_MIN_WIDTHS)

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _is_step_mode(self) -> bool:
        """Return whether the GUI is currently in step mode."""
        return bool(self._mode_var and self._mode_var.get() == Config.MODE_STEP)

    def _is_program_mode(self) -> bool:
        """Return whether the GUI is currently in program mode."""
        return bool(self._mode_var and self._mode_var.get() == Config.MODE_PROGRAM)

    def _select_mode(self, mode: str):
        """Select a movement mode from the segmented button control."""
        if self._mode_var:
            self._mode_var.set(mode)
        self._on_mode_change()

    def _refresh_mode_buttons(self):
        """Update segmented mode button relief to match the selected mode."""
        current = self._mode_var.get() if self._mode_var else Config.MODE_TARGET
        for mode, button in self._mode_buttons.items():
            selected = mode == current
            try:
                button.configure(style="Selected.Console.TButton" if selected else "Console.TButton")
            except tk.TclError:
                pass

    def _on_mode_change(self):
        """Handle switching between movement modes."""
        self._refresh_mode_buttons()
        self._update_mode_visibility()
        self._rebuild_motor_rows()
        self._fit_window_to_content()

    def _update_mode_visibility(self):
        """Show controls for the selected movement mode."""
        if not (
            self._target_controls_frame
            and self._step_controls_frame
            and self._step_settings_frame
            and self._program_controls_frame
        ):
            return

        if self._is_step_mode():
            self._target_controls_frame.pack_forget()
            self._program_controls_frame.pack_forget()
            self._pack_above_motor_table(self._step_settings_frame, pady=(0, 10))
            self._pack_above_motor_table(self._step_controls_frame, pady=(0, 15))
        elif self._is_program_mode():
            self._target_controls_frame.pack_forget()
            self._step_settings_frame.pack_forget()
            self._step_controls_frame.pack_forget()
            self._pack_above_motor_table(self._program_controls_frame, pady=(0, 15))
        else:
            self._step_settings_frame.pack_forget()
            self._step_controls_frame.pack_forget()
            self._program_controls_frame.pack_forget()
            self._pack_above_motor_table(self._target_controls_frame, pady=(0, 15))

    def _pack_above_motor_table(self, frame: tk.Frame, **pack_options):
        """Pack a control frame before the motor table, even after mode toggles."""
        if self._motors_container and frame.master == self._motors_container.master:
            frame.pack(before=self._motors_container, **pack_options)
        else:
            frame.pack(**pack_options)

    def _current_instruction(self, state: MotorState) -> Optional[InstructionStep]:
        """Return the current instruction for a motor, if any."""
        if not state.instructions:
            return None
        index = max(0, min(state.instruction_index, len(state.instructions) - 1))
        return state.instructions[index]

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
        widgets = self._get_widgets(motor_id)
        if not state or not widgets:
            return

        if 'instructions_button' in widgets:
            widgets['instructions_button'].configure(text=self._instruction_button_text(state))
        if 'program_step_var' in widgets:
            widgets['program_step_var'].set(self._program_step_text(state))

        instruction = self._current_instruction(state)
        if 'speed_var' in widgets:
            widgets['speed_var'].set(f"{instruction.speed:g}" if instruction else "")
        if 'target_var' in widgets:
            widgets['target_var'].set(format_mm(instruction.target) if instruction else "")

    def _activate_program_instruction(self, state: MotorState, instruction: InstructionStep):
        """Mirror the active program instruction into the motor's target-mode state."""
        state.speed = instruction.speed
        state.target = round(instruction.target, 3)
        self._sync_trajectory_from_target(state)
        self._update_trajectory_summary_motor_state(state)

    def _apply_row_selection_style(self, widgets: dict, selected: bool):
        """Visually mark rows selected for group movement commands."""
        bg = Theme.COLOR_ROW_SELECTED_BG if selected else Theme.COLOR_BG

        for frame in widgets.get('row_frames', []):
            try:
                frame.configure(background=bg)
            except tk.TclError:
                pass

        for key in ('motor_label', 'checkbox'):
            widget = widgets.get(key)
            if widget:
                try:
                    widget.configure(background=bg)
                    if key == "checkbox":
                        widget.configure(activebackground=bg)
                except tk.TclError:
                    pass

        for key in ("absolute_entry", "relative_entry"):
            if key in widgets:
                widgets[key].configure(readonlybackground=bg,
                                       highlightthickness=0 if widgets[key].cget("state") == "readonly" else 1)
        if hasattr(self, "_selection_text"):
            count = sum(state.is_selected for state in self._motor_states)
            self._selection_text.set(f"{count} of {len(self._motor_states)} drives selected  /  Movement commands apply to selected drives")

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

    def _bind_keyboard_shortcuts(self):
        """Bind global keyboard shortcuts for movement commands."""
        self._root.bind_all('<KeyPress>', self._on_keypress)
        self._root.bind_all('<KeyRelease>', self._on_keyrelease)
        self._root.bind_all('<Command-Return>', self._on_run_shortcut)
        self._root.bind_all('<Command-KP_Enter>', self._on_run_shortcut)
        self._root.bind_all('<Control-Return>', self._on_run_shortcut)
        self._root.bind_all('<Control-KP_Enter>', self._on_run_shortcut)
        self._root.bind_all('<End>', self._on_pause_shortcut)

    def _on_run_shortcut(self, event):
        """Run selected motors in target mode from Command/Ctrl+Enter."""
        widget = self._event_widget(event)
        if widget and widget.winfo_toplevel() != self._root:
            return None
        if self._is_step_mode() or self._is_program_mode():
            return None
        self._on_run()
        return "break"

    def _on_pause_shortcut(self, event):
        """Pause selected running motors from the End key."""
        self._on_pause()
        return "break"

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
            self._step_targets[state.motor_id] = target
            cmd = SerialProtocol.cmd_run(state.motor_id, speed, target, state.absolute)
            state.is_running = self._serial_manager.send(cmd)

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

        entry.config(state='normal', background=Theme.COLOR_ENTRY_BG)
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

        if not math.isfinite(step_size) or step_size < Config.MIN_STEP_SIZE:
            self._safe_popup(messagebox.showwarning, "Invalid Input",
                             f"Step size must be at least {Config.MIN_STEP_SIZE:g}mm.")
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
        if path != self._saved_anatomy_path:
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
        if self._trajectory_summary_btn:
            self._trajectory_summary_btn.configure(state=state)
        for widgets in self._motor_widgets:
            button = widgets.get('trajectory_button')
            if button:
                button.configure(state=state)

    def _poll_serial_events(self):
        """Only this Tk callback renews the controller's GUI responsiveness lease."""
        if self._closing:
            return
        try:
            self._serial_manager.poll_events()
            status = "Connected" if self._serial_manager.is_connected else "Connecting / disconnecting" if self._serial_manager.is_busy else "Disconnected"
            if self._connection_text.get() != status:
                self._connection_text.set(status)
            if not self._serial_manager.is_busy and not self._serial_manager.is_connected:
                self._connect_btn.config(text="Connect", state="normal")
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
            for event in self._trajectory_summary_window.poll_events():
                event_type = event.get("type")
                if event_type == "pause_requested":
                    self._on_pause()
                elif event_type in ("closed", "exited"):
                    self._trajectory_summary_window = None
                elif event_type == "error":
                    logger.error("Trajectory summary error: %s", event.get("message"))

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
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
        if state and widgets:
            state.is_selected = widgets['selected_var'].get()
            self._apply_row_selection_style(widgets, state.is_selected)

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
            widgets['target_var'].set(format_mm(state.target))

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
            entry.config(state='normal', background='white', highlightthickness=1)
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

        entry.config(state='readonly', readonlybackground=Theme.COLOR_READONLY_BG)
        self._apply_row_selection_style(widgets, state.is_selected)

    def _enable_relative_edit(self, motor_id: int):
        """Enable editing of relative position field."""
        widgets = self._get_widgets(motor_id)
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

        entry.config(state='readonly', readonlybackground=Theme.COLOR_READONLY_BG)
        self._apply_row_selection_style(widgets, state.is_selected)

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
                instruction = self._current_instruction(state)
                if instruction:
                    self._activate_program_instruction(state, instruction)
        else:
            state.instruction_index = 0
            state.instruction_status = "idle"
        self._update_program_row(motor_id)
        self._save_state()

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
                "--orientation-actual", self._anatomy_orientation_actual,
                "--orientation-scanner", self._anatomy_orientation_scanner,
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
        current = self._port_var.get() if self._port_var else ""
        if current and current not in ports:
            ports.insert(0, current)
        self._port_combo['values'] = ports

    def _on_connect(self):
        """Handle connect/disconnect button click."""
        port = self._port_var.get()
        if not port:
            messagebox.showwarning("No Port", "Please select a port.")
            return
        self._saved_port = port

        if self._serial_manager.is_connected:
            # Pause all running motors before disconnecting
            self._reset_step_key_state()
            for state in self._motor_states:
                if state.is_running:
                    self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
                    state.is_running = False
                    if state.instruction_status == "running":
                        state.instruction_status = "paused"
                        self._update_program_row(state.motor_id)
            self._serial_manager.disconnect()
            self._step_targets.clear()
            self._connect_btn.config(text="Disconnecting...", state="disabled")
            self._focus_main_window()
        else:
            if self._serial_manager.connect(port):
                self._connect_btn.config(text="Connecting...", state="disabled")
            else:
                self._connect_btn.config(text="Disconnecting...", state="disabled")

    def _on_connected(self):
        """Finish connection setup on the Tk thread after the worker opens the port."""
        self._send_settings_to_controller()
        self._connect_btn.config(text="Disconnect", state="normal")
        self._save_state()
        self._focus_main_window()

    def _on_connection_lost(self):
        """Handle a queued connection-loss event on the Tk thread."""
        self._reset_step_key_state()
        for state in self._motor_states:
            if state.instruction_status == "running":
                state.instruction_status = "paused"
                self._update_program_row(state.motor_id)
            state.is_running = False
        self._step_targets.clear()
        self._root.after(0, self._show_connection_lost_dialog)

    def _show_connection_lost_dialog(self):
        """Show connection lost dialog on main thread."""
        self._connect_btn.config(text="Connect", state="normal")
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

        if self._is_program_mode():
            self._run_program_instructions(selected)
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
                widgets['target_var'].set(format_mm(state.target))
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
            state.is_running = self._serial_manager.send(cmd)

    def _run_program_instructions(self, selected: list[MotorState]):
        """Run or continue selected motors' active instructions."""
        runnable: list[tuple[MotorState, InstructionStep]] = []
        skipped_complete = 0

        for state in selected:
            if not state.instructions:
                self._safe_popup(
                    messagebox.showwarning,
                    "Missing Instructions",
                    f"Motor {state.motor_id} has no instructions."
                )
                return

            if state.instruction_status == "complete":
                skipped_complete += 1
                continue

            instruction = self._current_instruction(state)
            if not instruction:
                continue

            if not (self._min_speed <= instruction.speed <= self._max_speed):
                self._safe_popup(
                    messagebox.showwarning,
                    "Invalid Instructions",
                    f"Motor {state.motor_id} instruction speed is outside the current settings range."
                )
                return
            if not (Config.MIN_TARGET_POS <= instruction.target <= Config.MAX_TARGET_POS):
                self._safe_popup(
                    messagebox.showwarning,
                    "Invalid Instructions",
                    f"Motor {state.motor_id} instruction target is outside the allowed range."
                )
                return

            runnable.append((state, instruction))

        if not runnable and skipped_complete:
            return

        for state, instruction in runnable:
            self._activate_program_instruction(state, instruction)
            cmd = SerialProtocol.cmd_run(
                state.motor_id, state.speed, state.target, state.absolute
            )
            state.is_running = self._serial_manager.send(cmd)
            state.instruction_status = "running" if state.is_running else "paused"
            self._update_program_row(state.motor_id)

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
            self._step_targets[state.motor_id] = target
            cmd = SerialProtocol.cmd_run(state.motor_id, speed, target, state.absolute)
            state.is_running = self._serial_manager.send(cmd)

        self._focus_main_window()

    def _on_pause(self):
        """Handle Pause button click."""
        self._reset_step_key_state()
        selected = [s for s in self._motor_states if s.is_selected and s.is_running]
        for state in selected:
            self._serial_manager.send(SerialProtocol.cmd_pause(state.motor_id))
            state.is_running = False
            if state.instruction_status == "running":
                state.instruction_status = "paused"
                self._update_program_row(state.motor_id)
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
        self._style_widget_tree(window)
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
            self._step_targets.pop(state.motor_id, None)
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
            state.instructions = []
            state.instruction_index = 0
            state.instruction_status = "idle"

            widgets = self._get_widgets(state.motor_id)
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
        """Apply a queued position update on the Tk thread."""
        state = self._get_state(motor_id)
        if state:
            state.absolute = round(absolute, 3)
            state.relative = round(state.absolute - state.relative_zero_point, 3)
            if not state.is_running:
                self._step_targets.pop(motor_id, None)

        self._update_position_display(motor_id, absolute)

    def _update_position_display(self, motor_id: int, absolute: float):
        """Update position display widgets on main thread."""
        state = self._get_state(motor_id)
        widgets = self._get_widgets(motor_id)
        if state and widgets:
            self._advance_program_if_ready(state)
            if widgets['absolute_entry']['state'] == 'readonly':
                widgets['absolute_var'].set(format_mm(state.absolute))
            if widgets['relative_entry']['state'] == 'readonly':
                widgets['relative_var'].set(format_mm(state.relative))
            self._update_trajectory_motor_position(motor_id)

    def _advance_program_if_ready(self, state: MotorState):
        """Advance one motor's instructions when the current target is reached."""
        if state.instruction_status != "running" or not state.instructions:
            return

        instruction = self._current_instruction(state)
        if not instruction:
            return

        if abs(state.absolute - instruction.target) > Config.PROGRAM_TARGET_TOLERANCE_MM:
            return

        if state.instruction_index + 1 >= len(state.instructions):
            state.is_running = False
            state.instruction_status = "complete"
            self._update_program_row(state.motor_id)
            return

        state.instruction_index += 1
        next_instruction = self._current_instruction(state)
        if next_instruction:
            self._activate_program_instruction(state, next_instruction)
        self._update_program_row(state.motor_id)
        if not next_instruction or not self._serial_manager.is_connected:
            return

        cmd = SerialProtocol.cmd_run(
            state.motor_id,
            state.speed,
            state.target,
            state.absolute,
        )
        state.is_running = self._serial_manager.send(cmd)
        if not state.is_running:
            state.instruction_status = "paused"
            self._update_program_row(state.motor_id)

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
                'port': self._port_var.get() if self._port_var else self._saved_port,
                'mode': self._mode_var.get() if self._mode_var else self._saved_mode,
                'step_speed': self._step_speed_var.get() if self._step_speed_var else f"{self._saved_step_speed:g}",
                'step_size': self._step_size_var.get() if self._step_size_var else format_mm(self._saved_step_size),
                'min_speed': self._min_speed,
                'max_speed': self._max_speed,
                'backlash': round(self._backlash, 3),
                'anatomy_path': self._saved_anatomy_path,
                'anatomy_orientation_actual': self._anatomy_orientation_actual,
                'anatomy_orientation_scanner': self._anatomy_orientation_scanner,
                'recent_programs': self._recent_programs[:10],
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
                    if state.instruction_status == "running":
                        state.instruction_status = "paused"
                    state.target = round(state.target, 3)
                    state.absolute = round(state.absolute, 3)
                    state.relative = round(state.relative, 3)
                    state.relative_zero_point = round(state.relative_zero_point, 3)

                mode = data.get('mode', Config.MODE_TARGET)
                valid_modes = (Config.MODE_TARGET, Config.MODE_STEP, Config.MODE_PROGRAM)
                self._saved_mode = mode if mode in valid_modes else Config.MODE_TARGET
                self._saved_port = str(data.get('port', ""))
                self._saved_step_speed = float(data.get('step_speed', Config.DEFAULT_STEP_SPEED))
                self._saved_step_size = round(float(data.get('step_size', Config.DEFAULT_STEP_SIZE)), 3)
                self._min_speed = float(data.get('min_speed', Config.MIN_SPEED))
                self._max_speed = float(data.get('max_speed', Config.MAX_SPEED))
                self._backlash = round(float(data.get('backlash', Config.DEFAULT_BACKLASH_MM)), 3)
                self._saved_anatomy_path = str(data.get('anatomy_path', ""))
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
        self._reset_step_key_state()
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
