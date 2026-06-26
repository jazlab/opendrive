"""Serial command formatting and response parsing."""

from typing import Optional

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
