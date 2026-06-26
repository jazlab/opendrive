"""Serial connection management for MonkeyDrive."""

import logging
import threading
import time
from typing import Callable, Optional

import serial
import serial.tools.list_ports

from .config import Config
from .protocol import SerialProtocol

logger = logging.getLogger(__name__)

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
