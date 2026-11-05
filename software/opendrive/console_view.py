"""Tk presentation for OpenDrive's full and compact consoles.

ConsoleView owns widgets, Tk variables, styling, and layout. The application
owns drive state and movement decisions. Button/field callbacks call the app
explicitly; this view never writes to the serial port or loads workspace files.
All methods run on the Tk thread.
"""
from typing import Callable, Optional, TYPE_CHECKING
import tkinter as tk
from tkinter import ttk, font as tkfont

from .config import Config
from .console_theme import ConsoleTheme as Theme
from .models import MotorState
from .utils import format_mm


if TYPE_CHECKING:
    from opendrive_gui import OpenDriveApp


class ConsoleView:
    """Two presentations of the same drive data and selection variables."""

    def __init__(self, root: tk.Tk, app: 'OpenDriveApp'):
        self.root = root
        self._app = app
        self.motor_widgets = []
        self.compact_active = False
        self.full_console_size = None
        self.port_var = None
        self.port_combo = None
        self.connect_btn = None
        self.mode_var = None
        self.mode_buttons = {}
        self.step_speed_var = None
        self.step_size_var = None
        self.step_speed_entry = None
        self.step_size_entry = None
        self.target_controls_frame = None
        self.step_controls_frame = None
        self.step_settings_frame = None
        self.program_controls_frame = None
        self.trajectory_summary_btn = None
        self.motors_container = None
        self.configure_light_theme()

    def configure_light_theme(self):
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
            self.root.option_add(option, value)

        self.root.tk_setPalette(
            background=Theme.COLOR_BG,
            foreground=Theme.COLOR_FG,
            activeBackground=Theme.COLOR_ACTIVE_BG,
            activeForeground=Theme.COLOR_FG,
            highlightColor=Theme.COLOR_SELECT_BG,
            selectBackground=Theme.COLOR_SELECT_BG,
            selectForeground=Theme.COLOR_ENTRY_BG,
        )
        self.root.configure(background=Theme.COLOR_BG)

        self.console_font = tkfont.nametofont("TkDefaultFont").copy()
        self.console_font.configure(size=11)
        self.readout_font = self.console_font.copy()
        self.readout_font.configure(size=13, weight="bold")
        self.root.option_add("*Font", self.console_font)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TCombobox",
            font=self.console_font,
            fieldbackground=Theme.COLOR_ENTRY_BG,
            background=Theme.COLOR_ENTRY_BG,
            foreground=Theme.COLOR_FG,
            selectbackground=Theme.COLOR_SELECT_BG,
            selectforeground=Theme.COLOR_ENTRY_BG,
        )

        style.configure("Console.TButton", font=self.console_font, padding=(10, 6), background="#ffffff",
                        foreground=Theme.COLOR_FG, bordercolor="#dce2e9", relief="flat")
        style.map("Console.TButton", background=[("active", "#eaf1fa")],
                  foreground=[("disabled", "#929ba7")])
        style.configure("Primary.Console.TButton", background="#225da8", foreground="white")
        style.map("Primary.Console.TButton", background=[("active", "#194b89")],
                  foreground=[("disabled", "#b7c6da"), ("!disabled", "white")])
        style.configure("Pause.Console.TButton", foreground="#a52d36")
        style.configure("Selected.Console.TButton", background="#eaf1fa", foreground="#225da8")
        style.configure("Console.TLabel", font=self.console_font, background=Theme.COLOR_BG, foreground="#626e7d")
        style.configure('Compact.Readout.TLabel', background=Theme.COLOR_BG, foreground=Theme.COLOR_FG,
                        font=(self.console_font.actual('family'), 11, 'bold'))
        style.configure("Title.Console.TLabel", font=(self.console_font.actual("family"), 26, "bold"),
                        foreground=Theme.COLOR_FG)

    def style_widget_tree(self, widget=None):
        """Apply explicit colors to Tk widgets that may ignore the app palette."""
        widget = widget or self.root

        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
                widget.configure(background=Theme.COLOR_BG)
            elif isinstance(widget, tk.Label):
                if getattr(widget, "_opendrive_role", "") not in ("mode_tab", "motion_status"):
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
                role = getattr(widget, "_opendrive_role", "")
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
            self.style_widget_tree(child)

        if widget is self.root:
            self.refresh_mode_buttons()
            for state, row in zip(self._app.motor_states, self.motor_widgets):
                self.apply_row_selection_style(row, state.is_selected)

    def fit_window_to_content(self):
        """Fit mode contents while preserving space the user has added."""
        self.root.update_idletasks()
        width = max(260 if self.compact_active else 980, self.root.winfo_reqwidth())
        height = self.root.winfo_reqheight()
        self.root.minsize(width, height)
        if self.compact_active:
            self.root.geometry(f'{width}x{height}')
        else:
            self.root.geometry(f"{max(width, self.root.winfo_width())}x{max(height, self.root.winfo_height())}")

    def build(self):
        menu = tk.Menu(self.root)
        self.config_menu = tk.Menu(menu, tearoff=False)
        self.config_menu.add_command(label='Save Config...', command=self._app._on_save_config)
        self.config_menu.add_command(label='Load Config...', command=self._app._on_load_config)
        menu.add_cascade(label='File', menu=self.config_menu)
        self.compact_view_var = tk.BooleanVar(self.root, value=self._app._saved_compact_view)
        view_menu = tk.Menu(menu, tearoff=False)
        view_menu.add_checkbutton(label='Compact View', variable=self.compact_view_var,
                                  command=self.set_compact_view)
        menu.add_cascade(label='View', menu=view_menu)
        self.root.configure(menu=menu)
        main = tk.Frame(self.root, padx=24, pady=20)
        self.full_console_frame = main
        main.pack(fill=tk.BOTH, expand=True)
        header = tk.Frame(main)
        header.pack(fill=tk.X, pady=(0, 22))
        # Override the application-wide *Font option, which takes precedence over ttk styles.
        ttk.Label(header, text="OpenDrive", style="Title.Console.TLabel",
                  font=(self.console_font.actual("family"), 26, "bold")).pack(side=tk.LEFT)
        self.build_port_selection(header)
        toolbar = tk.Frame(main)
        toolbar.pack(fill=tk.X, pady=(0, 16))
        self.mode_toolbar = tk.Frame(toolbar)
        self.mode_toolbar.pack(side=tk.LEFT)
        utilities = tk.Frame(toolbar)
        utilities.pack(side=tk.RIGHT)
        self.build_utility_buttons(utilities)
        self.step_options = tk.Frame(main)
        self.step_options.pack(fill=tk.X)
        ttk.Separator(main).pack(fill=tk.X)
        self.motors_container = tk.Frame(main, pady=8)
        self.motors_container.pack(fill=tk.BOTH, expand=True)
        ttk.Separator(main).pack(fill=tk.X, pady=(12, 12))
        footer = tk.Frame(main)
        footer.pack(fill=tk.X)
        self.selection_text = tk.StringVar(value="")
        actions = tk.Frame(footer)
        actions.pack(fill=tk.X)
        self.build_control_buttons(actions)
        pause_all_row = tk.Frame(footer)
        pause_all_row.pack(fill=tk.X)
        self.stop_all_btn = ttk.Button(pause_all_row, text='Pause All', command=self._app._on_stop_all,
                                        style='Pause.Console.TButton', width=13)
        self.stop_all_btn.pack(pady=(8, 4))
        self.build_compact_view()

    def build_compact_view(self):
        self.compact_frame = tk.Frame(self.root, padx=12, pady=10)
        header = tk.Frame(self.compact_frame)
        header.pack(fill=tk.X, pady=(0, 4))
        tk.Label(header, text='OpenDrive', font=(self.console_font.actual('family'), 13, 'bold')).pack(side=tk.LEFT)
        ttk.Button(header, text='Expand', command=lambda: self.set_compact_view(False),
                   style='Console.TButton', width=7).pack(side=tk.RIGHT)
        status = tk.Frame(self.compact_frame)
        status.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(status, textvariable=self.connection_text, style='Console.TLabel').pack(side=tk.LEFT)
        ttk.Label(status, textvariable=self.mode_var, style='Console.TLabel').pack(side=tk.RIGHT)
        self.compact_table = tk.Frame(self.compact_frame)
        self.compact_table.pack(fill=tk.BOTH, expand=True)
        self.compact_widgets = {}
        actions = tk.Frame(self.compact_frame)
        actions.pack(fill=tk.X, pady=(10, 0))
        self.compact_run_btn = ttk.Button(actions, text='Run Selected', command=self._app._on_run,
            style='Primary.Console.TButton', state='disabled' if self.is_step_mode() else 'normal')
        self.compact_run_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.compact_run_btn.bind('<Return>', lambda _event: 'break')
        self.compact_run_btn.bind('<KP_Enter>', lambda _event: 'break')
        self.compact_run_btn.bind('<KeyRelease-Return>', lambda _event: 'break')
        self.compact_run_btn.bind('<KeyRelease-KP_Enter>', lambda _event: 'break')
        self.compact_pause_all_btn = ttk.Button(actions, text='Pause All',
            command=self._app._on_stop_all, style='Pause.Console.TButton')
        self.compact_pause_all_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

    def rebuild_compact_rows(self):
        for child in self.compact_table.winfo_children():
            child.destroy()
        self.compact_widgets.clear()
        for column, label in enumerate(('Drive', 'Abs (mm)', 'Rel (mm)')):
            ttk.Label(self.compact_table, text=label, style='Console.TLabel', anchor='center').grid(
                row=0, column=column, sticky='ew', padx=5, pady=(0, 5))
            self.compact_table.grid_columnconfigure(column, weight=1)
        for row, (state, widgets) in enumerate(zip(self._app.motor_states, self.motor_widgets), 1):
            selection = tk.Checkbutton(self.compact_table, text=str(state.motor_id),
                variable=widgets['selected_var'], command=lambda mid=state.motor_id: self._app._on_selection_change(mid),
                background=Theme.COLOR_BG, activebackground=Theme.COLOR_BG, selectcolor=Theme.COLOR_ENTRY_BG)
            selection.grid(
                row=row, column=0, sticky='ew', padx=5, pady=3)
            values = {'selection': selection}
            for column, name in enumerate(('absolute', 'relative'), 1):
                label = ttk.Label(self.compact_table, textvariable=widgets[f'{name}_var'],
                                  style='Compact.Readout.TLabel', anchor='center', width=8)
                label.grid(row=row, column=column, sticky='ew', padx=5, pady=3)
                values[name] = label
            self.compact_widgets[state.motor_id] = values

    def set_compact_view(self, compact=None, remember=True):
        compact = self.compact_view_var.get() if compact is None else bool(compact)
        self.compact_view_var.set(compact)
        if compact == self.compact_active:
            return
        self._app.shortcuts.reset_step_key_state()
        if compact:
            self.full_console_size = (self.root.winfo_width(), self.root.winfo_height())
            if self.root.state() == 'zoomed':
                self.root.state('normal')
            self.full_console_frame.pack_forget()
            self.compact_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.compact_frame.pack_forget()
            self.full_console_frame.pack(fill=tk.BOTH, expand=True)
        self.compact_active = compact
        self.root.minsize(1, 1)
        if not compact and self.full_console_size:
            self.root.geometry(f'{self.full_console_size[0]}x{self.full_console_size[1]}')
        self.fit_window_to_content()
        self.root.focus_set()
        if remember:
            self._app._save_state()

    def build_port_selection(self, parent):
        connection = tk.Frame(parent)
        connection.pack(side=tk.RIGHT)
        self.connection_text = tk.StringVar(value="Disconnected")
        ttk.Label(connection, textvariable=self.connection_text, style="Console.TLabel").pack(side=tk.LEFT, padx=(0, 12))
        self.port_var = tk.StringVar(value=self._app._saved_port)
        self.port_combo = ttk.Combobox(connection, textvariable=self.port_var, width=24, state="readonly")
        if self._app._saved_port:
            self.port_combo["values"] = [self._app._saved_port]
        self.port_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.port_combo.bind("<Button-1>", self._app._refresh_ports)
        self.connect_btn = ttk.Button(connection, text="Connect", command=self._app._on_connect, style="Console.TButton")
        self.connect_btn.pack(side=tk.LEFT)

    def build_utility_buttons(self, parent):
        for label, command in (("Anatomy", self._app._on_anatomy),
                               ("Trajectories", self._app._on_trajectory_summary),
                               ("Probes", self._app._on_probe_summary),
                               ("Settings", self._app._on_settings)):
            button = ttk.Button(parent, text=label, command=command, style="Console.TButton")
            button.pack(side=tk.LEFT, padx=(5, 0))
            if label == "Trajectories":
                self.trajectory_summary_btn = button
                button.configure(state=tk.NORMAL if self._app._has_loaded_anatomy() else tk.DISABLED)

    def build_control_buttons(self, parent: tk.Frame):
        """Build the main control buttons."""
        mode_frame = self.mode_toolbar

        self.mode_var = tk.StringVar(value=self._app._saved_mode)
        self.mode_buttons = {}
        for label, value in (
            ("Target", Config.MODE_TARGET),
            ("Step", Config.MODE_STEP),
            ("Program", Config.MODE_PROGRAM),
        ):
            button = ttk.Button(mode_frame, text=label, width=9,
                                command=lambda mode=value: self._app._select_mode(mode),
                                style="Console.TButton")
            button.pack(side=tk.LEFT)
            self.mode_buttons[value] = button
        self.refresh_mode_buttons()

        self.step_settings_frame = tk.Frame(self.step_options)
        self.step_settings_frame.pack(pady=(0, 10))

        self.step_speed_var = tk.StringVar(value=f"{self._app._saved_step_speed:g}")
        self.step_size_var = tk.StringVar(value=format_mm(self._app._saved_step_size))

        tk.Label(self.step_settings_frame, text="Step speed (mm/s):").pack(side=tk.LEFT, padx=(0, 5))
        step_speed_entry = tk.Entry(
            self.step_settings_frame, textvariable=self.step_speed_var,
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground=Theme.COLOR_ENTRY_BG
        )
        self.step_speed_entry = step_speed_entry
        step_speed_entry.pack(side=tk.LEFT, padx=(0, 15))
        step_speed_entry.bind('<Double-Button-1>', lambda e: self._app._enable_step_setting_edit('speed'))
        self.bind_numeric_stepper(
            step_speed_entry, self.step_speed_var,
            Config.STEP_SPEED_KEY_INCREMENT, decimals=3,
            min_value=lambda: self._app._min_speed,
            max_value=lambda: self._app._max_speed
        )

        tk.Label(self.step_settings_frame, text="Step size (mm):").pack(side=tk.LEFT, padx=(0, 5))
        step_size_entry = tk.Entry(
            self.step_settings_frame, textvariable=self.step_size_var,
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground=Theme.COLOR_ENTRY_BG
        )
        self.step_size_entry = step_size_entry
        step_size_entry.pack(side=tk.LEFT)
        step_size_entry.bind('<Double-Button-1>', lambda e: self._app._enable_step_setting_edit('size'))
        self.bind_numeric_stepper(
            step_size_entry, self.step_size_var,
            Config.STEP_SIZE_KEY_INCREMENT, decimals=3,
            min_value=Config.MIN_STEP_SIZE
        )

        self.target_controls_frame = tk.Frame(parent)
        self.target_controls_frame.pack(pady=(0, 15))

        self.add_action_group(
            self.target_controls_frame,
            [("Run Selected", self._app._on_run), ("Pause Selected", self._app._on_pause), ("Zero Selected", self._app._on_zero)],
            width=10,
        )
        self.add_action_group(
            self.target_controls_frame,
            [("Export", self._app._on_export), ("Clear Selected", self._app._on_clear)],
            width=13,
            padx=(14, 0),
        )

        self.step_controls_frame = tk.Frame(parent)
        self.step_controls_frame.pack(pady=(0, 15))

        self.add_action_group(
            self.step_controls_frame,
            [
                ("Step Up", lambda: self._app._on_step(direction=-1)),
                ("Step Down", lambda: self._app._on_step(direction=1)),
                ("Pause Selected", self._app._on_pause),
                ("Zero Selected", self._app._on_zero),
            ],
            width=11,
        )
        self.add_action_group(
            self.step_controls_frame,
            [("Export", self._app._on_export), ("Clear Selected", self._app._on_clear)],
            width=13,
            padx=(14, 0),
        )

        self.program_controls_frame = tk.Frame(parent)
        self.program_controls_frame.pack(pady=(0, 15))

        self.add_action_group(
            self.program_controls_frame,
            [
                ("Run Selected", self._app._on_run),
                ("Pause Selected", self._app._on_pause),
                ("Zero Selected", self._app._on_zero),
                ("Reset", self._app._on_reset_program),
            ],
            width=10,
        )
        self.add_action_group(
            self.program_controls_frame,
            [("Load Program", self._app._on_load_program), ("Save Program", self._app._on_save_program)],
            width=13,
            padx=(14, 0),
        )
        self.add_action_group(
            self.program_controls_frame,
            [("Clear Selected", self._app._on_clear)],
            width=13,
            padx=(14, 0),
        )

        self.update_mode_visibility()

    def add_action_group(self, parent: tk.Frame, buttons: list[tuple[str, Callable]],
                          width: int, padx: tuple[int, int] = (0, 0)):
        """Add a separated group of command buttons."""
        group = tk.Frame(parent)
        group.pack(side=tk.LEFT, padx=padx)
        for text, command in buttons:
            button = ttk.Button(group, text=text, command=command, width=max(width, len(text)),
                                style="Primary.Console.TButton" if text in ("Run Selected", "Step Up", "Step Down")
                                else "Pause.Console.TButton" if text == "Pause Selected" else "Console.TButton")
            if text in ('Run Selected', 'Step Up', 'Step Down'):
                # Some Tk platforms activate a focused button on Enter.
                # Plain Enter confirms fields; only the explicit modifier
                # shortcut may start movement.
                button.bind('<Return>', lambda _event: 'break')
                button.bind('<KP_Enter>', lambda _event: 'break')
                for shortcut in self._app.shortcuts.run_shortcuts():
                    button.bind(shortcut, self._app.shortcuts.on_run_shortcut)
                button.bind('<KeyRelease-Return>', lambda _event: 'break')
                button.bind('<KeyRelease-KP_Enter>', lambda _event: 'break')
            button.pack(side=tk.LEFT, padx=2)

    def build_header_row(self):
        """Build the header row with column labels."""
        # Col 0: Buttons (+/-)
        adjust_frame = tk.Frame(self.motors_container)
        adjust_frame.grid(row=0, column=0, sticky='w', padx=(0, 5), pady=Config.HEADER_PAD_Y)

        ttk.Button(
            adjust_frame, text="+", command=self._app._add_motor,
            width=2, style="Console.TButton"
        ).pack(side=tk.LEFT, padx=1)

        ttk.Button(
            adjust_frame, text="-", command=self._app._remove_motor,
            width=2, style="Console.TButton"
        ).pack(side=tk.LEFT, padx=1)

        if self.is_step_mode():
            labels = [
                (1, "Absolute (mm):"),
                (2, "Relative (mm):"),
                (3, "Notes:"),
            ]
        elif self.is_program_mode():
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
                self.motors_container, text=text.rstrip(":"), anchor='center', style="Console.TLabel"
            ).grid(row=0, column=col, sticky='ew', padx=5, pady=Config.HEADER_PAD_Y)

    def create_motor_row(self, state: MotorState, row_index: int) -> dict:
        """
        Create widgets for a motor row.

        Args:
            state: MotorState for this row
            row_index: Grid row index (1-based, 0 is header)

        Returns:
            Dictionary of widget references.
        """
        widgets = {}
        stripe = tk.Frame(self.motors_container)
        stripe.grid(row=row_index, column=0, columnspan=self.visible_column_count(), sticky="nsew", pady=1)
        widgets["stripe"] = stripe

        # Col 0: Motor ID Label + Checkbox
        id_frame = tk.Frame(self.motors_container)
        id_frame.grid(row=row_index, column=0, sticky='w', padx=(0, 5), pady=10)
        widgets['row_frames'] = [stripe, id_frame]

        widgets['motor_label'] = tk.Label(id_frame, text=f"Drive {state.motor_id}:", width=8, anchor='w')
        widgets['motor_label'].pack(side=tk.LEFT)

        widgets['selected_var'] = tk.BooleanVar(value=state.is_selected)
        widgets['checkbox'] = tk.Checkbutton(
            id_frame,
            variable=widgets['selected_var'],
            command=lambda mid=state.motor_id: self._app._on_selection_change(mid)
        )
        widgets['checkbox'].pack(side=tk.LEFT)

        widgets['motion_status'] = tk.Label(id_frame, text="Idle", width=10, anchor="center",
                                            font=(self.console_font.actual("family"), 11, "bold"),
                                            foreground="#626e7d", background=Theme.COLOR_BG)
        widgets['motion_status']._opendrive_role = "motion_status"
        widgets['motion_status'].pack(side=tk.LEFT, padx=(8, 0))

        visible_widgets = [stripe, id_frame]

        if self.is_step_mode():
            absolute_col = 1
            relative_col = 2
            notes_col = 3
        elif self.is_program_mode():
            absolute_col = 5
            relative_col = 6
            notes_col = 7

            widgets['instructions_button'] = ttk.Button(
                self.motors_container,
                text=self._app._instruction_button_text(state), style="Console.TButton",
                command=lambda mid=state.motor_id: self._app._on_instructions_click(mid),
                width=14
            )
            widgets['instructions_button'].grid(row=row_index, column=1, sticky='ew', padx=5, pady=10)

            widgets['program_step_var'] = tk.StringVar(value=self._app._program_step_text(state))
            widgets['program_step_entry'] = tk.Entry(
                self.motors_container, textvariable=widgets['program_step_var'],
                width=Config.ENTRY_WIDTH, state='readonly',
                readonlybackground=Theme.COLOR_READONLY_BG
            )
            widgets['program_step_entry'].grid(row=row_index, column=2, sticky='ew', padx=5, pady=10)

            instruction = self._app.motion.current_instruction(state)
            widgets['speed_var'] = tk.StringVar(value=f"{instruction.speed:g}" if instruction else "")
            widgets['speed_entry'] = tk.Entry(
                self.motors_container, textvariable=widgets['speed_var'],
                width=Config.ENTRY_WIDTH, state='readonly',
                readonlybackground=Theme.COLOR_READONLY_BG
            )
            widgets['speed_entry'].grid(row=row_index, column=3, sticky='ew', padx=5, pady=10)

            widgets['target_var'] = tk.StringVar(value=format_mm(instruction.target) if instruction else "")
            widgets['target_entry'] = tk.Entry(
                self.motors_container, textvariable=widgets['target_var'],
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

            # Target-mode speed and destination fields.
            widgets['speed_var'] = tk.StringVar(value=f"{state.speed}" if state.speed else "")
            widgets['speed_entry'] = tk.Entry(
                self.motors_container, textvariable=widgets['speed_var'],
                width=Config.ENTRY_WIDTH
            )
            widgets['speed_entry'].grid(row=row_index, column=4, sticky='ew', padx=5, pady=10)
            widgets['speed_entry'].bind('<KeyRelease>', lambda e, mid=state.motor_id: self._app._on_speed_change(mid))
            widgets['speed_entry'].bind('<FocusOut>', lambda e, mid=state.motor_id: self._app._on_speed_change(mid))
            self.bind_numeric_stepper(
                widgets['speed_entry'], widgets['speed_var'],
                Config.SPEED_KEY_INCREMENT, decimals=3,
                min_value=lambda: self._app._min_speed,
                max_value=lambda: self._app._max_speed,
                on_change=lambda mid=state.motor_id: self._app._on_speed_change(mid)
            )
            self.bind_vertical_tab(widgets['speed_entry'], state.motor_id, 'speed_entry')
            visible_widgets.append(widgets['speed_entry'])

            widgets['target_var'] = tk.StringVar(value=format_mm(state.target) if state.target else "")
            widgets['target_entry'] = tk.Entry(
                self.motors_container, textvariable=widgets['target_var'],
                width=Config.ENTRY_WIDTH
            )
            widgets['target_entry'].grid(row=row_index, column=3, sticky='ew', padx=5, pady=10)
            widgets['target_entry'].bind('<KeyRelease>', lambda e, mid=state.motor_id: self._app._on_target_change(mid))
            widgets['target_entry'].bind('<FocusOut>', lambda e, mid=state.motor_id: self._app._on_target_focus_out(mid))
            self.bind_numeric_stepper(
                widgets['target_entry'], widgets['target_var'],
                Config.TARGET_KEY_INCREMENT, decimals=3,
                min_value=Config.MIN_TARGET_POS,
                max_value=Config.MAX_TARGET_POS,
                on_change=lambda mid=state.motor_id: self._app._on_target_change(mid)
            )
            self.bind_vertical_tab(widgets['target_entry'], state.motor_id, 'target_entry')
            visible_widgets.append(widgets['target_entry'])

        # Absolute position
        widgets['absolute_var'] = tk.StringVar(value=format_mm(state.absolute))
        widgets['absolute_entry'] = tk.Entry(
            self.motors_container, textvariable=widgets['absolute_var'],
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground=Theme.COLOR_READONLY_BG
        )
        widgets['absolute_entry'].grid(row=row_index, column=absolute_col, sticky='ew', padx=5, pady=10)
        widgets['absolute_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._app._enable_absolute_edit(mid))

        # Relative position
        widgets['relative_var'] = tk.StringVar(value=format_mm(state.relative))
        widgets['relative_entry'] = tk.Entry(
            self.motors_container, textvariable=widgets['relative_var'],
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground=Theme.COLOR_READONLY_BG
        )
        widgets['relative_entry'].grid(row=row_index, column=relative_col, sticky='ew', padx=5, pady=10)
        widgets['relative_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._app._enable_relative_edit(mid))

        # Notes
        notes_preview = state.notes[:15] + "..." if len(state.notes) > 15 else state.notes
        widgets['notes_var'] = tk.StringVar(value=notes_preview)
        widgets['notes_entry'] = tk.Entry(
            self.motors_container, textvariable=widgets['notes_var'],
            width=Config.ENTRY_WIDTH, state='readonly',
            readonlybackground='white', cursor='hand2'
        )
        widgets['notes_entry'].grid(row=row_index, column=notes_col, sticky='ew', padx=5, pady=10)
        widgets['notes_entry'].bind('<Double-Button-1>', lambda e, mid=state.motor_id: self._app._on_notes_click(mid))

        widgets['zero_button'] = ttk.Button(
            self.motors_container, text='Zero', width=7, style='Console.TButton',
            command=lambda mid=state.motor_id: self._app._zero_motor(mid))
        widgets['zero_button'].grid(row=row_index, column=notes_col + 1, padx=5, pady=10)

        widgets["absolute_entry"].configure(font=self.readout_font, justify="center")
        widgets["relative_entry"].configure(justify="center")
        for key in ("speed_entry", "target_entry"):
            if key in widgets:
                widgets[key].configure(justify="right")

        # Store references to widgets for cleanup
        visible_widgets.extend([
            widgets['absolute_entry'], widgets['relative_entry'], widgets['notes_entry'], widgets['zero_button']
        ])
        widgets['all_widgets'] = visible_widgets
        self.apply_row_selection_style(widgets, state.is_selected)

        return widgets

    def rebuild_motor_rows(self):
        """Rebuild all motor row widgets."""
        for child in self.motors_container.winfo_children():
            child.destroy()
        self.motor_widgets.clear()

        if self.is_step_mode():
            min_widths = Config.STEP_COL_MIN_WIDTHS
        elif self.is_program_mode():
            min_widths = Config.PROGRAM_COL_MIN_WIDTHS
        else:
            min_widths = Config.COL_MIN_WIDTHS
        max_columns = max(
            len(Config.COL_MIN_WIDTHS),
            len(Config.STEP_COL_MIN_WIDTHS),
            len(Config.PROGRAM_COL_MIN_WIDTHS),
        )
        for i in range(max_columns):
            self.motors_container.grid_columnconfigure(i, weight=0, minsize=0)
        for i, min_w in enumerate(min_widths):
            self.motors_container.grid_columnconfigure(i, weight=1, minsize=min_w)

        self.build_header_row()

        # Recreate rows starting from index 1 (0 is header)
        for i, state in enumerate(self._app.motor_states):
            widgets = self.create_motor_row(state, row_index=i + 1)
            self.motor_widgets.append(widgets)

        self.style_widget_tree(self.motors_container)
        for state, widgets in zip(self._app.motor_states, self.motor_widgets):
            self.apply_row_selection_style(widgets, state.is_selected)
        self.rebuild_compact_rows()
        self.fit_window_to_content()

    def visible_column_count(self) -> int:
        """Return the number of visible columns in the active motor table."""
        if self.is_step_mode():
            return len(Config.STEP_COL_MIN_WIDTHS)
        if self.is_program_mode():
            return len(Config.PROGRAM_COL_MIN_WIDTHS)
        return len(Config.COL_MIN_WIDTHS)

    def is_step_mode(self) -> bool:
        """Return whether the GUI is currently in step mode."""
        return bool(self.mode_var and self.mode_var.get() == Config.MODE_STEP)

    def is_program_mode(self) -> bool:
        """Return whether the GUI is currently in program mode."""
        return bool(self.mode_var and self.mode_var.get() == Config.MODE_PROGRAM)

    def refresh_mode_buttons(self):
        """Update segmented mode button relief to match the selected mode."""
        current = self.mode_var.get() if self.mode_var else Config.MODE_TARGET
        for mode, button in self.mode_buttons.items():
            selected = mode == current
            try:
                button.configure(style="Selected.Console.TButton" if selected else "Console.TButton")
            except tk.TclError:
                pass

    def update_mode_visibility(self):
        """Show controls for the selected movement mode."""
        if hasattr(self, 'compact_run_btn'):
            self.compact_run_btn.configure(state='disabled' if self.is_step_mode() else 'normal')
        if not (
            self.target_controls_frame
            and self.step_controls_frame
            and self.step_settings_frame
            and self.program_controls_frame
        ):
            return

        if self.is_step_mode():
            self.target_controls_frame.pack_forget()
            self.program_controls_frame.pack_forget()
            self.pack_control_frame(self.step_settings_frame, pady=(0, 10))
            self.pack_control_frame(self.step_controls_frame, pady=(0, 15))
        elif self.is_program_mode():
            self.target_controls_frame.pack_forget()
            self.step_settings_frame.pack_forget()
            self.step_controls_frame.pack_forget()
            self.pack_control_frame(self.program_controls_frame, pady=(0, 15))
        else:
            self.step_settings_frame.pack_forget()
            self.step_controls_frame.pack_forget()
            self.program_controls_frame.pack_forget()
            self.pack_control_frame(self.target_controls_frame, pady=(0, 15))

    def pack_control_frame(self, frame: tk.Frame, **pack_options):
        """Keep input options above the table and actions in their footer row."""
        if self.motors_container and frame.master == self.motors_container.master:
            frame.pack(before=self.motors_container, **pack_options)
        else:
            frame.pack(**pack_options)

    def apply_row_selection_style(self, widgets: dict, selected: bool):
        """Visually mark rows selected for group movement commands."""
        bg = Theme.COLOR_ROW_SELECTED_BG if selected else Theme.COLOR_BG

        for frame in widgets.get('row_frames', []):
            try:
                frame.configure(background=bg)
            except tk.TclError:
                pass

        for key in ('motor_label', 'checkbox', 'motion_status'):
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
        if hasattr(self, "selection_text"):
            count = sum(state.is_selected for state in self._app.motor_states)
            self.selection_text.set(f"{count} of {len(self._app.motor_states)} drives selected  /  Movement commands apply to selected drives")

    def bind_numeric_stepper(self, entry: tk.Entry, variable: tk.StringVar,
                              increment: float, decimals: int,
                              min_value=None, max_value=None,
                              on_change: Callable = None):
        """Bind Up/Down keys to increment a numeric entry."""
        entry.bind(
            '<Up>',
            lambda event: self.adjust_numeric_entry(
                variable, increment, decimals, min_value, max_value, on_change
            )
        )
        entry.bind(
            '<Down>',
            lambda event: self.adjust_numeric_entry(
                variable, -increment, decimals, min_value, max_value, on_change
            )
        )

    def adjust_numeric_entry(self, variable: tk.StringVar, delta: float,
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

    def bind_vertical_tab(self, entry: tk.Entry, motor_id: int, widget_key: str):
        """Bind Tab and Shift-Tab to move vertically within a table column."""
        entry.bind(
            '<Tab>',
            lambda event: self.focus_vertical_entry(motor_id, widget_key, direction=1)
        )
        entry.bind(
            '<Shift-Tab>',
            lambda event: self.focus_vertical_entry(motor_id, widget_key, direction=-1)
        )

    def focus_vertical_entry(self, motor_id: int, widget_key: str, direction: int):
        """Move focus vertically, then horizontally at column boundaries."""
        next_id = motor_id + direction

        while 1 <= next_id <= len(self.motor_widgets):
            widgets = self.get_widgets(next_id)
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
        next_id = 1 if direction > 0 else len(self.motor_widgets)

        while 1 <= next_id <= len(self.motor_widgets):
            widgets = self.get_widgets(next_id)
            if widgets and next_widget_key in widgets:
                widgets[next_widget_key].focus_set()
                widgets[next_widget_key].select_range(0, tk.END)
                return "break"
            next_id += direction

        return "break"

    def get_widgets(self, motor_id: int) -> Optional[dict]:
        """Get widget dictionary for motor by ID."""
        idx = motor_id - 1
        if 0 <= idx < len(self.motor_widgets):
            return self.motor_widgets[idx]
        return None
