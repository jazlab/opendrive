"""Application data models."""

from dataclasses import dataclass, asdict

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
