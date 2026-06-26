"""Dialog windows used by the MonkeyDrive GUI."""

from typing import Callable

import tkinter as tk
from tkinter import messagebox

from .config import Config
from .utils import format_mm

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
