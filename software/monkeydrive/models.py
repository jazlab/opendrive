"""Application data models."""

from dataclasses import dataclass, field, asdict


@dataclass
class InstructionStep:
    """One target/speed instruction for program mode."""

    target: float = 0.0
    speed: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict:
        """Convert instruction to a dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'InstructionStep':
        """Create InstructionStep from a dictionary."""
        return cls(
            target=float(data.get('target', data.get('target_mm', 0.0))),
            speed=float(data.get('speed', data.get('speed_mm_s', 0.0))),
            notes=str(data.get('notes', "")),
        )


@dataclass
class TrajectoryState:
    """MRI-space trajectory parameters for one motor."""

    x: float = 0.0
    y: float = 0.0
    ap_rotation: float = 0.0
    ml_rotation: float = 0.0
    twist_rotation: float = 0.0
    perpendicular_offset: float = 0.0
    zoom: float = 1.0
    probe_location_set: bool = False
    probe_offset: float = 0.0
    probe_motor_position: float = 0.0
    target_location_set: bool = False
    target_offset: float = 0.0
    probe_map_enabled: bool = False
    probe_map_probe: int = 0
    probe_map_min_mv: float = 0.0
    probe_map_max_mv: float = 2.5
    probe_map_update_ms: int = 500

    def to_dict(self) -> dict:
        """Convert trajectory to a dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'TrajectoryState':
        """Create TrajectoryState from a dictionary."""
        return cls(
            x=float(data.get('x', 0.0)),
            y=float(data.get('y', 0.0)),
            ap_rotation=float(data.get('ap_rotation', 0.0)),
            ml_rotation=float(data.get('ml_rotation', 0.0)),
            twist_rotation=float(data.get('twist_rotation', 0.0)),
            perpendicular_offset=float(data.get('perpendicular_offset', 0.0)),
            zoom=float(data.get('zoom', 1.0)),
            probe_location_set=bool(data.get('probe_location_set', False)),
            probe_offset=float(data.get('probe_offset', 0.0)),
            probe_motor_position=float(data.get('probe_motor_position', 0.0)),
            target_location_set=bool(data.get('target_location_set', False)),
            target_offset=float(data.get('target_offset', 0.0)),
            probe_map_enabled=bool(data.get('probe_map_enabled', False)),
            probe_map_probe=int(data.get('probe_map_probe', 0)),
            probe_map_min_mv=float(data.get('probe_map_min_mv', 0.0)),
            probe_map_max_mv=float(data.get('probe_map_max_mv', 2.5)),
            probe_map_update_ms=int(data.get('probe_map_update_ms', 500)),
        )

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
    trajectory: TrajectoryState = field(default_factory=TrajectoryState)
    instructions: list[InstructionStep] = field(default_factory=list)
    instruction_index: int = 0
    instruction_status: str = "idle"

    def to_dict(self) -> dict:
        """Convert state to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'MotorState':
        """Create MotorState from dictionary."""
        data = dict(data)
        trajectory = data.get('trajectory', {})
        if isinstance(trajectory, TrajectoryState):
            data['trajectory'] = trajectory
        else:
            data['trajectory'] = TrajectoryState.from_dict(trajectory)
        instructions = data.get('instructions', [])
        data['instructions'] = [
            step if isinstance(step, InstructionStep) else InstructionStep.from_dict(step)
            for step in instructions
        ]
        data['instruction_index'] = int(data.get('instruction_index', 0))
        data['instruction_status'] = str(data.get('instruction_status', "idle"))
        return cls(**data)
