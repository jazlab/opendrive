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
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

import serial
import serial.tools.list_ports
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def format_mm(value: float) -> str:
    """Format a millimeter value to nearest micron."""
    return f"{value:.3f}"


# =============================================================================
# Configuration Constants
# =============================================================================

class Config:
    """Global configuration parameters for the MonkeyDrive system."""

    # Serial communication
    BAUD_RATE: int = 115200
    SERIAL_TIMEOUT: float = 0.1

    # Motor limits
    MAX_MOTORS: int = 6
    MIN_ABSOLUTE_POS: float = 0.0      # mm
    MAX_ABSOLUTE_POS: float = 50.0     # mm
    MIN_SPEED: float = 0.001           # mm/s (inclusive)
    MAX_SPEED: float = 0.2             # mm/s (inclusive)
    MIN_TARGET_POS: float = 0.0        # mm
    MAX_TARGET_POS: float = 50.0       # mm
    DEFAULT_BACKLASH_MM: float = 0.058

    # Timing intervals (seconds)
    HEARTBEAT_INTERVAL: float = 0.5
    HEARTBEAT_TIMEOUT: float = 1.0
    POSITION_UPDATE_INTERVAL: float = 0.2
    AUTO_SAVE_INTERVAL: float = 5.0
    SERIAL_READ_INTERVAL: float = 0.02

    # Data persistence
    DATA_FILE: str = "monkeydrive_state.json"

    # GUI Layout
    WINDOW_TITLE: str = "MonkeyDrive"
    DECIMAL_PLACES: int = 3
    ENTRY_WIDTH: int = 12
    MODE_TARGET: str = "target"
    MODE_STEP: str = "step"
    DEFAULT_STEP_SPEED: float = 0.01
    DEFAULT_STEP_SIZE: float = 0.05
    SPEED_KEY_INCREMENT: float = 0.001
    STEP_SPEED_KEY_INCREMENT: float = 0.01
    TARGET_KEY_INCREMENT: float = 0.1
    STEP_SIZE_KEY_INCREMENT: float = 0.01
    STEP_JOG_HOLD_DELAY_MS: int = 300
    STEP_KEY_RELEASE_DEBOUNCE_MS: int = 100
    COLOR_BG: str = "#f0f0f0"
    COLOR_FG: str = "#000000"
    COLOR_ENTRY_BG: str = "#ffffff"
    COLOR_READONLY_BG: str = "#d9d9d9"
    COLOR_ACTIVE_BG: str = "#e5e5e5"
    COLOR_SELECT_BG: str = "#4a90e2"

    # Column Weights and Min Sizes for Grid Layout
    # Col 0: Buttons/ID | Col 1: Speed | Col 2: Target | Col 3: Abs | Col 4: Rel | Col 5: Notes
    COL_MIN_WIDTHS: tuple = (120, 100, 100, 100, 100, 150)
    STEP_COL_MIN_WIDTHS: tuple = (120, 120, 120, 150)
    ROW_PAD_Y: int = 2
    HEADER_PAD_Y: tuple = (5, 10)


# =============================================================================
# Serial Communication Protocol
# =============================================================================

class SerialProtocol:
    """
    Serial communication protocol between GUI and Teensy console.
    
    All commands use XOR checksum for data integrity verification.
    Format: <command>,<arg1>,<arg2>,...*<checksum>
    """

    @staticmethod
    def calculate_checksum(data: str) -> str:
        """Calculate XOR checksum of data string and return as 2-digit hex."""
        checksum = 0
        for char in data:
            checksum ^= ord(char)
        return f"{checksum:02X}"

    @staticmethod
    def create_command(cmd: str, *args) -> str:
        """Create a command string with checksum."""
        if args:
            data = f"{cmd},{','.join(str(a) for a in args)}"
        else:
            data = cmd
        checksum = SerialProtocol.calculate_checksum(data)
        return f"{data}*{checksum}\n"

    @staticmethod
    def parse_response(response: str) -> Optional[tuple]:
        """
        Parse a response string and verify checksum.
        
        Returns:
            Tuple of (command, [args]) if valid, None if invalid.
        """
        response = response.strip()
        if not response or '*' not in response:
            return None

        try:
            data, received_checksum = response.rsplit('*', 1)
            expected_checksum = SerialProtocol.calculate_checksum(data)

            if received_checksum != expected_checksum:
                return None

            parts = data.split(',')
            return (parts[0], parts[1:])
        except (ValueError, IndexError):
            return None

    @staticmethod
    def cmd_run(motor_id: int, speed: float, target: float, absolute: float) -> str:
        """Create RUN command for motor movement."""
        return SerialProtocol.create_command(
            "RUN", motor_id, f"{speed:.6f}", f"{target:.3f}", f"{absolute:.3f}"
        )

    @staticmethod
    def cmd_pause(motor_id: int) -> str:
        """Create PAUSE command for a single motor."""
        return SerialProtocol.create_command("PAUSE", motor_id)

    @staticmethod
    def cmd_pause_all() -> str:
        """Create PAUSE_ALL command to stop all motors."""
        return SerialProtocol.create_command("PAUSE_ALL")

    @staticmethod
    def cmd_heartbeat() -> str:
        """Create HEARTBEAT command for connection monitoring."""
        return SerialProtocol.create_command("HEARTBEAT")

    @staticmethod
    def cmd_update_target(motor_id: int, target: float) -> str:
        """Create TARGET command to update motor target position."""
        return SerialProtocol.create_command("TARGET", motor_id, f"{target:.3f}")

    @staticmethod
    def cmd_config(min_speed: float, max_speed: float, backlash: float) -> str:
        """Create CONFIG command for runtime controller settings."""
        return SerialProtocol.create_command(
            "CONFIG", f"{min_speed:.6f}", f"{max_speed:.6f}", f"{backlash:.6f}"
        )


# =============================================================================
# Motor State Data Class
# =============================================================================

@dataclass
class MotorState:
    """
    State of a single motor/drive.
    
    Attributes:
        motor_id: Unique identifier (1-indexed)
        speed: Movement speed in mm/s
        target: Target position in mm
        absolute: Current absolute position in mm
        relative: Position relative to zero point in mm
        relative_zero_point: Reference point for relative position
        notes: User notes for this motor
        is_running: Whether motor is currently moving
        is_selected: Whether motor is selected in GUI
    """
    motor_id: int
    speed: float = 0.0
    target: float = 0.0
    absolute: float = 0.0
    relative: float = 0.0
    relative_zero_point: float = 0.0
    notes: str = ""
    is_running: bool = False
    is_selected: bool = False

    def to_dict(self) -> dict:
        """Convert state to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'MotorState':
        """Create MotorState from dictionary."""
        return cls(**data)


# =============================================================================
# Serial Manager
# =============================================================================

class SerialManager:
    """
    Manages serial communication with the Teensy console.
    
    Handles connection management, heartbeat monitoring, and position updates
    in background threads.
    """

    def __init__(self, on_position_update: Callable, on_connection_lost: Callable):
        """
        Initialize SerialManager.
        
        Args:
            on_position_update: Callback(motor_id, position) for position updates
            on_connection_lost: Callback() when connection is lost
        """
        self._serial_port: Optional[serial.Serial] = None
        self._port_name: str = ""
        self._is_connected: bool = False

        self._on_position_update = on_position_update
        self._on_connection_lost = on_connection_lost

        self._read_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running: bool = False

        self._last_heartbeat_received: float = 0.0
        self._lock = threading.Lock()
        self._connection_lost_notified: bool = False
        self._heartbeat_suspended: bool = False

    @property
    def is_connected(self) -> bool:
        """Return current connection status."""
        return self._is_connected

    @is_connected.setter
    def is_connected(self, value: bool):
        """Set connection status."""
        self._is_connected = value

    @property
    def heartbeat_suspended(self) -> bool:
        """Return whether heartbeat checking is suspended."""
        return self._heartbeat_suspended

    @heartbeat_suspended.setter
    def heartbeat_suspended(self, value: bool):
        """Set heartbeat suspension status."""
        self._heartbeat_suspended = value

    @property
    def last_heartbeat_received(self) -> float:
        """Return timestamp of last heartbeat."""
        return self._last_heartbeat_received

    @last_heartbeat_received.setter
    def last_heartbeat_received(self, value: float):
        """Set timestamp of last heartbeat."""
        self._last_heartbeat_received = value

    @staticmethod
    def list_ports() -> list:
        """Return list of available serial port names."""
        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports]

    def connect(self, port_name: str) -> bool:
        """
        Connect to the specified serial port.
        
        Args:
            port_name: Name of the serial port to connect to.
            
        Returns:
            True if connection successful, False otherwise.
        """
        try:
            self.disconnect()

            self._serial_port = serial.Serial(
                port=port_name,
                baudrate=Config.BAUD_RATE,
                timeout=Config.SERIAL_TIMEOUT
            )

            self._serial_port.reset_input_buffer()
            self._serial_port.reset_output_buffer()

            self._port_name = port_name
            self._is_connected = True
            self._running = True
            self._last_heartbeat_received = time.time()
            self._connection_lost_notified = False

            self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()

            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

            return True

        except serial.SerialException as e:
            logger.error("Connection error: %s", e)
            self._is_connected = False
            return False

    def disconnect(self):
        """Disconnect from the serial port and stop background threads."""
        self._running = False
        self._is_connected = False

        if self._serial_port and self._serial_port.is_open:
            try:
                self._serial_port.close()
            except serial.SerialException:
                pass

        self._serial_port = None
        self._port_name = ""

    def send(self, command: str) -> bool:
        """
        Send a command to the console.
        
        Args:
            command: Command string to send.
            
        Returns:
            True if send successful, False otherwise.
        """
        if not self._is_connected or not self._serial_port:
            return False

        try:
            with self._lock:
                self._serial_port.write(command.encode('utf-8'))
                self._serial_port.flush()
            return True
        except serial.SerialException as e:
            if self._running:
                logger.error("Send error: %s", e)
            return False

    def _read_loop(self):
        """Background thread loop for reading serial data."""
        buffer = ""
        while self._running:
            try:
                if (self._serial_port and 
                    self._serial_port.is_open and 
                    self._serial_port.in_waiting > 0):
                    
                    chunk = self._serial_port.read(
                        self._serial_port.in_waiting
                    ).decode('utf-8', errors='ignore')
                    buffer += chunk

                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if line:
                            self._handle_response(line)

            except (OSError, serial.SerialException) as e:
                if self._running:
                    logger.error("Device disconnected: %s", e)
                    self._running = False
                    self._is_connected = False
                    if not self._connection_lost_notified:
                        self._connection_lost_notified = True
                        self._on_connection_lost()
                break

            except Exception as e:
                if self._running:
                    logger.error("Read error: %s", e)

            time.sleep(Config.SERIAL_READ_INTERVAL)

    def _heartbeat_loop(self):
        """Background thread loop for heartbeat monitoring."""
        while self._running:
            if not self._heartbeat_suspended:
                self.send(SerialProtocol.cmd_heartbeat())

                elapsed = time.time() - self._last_heartbeat_received
                if elapsed > Config.HEARTBEAT_TIMEOUT:
                    if self._is_connected and not self._connection_lost_notified:
                        self._is_connected = False
                        self._connection_lost_notified = True
                        self._on_connection_lost()

            time.sleep(Config.HEARTBEAT_INTERVAL)

    def _handle_response(self, response: str):
        """Process a response from the console."""
        parsed = SerialProtocol.parse_response(response)
        if not parsed:
            return

        cmd, args = parsed

        if cmd == "HEARTBEAT_ACK":
            self._last_heartbeat_received = time.time()

        elif cmd == "POS" and len(args) >= 2:
            try:
                motor_id = int(args[0])
                position = float(args[1])
                self._on_position_update(motor_id, position)
            except (ValueError, IndexError):
                pass


# =============================================================================
# Notes Window
# =============================================================================

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
        self._saved_mode: str = Config.MODE_TARGET
        self._saved_step_speed: float = Config.DEFAULT_STEP_SPEED
        self._saved_step_size: float = Config.DEFAULT_STEP_SIZE
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
