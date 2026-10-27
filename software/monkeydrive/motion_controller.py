"""Movement rules and program execution, independent of Tk and port ownership.

The application supplies plain field values, drive models, current speed bounds,
and a non-blocking command sender. Optional notifications let the application
refresh program readouts and trajectory geometry without putting widgets here.
All calls run on the application's thread; SerialManager owns device I/O.
"""
from dataclasses import dataclass
import math
from typing import Callable, Iterable, Sequence

from .config import Config
from .models import InstructionStep, MotorState
from .protocol import SerialProtocol


class MotionError(ValueError):
    """A rejected action, with presentation-neutral details for the caller."""

    def __init__(self, title: str, message: str, *, informational: bool = False):
        super().__init__(message)
        self.title = title
        self.informational = informational


@dataclass(frozen=True)
class TargetMove:
    """One drive and its target-mode inputs, before numeric validation."""

    motor: MotorState
    speed: str | float
    target: str | float


class MotionController:
    """Queue movement and maintain transient destinations and program state.

    Queue acceptance is not a controller acknowledgement or physical feedback.
    motion_targets drives the Running badge; step_targets tracks transient
    step/jog destinations without replacing a drive's saved target.
    """

    def __init__(self, send: Callable[[str], bool], connected: Callable[[], bool], *,
                 on_target_parsed: Callable[[MotorState], None] = lambda _state: None,
                 on_instruction_activated: Callable[[MotorState], None] = lambda _state: None,
                 on_program_changed: Callable[[MotorState], None] = lambda _state: None):
        self._send = send
        self._connected = connected
        self._on_target_parsed = on_target_parsed
        self._on_instruction_activated = on_instruction_activated
        self._on_program_changed = on_program_changed
        self.motion_targets: dict[int, float] = {}
        self.step_targets: dict[int, float] = {}

    def require_ready(self, motors: Sequence[MotorState]):
        if not self._connected():
            raise MotionError('Not Connected', 'Console not connected.')
        if not motors:
            raise MotionError('No Selection', 'Select at least one motor.', informational=True)

    def queue_motion(self, state: MotorState, command: str) -> bool:
        """Track an accepted RUN destination, without claiming acknowledgement."""
        accepted = self._send(command)
        if accepted:
            _name, args = SerialProtocol.parse_response(command)
            target = float(args[2])
            if abs(target - state.absolute) >= .0005:
                self.motion_targets[state.motor_id] = target
            else:
                self.motion_targets.pop(state.motor_id, None)
        else:
            self.motion_targets.pop(state.motor_id, None)
        return accepted

    def run_targets(self, moves: Sequence[TargetMove], min_speed: float, max_speed: float):
        """Validate every selected drive before queueing any movement."""
        self.require_ready([move.motor for move in moves])
        for move in moves:
            state = move.motor
            try:
                state.speed = float(move.speed)
            except (ValueError, TypeError) as error:
                raise MotionError('Invalid Input', f'Invalid speed input for Motor {state.motor_id}.') from error
            try:
                state.target = round(float(move.target), 3)
                self._on_target_parsed(state)
            except (ValueError, TypeError) as error:
                raise MotionError('Invalid Input', f'Invalid target input for Motor {state.motor_id}.') from error
            if not min_speed <= state.speed <= max_speed:
                raise MotionError('Invalid Input',
                    f'Speed must be between {min_speed}mm/s and {max_speed}mm/s. See Motor {state.motor_id}.')
            if not Config.MIN_TARGET_POS <= state.target <= Config.MAX_TARGET_POS:
                raise MotionError('Invalid Input',
                    f'Target must be between {Config.MIN_TARGET_POS}mm and {Config.MAX_TARGET_POS}mm. See Motor {state.motor_id}.')
            if not Config.MIN_ABSOLUTE_POS <= state.absolute <= Config.MAX_ABSOLUTE_POS:
                raise MotionError('Invalid Input',
                    f'Absolute position must be between {Config.MIN_ABSOLUTE_POS}mm and {Config.MAX_ABSOLUTE_POS}mm. See Motor {state.motor_id}.')
        for move in moves:
            state = move.motor
            self._start(state, state.speed, state.target)

    @staticmethod
    def parse_step_settings(speed_input: str | float, size_input: str | float,
                            min_speed: float, max_speed: float) -> tuple[float, float]:
        try:
            speed = float(speed_input)
        except (ValueError, TypeError) as error:
            raise MotionError('Invalid Input', 'Invalid step speed input.') from error
        try:
            size = float(size_input)
        except (ValueError, TypeError) as error:
            raise MotionError('Invalid Input', 'Invalid step size input.') from error
        if not min_speed <= speed <= max_speed:
            raise MotionError('Invalid Input', f'Step speed must be between {min_speed}mm/s and {max_speed}mm/s.')
        if not math.isfinite(size) or size < Config.MIN_STEP_SIZE:
            raise MotionError('Invalid Input', f'Step size must be at least {Config.MIN_STEP_SIZE:g}mm.')
        return speed, round(size, 3)

    def step(self, motors: Sequence[MotorState], direction: int, speed: float, size: float):
        """Use validated shared settings; preserve each drive's saved target."""
        self.require_ready(motors)
        updates = []
        for state in motors:
            target = round(state.absolute + direction * size, 3)
            if not Config.MIN_TARGET_POS <= target <= Config.MAX_TARGET_POS:
                raise MotionError('Invalid Step',
                    f'Motor {state.motor_id} step target would be {target:.3f}mm, '
                    f'outside the {Config.MIN_TARGET_POS}-{Config.MAX_TARGET_POS}mm range.')
            updates.append((state, target))
        for state, target in updates:
            self.step_targets[state.motor_id] = target
            self._start(state, speed, target)

    def jog(self, motors: Sequence[MotorState], direction: int, speed: float):
        """Queue a held gesture using its captured drives and validated speed."""
        self.require_ready(motors)
        target = Config.MAX_TARGET_POS if direction > 0 else Config.MIN_TARGET_POS
        for state in motors:
            self.step_targets[state.motor_id] = target
            self._start(state, speed, target)

    def _start(self, state: MotorState, speed: float, target: float):
        command = SerialProtocol.cmd_run(state.motor_id, speed, target, state.absolute)
        state.is_running = self.queue_motion(state, command)

    def pause(self, motor_ids: Iterable[int], motors: Iterable[MotorState]):
        """Pause explicit IDs even if selection/running flags are stale."""
        states = {state.motor_id: state for state in motors}
        for motor_id in set(motor_ids):
            self._send(SerialProtocol.cmd_pause(motor_id))
            state = states.get(motor_id)
            if state is not None:
                self._mark_paused(state)
            self.step_targets.pop(motor_id, None)
            self.motion_targets.pop(motor_id, None)

    def pause_all(self, motors: Iterable[MotorState]):
        self._send(SerialProtocol.cmd_pause_all())
        for state in motors:
            self._mark_paused(state)
        self.motion_targets.clear()
        self.step_targets.clear()

    def _mark_paused(self, state: MotorState):
        state.is_running = False
        if state.instruction_status == 'running':
            state.instruction_status = 'paused'
            self._on_program_changed(state)

    def is_moving(self, state: MotorState) -> bool:
        """Badge state follows the queued destination and existing GUI flags."""
        if not self._connected() or not state.is_running:
            self.motion_targets.pop(state.motor_id, None)
        return state.motor_id in self.motion_targets

    def apply_position(self, state: MotorState, absolute: float):
        target = self.motion_targets.get(state.motor_id)
        if target is not None and abs(absolute - target) < .0005:
            self.motion_targets.pop(state.motor_id, None)
        state.absolute = round(absolute, 3)
        state.relative = round(state.absolute - state.relative_zero_point, 3)
        if not state.is_running:
            self.step_targets.pop(state.motor_id, None)

    @staticmethod
    def current_instruction(state: MotorState) -> InstructionStep | None:
        if not state.instructions:
            return None
        index = max(0, min(state.instruction_index, len(state.instructions) - 1))
        return state.instructions[index]

    def activate_instruction(self, state: MotorState, instruction: InstructionStep):
        state.speed = instruction.speed
        state.target = round(instruction.target, 3)
        self._on_instruction_activated(state)

    def run_program(self, motors: Sequence[MotorState], min_speed: float, max_speed: float):
        self.require_ready(motors)
        runnable = []
        for state in motors:
            if not state.instructions:
                raise MotionError('Missing Instructions', f'Motor {state.motor_id} has no instructions.')
            if state.instruction_status == 'complete':
                continue
            instruction = self.current_instruction(state)
            if instruction is None:
                continue
            if not min_speed <= instruction.speed <= max_speed:
                raise MotionError('Invalid Instructions', f'Motor {state.motor_id} instruction speed is outside the current settings range.')
            if not Config.MIN_TARGET_POS <= instruction.target <= Config.MAX_TARGET_POS:
                raise MotionError('Invalid Instructions', f'Motor {state.motor_id} instruction target is outside the allowed range.')
            runnable.append((state, instruction))
        for state, instruction in runnable:
            self.activate_instruction(state, instruction)
            self._start(state, state.speed, state.target)
            state.instruction_status = 'running' if state.is_running else 'paused'
            self._on_program_changed(state)

    def advance_program(self, state: MotorState):
        """Advance only a running program whose active target was reported."""
        if state.instruction_status != 'running' or not state.instructions:
            return
        instruction = self.current_instruction(state)
        if instruction is None:
            return
        if abs(state.absolute - instruction.target) > Config.PROGRAM_TARGET_TOLERANCE_MM:
            return
        if state.instruction_index + 1 >= len(state.instructions):
            state.is_running = False
            state.instruction_status = 'complete'
            self._on_program_changed(state)
            return
        state.instruction_index += 1
        next_instruction = self.current_instruction(state)
        if next_instruction:
            self.activate_instruction(state, next_instruction)
        self._on_program_changed(state)
        if not next_instruction or not self._connected():
            return
        self._start(state, state.speed, state.target)
        if not state.is_running:
            state.instruction_status = 'paused'
            self._on_program_changed(state)
