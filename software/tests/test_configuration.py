"""Workspace files preserve content and reject malformed replacements."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monkeydrive.configuration import atomic_write_json, read_config, validate_workspace, write_config
from monkeydrive.models import InstructionStep, MotorState, TrajectoryState


class ConfigurationTests(unittest.TestCase):
    def workspace(self):
        motor = MotorState(1, absolute=7.125, relative=-.5, relative_zero_point=7.625,
                           target=12, speed=.02, notes='Probe A\nSecond line', is_selected=True,
                           is_running=True, instruction_status='running', instruction_index=1,
                           instructions=[InstructionStep(8, .01, 'first'), InstructionStep(12, .02, 'second')],
                           trajectory=TrajectoryState(x=1, y=-3, z=4, ap_rotation=22, ml_rotation=-11,
                                                      twist_rotation=3, zoom=2, probe_location_set=True,
                                                      probe_motor_position=6, probe_offset=1,
                                                      target_location_set=True, target_offset=-4,
                                                      probe_map_enabled=True))
        return dict(motor_states=[motor.to_dict()], mode='program', port='COM9',
                    step_speed='.02', step_size='.001', min_speed=.001, max_speed=.2, backlash=.058,
                    anatomy_path='/data/subject.nii.gz', anatomy_orientation_actual='prone_head_first',
                    anatomy_orientation_scanner='supine_head_first',
                    explorer_state=dict(x=1, y=2, z=3, ap_rotation=45, ml_rotation=-20, twist_rotation=8, zoom=1.5),
                    recent_programs=['/data/program.csv'])

    def test_round_trip_preserves_workspace_but_never_resumes_motion(self):
        original = self.workspace()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.json'
            write_config(path, original)
            restored = read_config(path)
        expected = copy.deepcopy(original)
        expected['step_size'], expected['step_speed'] = .001, .02
        expected['motor_states'][0]['is_running'] = False
        expected['motor_states'][0]['instruction_status'] = 'paused'
        self.assertEqual(restored, expected)
        self.assertTrue(original['motor_states'][0]['is_running'])

    def test_bad_structure_ranges_and_nonfinite_values_are_rejected(self):
        changes = [dict(motor_states=[]), dict(motor_states=[{'motor_id': 2}]),
                   dict(motor_states=[{'motor_id': 1, 'absolute': 51}]),
                   dict(motor_states=[{'motor_id': 1, 'trajectory': {'x': 'nan'}}]),
                   dict(motor_states=[{'motor_id': 1, 'instructions': [None]}]),
                   dict(motor_states=[{'motor_id': 1, 'instruction_index': 2}]),
                   dict(step_size=0), dict(step_size=float('inf')), dict(min_speed=-1),
                   dict(max_speed=.0001), dict(backlash=-.1), dict(explorer_state={'zoom': 0}),
                   dict(explorer_state=[]), dict(anatomy_path=[]), dict(mode='unknown'),
                   dict(anatomy_orientation_actual='unknown')]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_workspace({**self.workspace(), **change})

    def test_unknown_version_or_unrelated_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bad.json'
            for data in ([], {}, {'format': 'monkeydrive-config', 'version': 99, 'workspace': self.workspace()}):
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ValueError):
                    read_config(path)

    def test_failed_replace_preserves_previous_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text('original', encoding='utf-8')
            with patch('monkeydrive.configuration.os.replace', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    write_config(path, self.workspace())
            self.assertEqual(path.read_text(), 'original')
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_automatic_state_keeps_legacy_format(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'monkeydrive_state.json'
            atomic_write_json(path, self.workspace())
            self.assertEqual(json.loads(path.read_text()), self.workspace())

    def test_hidden_drive_state_is_validated_and_restored_stopped(self):
        workspace = self.workspace()
        hidden = MotorState(2, absolute=20, relative=2, relative_zero_point=18,
                            notes='Hidden probe', is_running=True, is_selected=True).to_dict()
        workspace['hidden_motor_states'] = [hidden]
        restored = validate_workspace(workspace)
        self.assertEqual(restored['hidden_motor_states'][0]['absolute'], 20)
        self.assertFalse(restored['hidden_motor_states'][0]['is_running'])
        self.assertFalse(restored['hidden_motor_states'][0]['is_selected'])
        for invalid in (None, [dict(hidden, motor_id=1)], [dict(hidden, motor_id=7)],
                        [dict(hidden, absolute=51)], [hidden, hidden]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_workspace({**workspace, 'hidden_motor_states': invalid})
