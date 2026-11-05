"""Versioned workspace files, validated before they replace live state."""
import json
import math
import os
from pathlib import Path
import tempfile

from .config import Config
from .models import MotorState, TrajectoryState
from .probe_view_settings import probe_view_settings


FORMAT = 'opendrive-config'
VERSION = 1


def validate_workspace(data):
    """Return independent, normalized state; never restore running movement."""
    if not isinstance(data, dict):
        raise ValueError('The configuration must contain a workspace object.')
    # Reject non-finite values anywhere, including nested trajectory settings.
    try:
        data = json.loads(json.dumps(data, allow_nan=False))
    except (ValueError, TypeError) as error:
        raise ValueError('Configuration values must be valid, finite JSON values.') from error

    def number(value, name, minimum=None, maximum=None):
        if isinstance(value, bool):
            raise ValueError(f'{name} must be a number.')
        try:
            value = float(value)
        except (ValueError, TypeError) as error:
            raise ValueError(f'{name} must be a number.') from error
        if not math.isfinite(value) or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            raise ValueError(f'{name} is outside its permitted range.')
        return value

    def text(key, default=''):
        value = data.get(key, default)
        if not isinstance(value, str):
            raise ValueError(f'{key} must be text.')
        return value

    low = number(data.get('min_speed', Config.MIN_SPEED), 'Minimum speed', .000001)
    high = number(data.get('max_speed', Config.MAX_SPEED), 'Maximum speed', low)
    result = dict(data)
    result.update(min_speed=low, max_speed=high,
                  backlash=number(data.get('backlash', Config.DEFAULT_BACKLASH_MM), 'Backlash', 0),
                  step_speed=number(data.get('step_speed', Config.DEFAULT_STEP_SPEED), 'Step speed', .000001),
                  step_size=number(data.get('step_size', Config.DEFAULT_STEP_SIZE), 'Step size', Config.MIN_STEP_SIZE))
    mode = text('mode', Config.MODE_TARGET)
    if mode not in (Config.MODE_TARGET, Config.MODE_STEP, Config.MODE_PROGRAM):
        raise ValueError('Unknown movement mode.')
    result['mode'] = mode
    if 'compact_view' in data and type(data['compact_view']) is not bool:
        raise ValueError('Compact view must be true or false.')
    for key in ('port', 'anatomy_path', 'anatomy_orientation_actual', 'anatomy_orientation_scanner'):
        result[key] = text(key)
    from .anatomy import AnatomyOrientation
    actual = {spec.key for spec in AnatomyOrientation.actual_options()} | {''}
    scanner = {spec.key for spec in AnatomyOrientation.scanner_options()} | {''}
    if result['anatomy_orientation_actual'] not in actual or result['anatomy_orientation_scanner'] not in scanner:
        raise ValueError('Unknown anatomy orientation.')
    explorer = data.get('explorer_state', {})
    if not isinstance(explorer, dict):
        raise ValueError('Explorer settings must be an object.')
    result['explorer_state'] = {}
    for key in ('x', 'y', 'z', 'ap_rotation', 'ml_rotation', 'twist_rotation', 'zoom'):
        if key in explorer:
            bounds = (.5, 10) if key == 'zoom' else (-180, 180) if key.endswith('rotation') else (None, None)
            result['explorer_state'][key] = number(explorer[key], f'Explorer {key}', *bounds)
    recent = data.get('recent_programs', [])
    if not isinstance(recent, list) or not all(isinstance(path, str) for path in recent):
        raise ValueError('Recent programs must be a list of paths.')
    result['recent_programs'] = recent[:10]
    if 'probe_view_settings' in data:
        result['probe_view_settings'] = probe_view_settings(data['probe_view_settings'], strict=True)

    motors = data.get('motor_states')
    if not isinstance(motors, list) or not 1 <= len(motors) <= Config.MAX_MOTORS:
        raise ValueError(f'A configuration must contain 1–{Config.MAX_MOTORS} drives.')
    result['motor_states'] = []
    for index, motor in enumerate(motors, 1):
        if not isinstance(motor, dict) or type(motor.get('motor_id')) is not int or motor['motor_id'] != index:
            raise ValueError('Drive IDs must be consecutive, starting at 1.')
        try:
            state = MotorState.from_dict(motor)
            for key in ('absolute', 'target'):
                setattr(state, key, round(number(getattr(state, key), f'Drive {index} {key}', 0, 50), 3))
            for key in ('relative', 'relative_zero_point'):
                setattr(state, key, round(number(getattr(state, key), f'Drive {index} {key}'), 3))
            state.speed = number(state.speed, f'Drive {index} speed', 0)
            if not isinstance(state.notes, str) or type(state.is_selected) is not bool:
                raise ValueError('Drive notes and selection have invalid types.')
            for key, value in state.trajectory.to_dict().items():
                default = getattr(TrajectoryState(), key)
                if isinstance(default, bool):
                    if type(motor.get('trajectory', {}).get(key, default)) is not bool:
                        raise ValueError(f'Invalid trajectory flag: {key}.')
                else:
                    number(value, f'Drive {index} trajectory {key}')
            if not .5 <= state.trajectory.zoom <= 10:
                raise ValueError('Trajectory zoom must be between 0.5 and 10.')
            if not 10 <= state.trajectory.probe_map_smoothing_um <= 5000:
                raise ValueError('Activity smoothing width must be between 10 and 5000 µm.')
            if not 6 <= state.trajectory.probe_width_px <= 256:
                raise ValueError('Probe width must be between 6 and 256 pixels.')
            for step in state.instructions:
                step.target = number(step.target, 'Program target', 0, 50)
                step.speed = number(step.speed, 'Program speed', .000001)
            if not 0 <= state.instruction_index <= max(0, len(state.instructions) - 1):
                raise ValueError('Program step index is invalid.')
            if state.instruction_status not in ('idle', 'running', 'paused', 'complete'):
                raise ValueError('Program status is invalid.')
            state.is_running = False
            if state.instruction_status == 'running':
                state.instruction_status = 'paused'
        except (TypeError, AttributeError, ValueError, OverflowError) as error:
            raise ValueError(f'Invalid Drive {index}: {error}') from error
        result['motor_states'].append(state.to_dict())
    hidden = data.get('hidden_motor_states', [])
    if not isinstance(hidden, list):
        raise ValueError('Hidden drives must be a list.')
    if hidden:
        if not all(isinstance(item, dict) and type(item.get('motor_id')) is int for item in hidden):
            raise ValueError('Hidden drive IDs must be integers.')
        # Validate the retained suffix using exactly the same rules as visible
        # drives, including the total controller drive limit.
        hidden = sorted(hidden, key=lambda item: item['motor_id'])
        combined = validate_workspace({**data, 'motor_states': [*motors, *hidden], 'hidden_motor_states': []})
        result['hidden_motor_states'] = combined['motor_states'][len(motors):]
        for motor in result['hidden_motor_states']:
            motor['is_selected'] = False
    return result


def read_config(path):
    with open(path, encoding='utf-8-sig') as stream:
        document = json.load(stream)
    if not isinstance(document, dict) or document.get('format') not in (FORMAT, 'monkeydrive-config'):
        raise ValueError('This is not a OpenDrive configuration file.')
    if type(document.get('version')) is not int or document['version'] != VERSION:
        raise ValueError('This configuration version is not supported.')
    return validate_workspace(document.get('workspace'))


def atomic_write_json(path, data):
    """Replace only after a complete write, leaving an existing file on failure."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_config(path, workspace):
    validate_workspace(workspace)
    atomic_write_json(path, {'format': FORMAT, 'version': VERSION, 'workspace': workspace})
