"""Movement policy tests using plain models and a command recorder: no Tk/port."""
import unittest

from monkeydrive.models import InstructionStep, MotorState
from monkeydrive.motion_controller import MotionController, MotionError, TargetMove
from monkeydrive.protocol import SerialProtocol as P


class MotionControllerTests(unittest.TestCase):
    def setUp(self):
        self.connected = True
        self.accept = True
        self.commands = []
        self.notifications = []
        self.motion = MotionController(
            send=self.send, connected=lambda: self.connected,
            on_target_parsed=lambda state: self.notifications.append(('target', state.motor_id)),
            on_instruction_activated=lambda state: self.notifications.append(('instruction', state.motor_id)),
            on_program_changed=lambda state: self.notifications.append(('program', state.motor_id)),
        )

    def send(self, command):
        self.commands.append(command)
        return self.accept

    def test_target_commands_match_selected_inputs_and_current_absolute_positions(self):
        first, second = MotorState(1, absolute=4), MotorState(2, absolute=12)
        self.motion.run_targets([TargetMove(first, '.01', '5.1254'), TargetMove(second, .02, 10)], .001, .2)
        self.assertEqual(self.commands, [P.cmd_run(1, .01, 5.125, 4), P.cmd_run(2, .02, 10, 12)])
        self.assertEqual(first.target, 5.125)
        self.assertTrue(first.is_running)
        self.assertTrue(second.is_running)
        self.assertEqual(self.motion.motion_targets, {1: 5.125, 2: 10})

    def test_invalid_later_target_prevents_every_command(self):
        first, second = MotorState(1, absolute=2), MotorState(2, absolute=3)
        with self.assertRaises(MotionError):
            self.motion.run_targets([TargetMove(first, .01, 5), TargetMove(second, .01, 51)], .001, .2)
        self.assertEqual(self.commands, [])
        self.assertFalse(first.is_running)
        self.assertFalse(second.is_running)

    def test_bad_speed_target_and_position_are_rejected_without_movement(self):
        for speed, target, absolute in [
            ('invalid', 2, 1), (.0001, 2, 1), (.3, 2, 1), ('nan', 2, 1), ('inf', 2, 1),
            (.01, 'invalid', 1), (.01, -1, 1), (.01, 51, 1), (.01, 'nan', 1),
            (.01, 2, -1), (.01, 2, 51), (.01, 2, float('nan')),
        ]:
            with self.subTest(speed=speed, target=target, absolute=absolute):
                with self.assertRaises(MotionError):
                    self.motion.run_targets([TargetMove(MotorState(1, absolute=absolute), speed, target)], .001, .2)
                self.assertEqual(self.commands, [])

    def test_connection_and_selection_errors_do_not_send_commands(self):
        self.connected = False
        with self.assertRaises(MotionError) as failure:
            self.motion.run_targets([TargetMove(MotorState(1), .01, 1)], .001, .2)
        self.assertEqual(failure.exception.title, 'Not Connected')
        self.connected = True
        with self.assertRaises(MotionError) as failure:
            self.motion.run_targets([], .001, .2)
        self.assertTrue(failure.exception.informational)
        self.assertEqual(self.commands, [])

    def test_rejected_queue_submission_does_not_show_motion(self):
        state = MotorState(1, absolute=1)
        self.motion.run_targets([TargetMove(state, .01, 2)], .001, .2)
        self.accept = False
        self.motion.run_targets([TargetMove(state, .01, 3)], .001, .2)
        self.assertFalse(state.is_running)
        self.assertFalse(self.motion.is_moving(state))
        self.assertNotIn(1, self.motion.motion_targets)

    def test_step_preserves_saved_target_and_waits_for_position_feedback(self):
        state = MotorState(1, absolute=10, relative=2, relative_zero_point=8, target=30)
        speed, size = self.motion.parse_step_settings('.01', '.001', .001, .2)
        self.motion.step([state], 1, speed, size)
        self.assertEqual(self.commands, [P.cmd_run(1, .01, 10.001, 10)])
        self.assertEqual(state.target, 30)
        self.assertEqual((state.absolute, state.relative), (10, 2))
        self.motion.apply_position(state, 10.001)
        self.assertEqual(state.relative, 2.001)
        self.assertFalse(self.motion.is_moving(state))
        self.assertEqual(state.target, 30)

    def test_step_out_of_range_on_one_drive_prevents_all_steps(self):
        first, second = MotorState(1, absolute=2), MotorState(2, absolute=50)
        with self.assertRaises(MotionError) as failure:
            self.motion.step([first, second], 1, .01, .001)
        self.assertEqual(failure.exception.title, 'Invalid Step')
        self.assertEqual(self.commands, [])
        self.assertEqual(self.motion.step_targets, {})

    def test_step_settings_preserve_minimum_and_rounding(self):
        self.assertEqual(self.motion.parse_step_settings('.01', '.005', .001, .2), (.01, .005))
        self.assertEqual(self.motion.parse_step_settings('.01', '.0014', .001, .2), (.01, .001))
        for speed, size in [('bad', '.001'), ('.01', 'bad'), ('.01', 'nan'), ('.01', 'inf'),
                            ('.01', '0'), ('.01', '.0001'), ('.3', '.01')]:
            with self.subTest(speed=speed, size=size), self.assertRaises(MotionError):
                self.motion.parse_step_settings(speed, size, .001, .2)

    def test_jog_uses_captured_drives_instead_of_current_selection(self):
        first = MotorState(1, absolute=10, target=25, is_selected=False)
        second = MotorState(2, absolute=20, is_selected=True)
        self.motion.jog([first], -1, .01)
        self.assertEqual(self.commands, [P.cmd_run(1, .01, 0, 10)])
        self.assertEqual(first.target, 25)
        self.assertFalse(second.is_running)
        self.assertEqual(self.motion.step_targets, {1: 0})

    def test_pause_uses_explicit_ids_even_when_flags_are_false_or_state_is_missing(self):
        first, second = MotorState(1, is_running=False), MotorState(2, is_running=True)
        self.motion.motion_targets.update({1: 10, 2: 20})
        self.motion.pause([1, 3], [first, second])
        self.assertEqual({P.parse_response(command)[1][0] for command in self.commands}, {'1', '3'})
        self.assertTrue(second.is_running)
        self.assertEqual(self.motion.motion_targets, {2: 20})

    def test_pause_all_stops_all_models_and_programs_without_selection(self):
        first = MotorState(1, is_selected=False, is_running=True, instruction_status='running')
        hidden = MotorState(6, is_selected=False, is_running=False, instruction_status='running')
        self.motion.motion_targets[1] = 4
        self.motion.step_targets[6] = 50
        self.motion.pause_all([first, hidden])
        self.assertEqual(self.commands, [P.cmd_pause_all()])
        self.assertFalse(first.is_running)
        self.assertEqual((first.instruction_status, hidden.instruction_status), ('paused', 'paused'))
        self.assertEqual(self.motion.motion_targets, {})
        self.assertEqual(self.motion.step_targets, {})

    def test_position_during_backlash_does_not_clear_running_badge(self):
        state = MotorState(1, absolute=4)
        self.motion.run_targets([TargetMove(state, .01, 5)], .001, .2)
        self.motion.apply_position(state, 4)
        self.assertTrue(self.motion.is_moving(state))
        self.motion.apply_position(state, 5)
        self.assertFalse(self.motion.is_moving(state))
        self.assertEqual(self.commands, [P.cmd_run(1, .01, 5, 4)])
        # Preserve existing status semantics; acknowledging completion is a
        # separate change from extracting movement code out of the GUI.
        self.assertTrue(state.is_running)

    def program(self):
        return MotorState(1, absolute=1, instructions=[InstructionStep(2, .01), InstructionStep(3, .02)])

    def test_program_starts_advances_and_completes_from_reported_positions(self):
        state = self.program()
        self.motion.run_program([state], .001, .2)
        self.assertEqual(self.commands, [P.cmd_run(1, .01, 2, 1)])
        self.assertEqual(state.instruction_status, 'running')
        self.motion.apply_position(state, 1.5)
        self.motion.advance_program(state)
        self.assertEqual(len(self.commands), 1)
        self.motion.apply_position(state, 2)
        self.motion.advance_program(state)
        self.assertEqual(self.commands[-1], P.cmd_run(1, .02, 3, 2))
        self.assertEqual(state.instruction_index, 1)
        self.motion.apply_position(state, 3)
        self.motion.advance_program(state)
        self.assertEqual(state.instruction_status, 'complete')
        self.assertFalse(state.is_running)
        self.motion.run_program([state], .001, .2)
        self.assertEqual(len(self.commands), 2)

    def test_program_validates_every_drive_before_activating_any_instruction(self):
        for second in (MotorState(2), MotorState(2, instructions=[InstructionStep(51, .01)]),
                       MotorState(2, instructions=[InstructionStep(3, .3)])):
            with self.subTest(second=second), self.assertRaises(MotionError):
                self.motion.run_program([self.program(), second], .001, .2)
            self.assertEqual(self.commands, [])
            self.assertEqual(self.notifications, [])

    def test_program_failed_next_command_becomes_paused(self):
        state = self.program()
        self.motion.run_program([state], .001, .2)
        self.accept = False
        self.motion.apply_position(state, 2)
        self.motion.advance_program(state)
        self.assertEqual(state.instruction_status, 'paused')
        self.assertFalse(state.is_running)
        self.assertEqual(state.instruction_index, 1)

    def test_paused_program_does_not_advance_until_explicit_run(self):
        state = self.program()
        self.motion.run_program([state], .001, .2)
        self.motion.pause([1], [state])
        self.motion.apply_position(state, 2)
        self.motion.advance_program(state)
        self.assertEqual(state.instruction_index, 0)
        self.assertEqual(len(self.commands), 2)  # RUN, PAUSE
        self.motion.run_program([state], .001, .2)
        self.assertEqual(self.commands[-1], P.cmd_run(1, .01, 2, 2))

    def test_program_notifies_geometry_before_refreshing_progress(self):
        state = self.program()
        self.motion.run_program([state], .001, .2)
        self.assertEqual(self.notifications, [('instruction', 1), ('program', 1)])
        self.notifications.clear()
        self.motion.apply_position(state, 2)
        self.motion.advance_program(state)
        self.assertEqual(self.notifications, [('instruction', 1), ('program', 1)])
