"""Dialog windows used by the OpenDrive GUI."""

from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .config import Config
from .models import InstructionStep
from .utils import format_mm


class ProgramLoadWindow(tk.Toplevel):
    """Popup window for loading a program CSV from disk or recent files."""

    def __init__(self, parent: tk.Tk, recent_programs: list[str], on_load: Callable[[str], None]):
        super().__init__(parent)

        self._on_load = on_load
        self._recent_programs = recent_programs
        self._recent_var = tk.StringVar(value=recent_programs[0] if recent_programs else "")

        self.title("Load Program")
        self.resizable(False, False)

        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"+{parent_x + parent_width + 10}+{parent_y}")

        frame = tk.Frame(self, padx=16, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)

        top_row = tk.Frame(frame)
        top_row.pack(fill=tk.X)
        tk.Label(top_row, text="Choose a New Program:", font=("", 14, "bold")).pack(side=tk.LEFT)
        tk.Button(top_row, text="Choose File", command=self._choose_file, width=12).pack(side=tk.LEFT, padx=(18, 6))
        tk.Button(top_row, text="Cancel", command=self.destroy, width=10).pack(side=tk.LEFT)

        tk.Label(
            frame, text="Load a Recent Program:", font=("", 14, "bold")
        ).pack(anchor='w', pady=(22, 8))

        self._recent_combo = ttk.Combobox(
            frame,
            textvariable=self._recent_var,
            values=recent_programs,
            state="readonly" if recent_programs else "disabled",
            width=54,
        )
        self._recent_combo.pack(fill=tk.X)

        bottom_row = tk.Frame(frame)
        bottom_row.pack(anchor='e', pady=(18, 0))
        tk.Button(
            bottom_row,
            text="Load",
            command=self._load_recent,
            width=12,
            state=tk.NORMAL if recent_programs else tk.DISABLED,
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(bottom_row, text="Cancel", command=self.destroy, width=10).pack(side=tk.LEFT)

    def _choose_file(self):
        filepath = filedialog.askopenfilename(
            parent=self,
            title="Load Program",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if filepath:
            self._on_load(filepath)
            self.destroy()

    def _load_recent(self):
        filepath = self._recent_var.get()
        if filepath:
            self._on_load(filepath)
            self.destroy()


class NotesWindow(tk.Toplevel):
    """Popup window for editing motor notes."""

    def __init__(self, parent: tk.Tk, motor_id: int, current_notes: str, 
                 on_save: Callable[[int, str], None]):
        """
        Initialize NotesWindow.
        
        Args:
            parent: Parent Tk window
            motor_id: ID of motor being edited
            current_notes: Current notes content
            on_save: Callback(motor_id, notes) when window closes
        """
        super().__init__(parent)

        self._motor_id = motor_id
        self._on_save = on_save

        self.title(f"Notes for Motor {motor_id}")
        self.geometry("300x200")
        self.resizable(False, False)

        # Position window to the right of parent
        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"+{parent_x + parent_width + 10}+{parent_y}")

        self._text = tk.Text(self, wrap=tk.WORD, font=('TkDefaultFont', 10))
        self._text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self._text.insert('1.0', current_notes)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """Handle window close event."""
        notes = self._text.get('1.0', tk.END).strip()
        self._on_save(self._motor_id, notes)
        self.destroy()


class SettingsWindow(tk.Toplevel):
    """Popup window for editing global drive settings."""

    def __init__(self, parent: tk.Tk, min_speed: float, max_speed: float,
                 backlash: float, on_save: Callable[[float, float, float], None]):
        """Initialize SettingsWindow."""
        super().__init__(parent)

        self._on_save = on_save

        self.title("Settings")
        self.resizable(False, False)

        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"+{parent_x + parent_width + 10}+{parent_y}")

        frame = tk.Frame(self, padx=15, pady=15)
        frame.pack(fill=tk.BOTH, expand=True)

        self._min_speed_var = tk.StringVar(value=f"{min_speed:g}")
        self._max_speed_var = tk.StringVar(value=f"{max_speed:g}")
        self._backlash_var = tk.StringVar(value=format_mm(backlash))

        fields = [
            ("Minimum speed (mm/s):", self._min_speed_var),
            ("Maximum speed (mm/s):", self._max_speed_var),
            ("Backlash (mm):", self._backlash_var),
        ]

        for row, (label, variable) in enumerate(fields):
            tk.Label(frame, text=label, anchor='w').grid(row=row, column=0, sticky='w', pady=4)
            tk.Entry(frame, textvariable=variable, width=Config.ENTRY_WIDTH).grid(
                row=row, column=1, sticky='ew', padx=(10, 0), pady=4
            )

        button_frame = tk.Frame(frame)
        button_frame.grid(row=len(fields), column=0, columnspan=2, pady=(12, 0))

        tk.Button(button_frame, text="Save", command=self._on_save_click, width=8).pack(side=tk.LEFT, padx=2)
        tk.Button(button_frame, text="Cancel", command=self.destroy, width=8).pack(side=tk.LEFT, padx=2)

    def _on_save_click(self):
        """Validate and save settings."""
        try:
            min_speed = float(self._min_speed_var.get())
            max_speed = float(self._max_speed_var.get())
            backlash = float(self._backlash_var.get())
        except ValueError:
            messagebox.showwarning("Invalid Input", "Settings must be numeric.", parent=self)
            return

        if min_speed <= 0:
            messagebox.showwarning("Invalid Input", "Minimum speed must be greater than 0.", parent=self)
            return
        if max_speed < min_speed:
            messagebox.showwarning("Invalid Input", "Maximum speed must be greater than or equal to minimum speed.", parent=self)
            return
        if backlash < 0:
            messagebox.showwarning("Invalid Input", "Backlash must be 0 or greater.", parent=self)
            return

        backlash = round(backlash, 3)
        self._backlash_var.set(format_mm(backlash))
        self._on_save(min_speed, max_speed, backlash)
        self.destroy()


class InstructionsWindow(tk.Toplevel):
    """Popup window for editing one motor's program-mode instructions."""

    def __init__(self, parent: tk.Tk, motor_id: int, instructions: list[InstructionStep],
                 min_speed: float, max_speed: float,
                 on_save: Callable[[int, list[InstructionStep]], None]):
        """Initialize InstructionsWindow."""
        super().__init__(parent)

        self._motor_id = motor_id
        self._min_speed = min_speed
        self._max_speed = max_speed
        self._on_save = on_save
        self._rows: list[dict] = []

        self.title(f"Instructions for Motor {motor_id}")
        self.resizable(True, True)

        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"560x420+{parent_x + parent_width + 10}+{parent_y}")
        self.minsize(460, 280)

        frame = tk.Frame(self, padx=12, pady=12)
        frame.pack(fill=tk.BOTH, expand=True)

        table_container = tk.Frame(frame)
        table_container.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(table_container, highlightthickness=0)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = tk.Scrollbar(table_container, orient=tk.VERTICAL, command=self._canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.configure(yscrollcommand=scrollbar.set)

        table = tk.Frame(self._canvas)
        self._table = table
        self._table_window = self._canvas.create_window((0, 0), window=table, anchor='nw')
        table.bind('<Configure>', lambda _event: self._sync_scroll_region())
        self._canvas.bind(
            '<Configure>',
            lambda event: self._canvas.itemconfigure(self._table_window, width=event.width)
        )

        headers = ["Step", "Target (mm)", "Speed (mm/s)", "Notes"]
        for col, text in enumerate(headers):
            tk.Label(table, text=text, anchor='w').grid(row=0, column=col, sticky='w', padx=4, pady=(0, 6))

        for step in instructions:
            self._add_row(step)
        if not instructions:
            self._add_row(InstructionStep())

        button_frame = tk.Frame(frame)
        button_frame.pack(pady=(12, 0))
        tk.Button(button_frame, text="Add Step", command=self._add_empty_row, width=10).pack(side=tk.LEFT, padx=2)
        tk.Button(button_frame, text="Remove Last", command=self._remove_last_row, width=10).pack(side=tk.LEFT, padx=2)
        tk.Button(button_frame, text="Save", command=self._on_save_click, width=8).pack(side=tk.LEFT, padx=(12, 2))
        tk.Button(button_frame, text="Cancel", command=self.destroy, width=8).pack(side=tk.LEFT, padx=2)

    def _sync_scroll_region(self):
        """Keep the instruction table scroll region in sync with its contents."""
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _add_empty_row(self):
        self._add_row(InstructionStep())

    def _add_row(self, step: InstructionStep):
        row_index = len(self._rows) + 1
        target_var = tk.StringVar(value=format_mm(step.target) if step.target else "")
        speed_var = tk.StringVar(value=f"{step.speed:g}" if step.speed else "")
        notes_var = tk.StringVar(value=step.notes)

        step_label = tk.Label(self._table, text=str(row_index), width=4, anchor='w')
        step_label.grid(row=row_index, column=0, sticky='w', padx=4, pady=2)
        target_entry = tk.Entry(self._table, textvariable=target_var, width=Config.ENTRY_WIDTH)
        target_entry.grid(row=row_index, column=1, sticky='ew', padx=4, pady=2)
        speed_entry = tk.Entry(self._table, textvariable=speed_var, width=Config.ENTRY_WIDTH)
        speed_entry.grid(row=row_index, column=2, sticky='ew', padx=4, pady=2)
        notes_entry = tk.Entry(self._table, textvariable=notes_var, width=24)
        notes_entry.grid(row=row_index, column=3, sticky='ew', padx=4, pady=2)

        self._rows.append({
            'target_var': target_var,
            'speed_var': speed_var,
            'notes_var': notes_var,
            'widgets': [step_label, target_entry, speed_entry, notes_entry],
        })
        self.after_idle(self._sync_scroll_region)

    def _remove_last_row(self):
        if not self._rows:
            return
        row_index = len(self._rows)
        row = self._rows.pop()
        for widget in row['widgets']:
            widget.destroy()
        self.after_idle(self._sync_scroll_region)

    def _on_save_click(self):
        instructions: list[InstructionStep] = []
        for index, row in enumerate(self._rows, start=1):
            try:
                target = round(float(row['target_var'].get()), 3)
                speed = float(row['speed_var'].get())
            except ValueError:
                messagebox.showwarning(
                    "Invalid Instructions",
                    f"Step {index} target and speed must be numeric.",
                    parent=self,
                )
                return

            if not (Config.MIN_TARGET_POS <= target <= Config.MAX_TARGET_POS):
                messagebox.showwarning(
                    "Invalid Instructions",
                    f"Step {index} target must be between {Config.MIN_TARGET_POS} and {Config.MAX_TARGET_POS} mm.",
                    parent=self,
                )
                return
            if not (self._min_speed <= speed <= self._max_speed):
                messagebox.showwarning(
                    "Invalid Instructions",
                    f"Step {index} speed must be between {self._min_speed} and {self._max_speed} mm/s.",
                    parent=self,
                )
                return

            instructions.append(InstructionStep(
                target=target,
                speed=speed,
                notes=row['notes_var'].get().strip(),
            ))

        self._on_save(self._motor_id, instructions)
        self.destroy()
