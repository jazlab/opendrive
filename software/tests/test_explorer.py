"""Synthetic MRI checks for the embedded anatomy Explorer tab."""
import gc
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from monkeydrive.anatomy import AnatomyVolume, AnatomySliceRenderer
from monkeydrive.trajectories_viewer import TrajectoriesViewer
from monkeydrive.models import MotorState
from unittest.mock import patch


class ExplorerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.saved = []
        self.assignments = []
        data = np.indices((32, 32, 32)).sum(axis=0).astype(np.float32)
        affine = np.eye(4)
        affine[:3, 3] = -16
        self.volume = AnatomyVolume(data, Path('synthetic.nii'), 'NIfTI', affine, (1, 1, 1))
        self.loader = patch('monkeydrive.anatomy.AnatomyLoader.load', return_value=self.volume)
        self.load = self.loader.start()
        self.viewer = TrajectoriesViewer(self.root, [MotorState(1), MotorState(3)], 'synthetic.nii',
            on_explorer_change=self.saved.append,
            on_assign=lambda drive, state: self.assignments.append((drive, state)))
        self.viewer.withdraw()
        self.explorer = self.viewer._explorer
        self.wait_for(lambda: len(self.explorer._image_refs) == 3)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('Explorer render timed out')
            self.root.update()
            time.sleep(.002)

    def tearDown(self):
        if not hasattr(self, 'viewer'):
            return
        self.loader.stop()
        self.root.update_idletasks()
        self.viewer.destroy()
        self.root.destroy()
        self.explorer = self.viewer = self.root = None
        gc.collect()
        self.assertEqual(self.errors, [])

    def test_embedded_tab_renders_all_three_planes_with_shared_origin(self):
        self.wait_for(lambda: len(self.explorer._image_refs) == 3)
        self.assertEqual(self.viewer._notebook.tab(self.explorer, 'text'), 'Explorer')
        np.testing.assert_allclose(self.explorer._origin, [-.5, -.5, -.5])
        for axis in range(3):
            geometry = self.explorer._slice_geometry(axis)
            np.testing.assert_allclose(geometry[0], self.explorer._origin)
            self.assertEqual(len(self.explorer._canvases[axis].find_withtag('crosshair')), 2)

    def test_scroll_updates_corresponding_coordinate_and_saved_state(self):
        for axis in range(3):
            old = self.explorer._origin.copy()
            self.explorer._scroll_origin(axis, SimpleNamespace(delta=120, num=None))
            expected = old.copy()
            expected[axis] += .5
            np.testing.assert_allclose(self.explorer._origin, expected)
            self.explorer._scroll_origin(axis, SimpleNamespace(delta=0, num=5))
            np.testing.assert_allclose(self.explorer._origin, old)
        state = self.explorer.get_state()
        for axis, key in enumerate(('x', 'y', 'z')):
            self.assertEqual(state[key], float(self.explorer._origin_vars[axis].get()))
        self.assertEqual(self.saved[-1], state)

    def test_angles_rotate_orthogonal_planes_about_fixed_origin(self):
        old = self.explorer._origin.copy()
        for key, value in (('ap_rotation', -27.9), ('ml_rotation', 28.4), ('twist_rotation', -1.3)):
            self.explorer._angle_vars[key].set(str(value))
            self.explorer._commit_angle(key)
            self.assertAlmostEqual(getattr(self.explorer._trajectory, key), value)
            self.assertAlmostEqual(self.explorer._angle_scales[key].get(), value)
        basis = np.array(self.explorer._trajectory_basis())
        np.testing.assert_allclose(basis @ basis.T, np.eye(3), atol=1e-10)
        np.testing.assert_allclose(self.explorer._origin, old)
        self.explorer._angle_vars['ap_rotation'].set('nan')
        self.explorer._commit_angle('ap_rotation')
        self.assertEqual(self.explorer._trajectory.ap_rotation, -27.9)

    def test_oblique_scroll_is_perpendicular_to_the_rendered_plane(self):
        from monkeydrive.anatomy import AnatomyOrientation
        for orientation in ('', AnatomyOrientation.PRONE_HEAD_FIRST):
            self.explorer._orientation_actual_key = orientation
            self.explorer._orientation_scanner_key = AnatomyOrientation.SUPINE_HEAD_FIRST if orientation else ''
            for angles in ((-27.9, 28.4, -1.3), (90, 45, 90), (0, 0, 45)):
                for key, value in zip(('ap_rotation', 'ml_rotation', 'twist_rotation'), angles):
                    self.explorer._rotate(key, value)
                for axis in range(3):
                    with self.subTest(orientation=orientation, angles=angles, axis=axis):
                        old_origin = self.explorer._origin.copy()
                        center, horizontal, vertical, *_ = self.explorer._slice_geometry(axis)
                        self.explorer._scroll_origin(axis, SimpleNamespace(delta=120, num=None))
                        new_center = self.explorer._slice_geometry(axis)[0]
                        movement = np.array(new_center) - center
                        self.assertAlmostEqual(np.linalg.norm(movement), .5)
                        self.assertAlmostEqual(np.dot(movement, horizontal), 0)
                        self.assertAlmostEqual(np.dot(movement, vertical), 0)
                        for plane in range(3):
                            np.testing.assert_allclose(self.explorer._slice_geometry(plane)[0], new_center)
                        self.explorer._scroll_origin(axis, SimpleNamespace(delta=0, num=5))
                        np.testing.assert_allclose(self.explorer._origin, old_origin, atol=1e-12)
                self.assertEqual(tuple(getattr(self.explorer._trajectory, key) for key in
                                       ('ap_rotation', 'ml_rotation', 'twist_rotation')), angles)

    def test_hidden_tab_does_not_schedule_render_and_controls_coalesce(self):
        self.wait_for(lambda: len(self.explorer._image_refs) == 3)
        for value in range(100):
            self.explorer._rotate('twist_rotation', value)
        job = self.explorer._render_job
        self.assertIsNotNone(job)
        self.explorer._rotate('twist_rotation', 100)
        self.assertEqual(self.explorer._render_job, job)
        self.viewer.select_tab('summary')
        self.wait_for(lambda: self.explorer._render_job is None)
        self.explorer._rotate('twist_rotation', 90)
        self.assertIsNone(self.explorer._render_job)
        self.viewer.select_tab('explorer')
        self.assertIsNotNone(self.explorer._render_job)

    def test_origin_and_angles_restore_without_touching_source_volume(self):
        from monkeydrive.explorer_view import ExplorerView
        state = dict(x=1.1, y=2.2, z=3.3, ap_rotation=4.4, ml_rotation=5.5, twist_rotation=6.6, zoom=1.5)
        restored = ExplorerView(self.viewer._notebook, self.viewer, state)
        original = self.volume.data.copy()
        restored.set_volume(self.volume)
        self.assertEqual(restored.get_state(), state)
        np.testing.assert_array_equal(self.volume.data, original)
        restored.destroy()

    def test_segmented_buttons_switch_pages_and_show_selection(self):
        self.viewer._buttons['summary'].invoke()
        self.root.update()
        self.assertEqual(self.viewer._notebook.index(self.viewer._notebook.select()), 1)
        self.assertEqual(self.viewer._buttons['summary'].cget('style'), 'Selected.Viewer.TButton')
        self.viewer._buttons['explorer'].invoke()
        self.root.update()
        self.assertEqual(self.viewer._buttons['explorer'].cget('style'), 'Selected.Viewer.TButton')
        self.assertTrue(self.explorer._active)

    def test_assign_button_copies_current_geometry_to_chosen_drive(self):
        self.explorer._drive_var.set('Drive 3')
        for variable, value in zip(self.explorer._origin_vars, ('1.25', '2.5', '3.75')):
            variable.set(value)
        for key, value in (('ap_rotation', -27.9), ('ml_rotation', 28.4), ('twist_rotation', -1.3)):
            self.explorer._angle_vars[key].set(str(value))
        self.explorer._assign_button.invoke()
        drive, state = self.assignments[-1]
        self.assertEqual(drive, 3)
        self.assertEqual((state['x'], state['y'], state['z']), (1.25, 2.5, 3.75))
        self.assertEqual((state['ap_rotation'], state['ml_rotation'], state['twist_rotation']), (-27.9, 28.4, -1.3))
        self.explorer.set_drive_ids([1])
        self.assertEqual(self.explorer._drive_var.get(), 'Drive 1')
        self.explorer.set_drive_ids([])
        self.assertTrue(self.explorer._assign_button.instate(['disabled']))

    def test_drive_views_are_lazy_and_share_one_loaded_mri(self):
        self.assertEqual(self.viewer._selected, 'explorer')
        self.assertEqual(self.viewer._panels, {})
        self.viewer.update_motor_position(1, 17.25)
        self.viewer.select_tab(1)
        detail = self.viewer._panels[1]
        self.wait_for(lambda: len(detail._image_refs) == 3)
        self.assertIs(detail._volume, self.explorer._volume)
        self.assertEqual(detail._motor_position, 17.25)
        self.viewer.select_tab('summary')
        summary = self.viewer._panels['summary']
        self.wait_for(lambda: len(summary._image_refs) == 2)
        self.assertIs(summary._volume, detail._volume)
        self.assertEqual(summary._motor_states[1].absolute, 17.25)
        self.assertEqual(self.load.call_count, 1)

    def test_hidden_drive_stays_current_without_submitting_renders(self):
        self.viewer.select_tab(1)
        detail = self.viewer._panels[1]
        self.wait_for(lambda: len(detail._image_refs) == 3)
        self.viewer.select_tab('explorer')
        with patch.object(detail._render_worker, 'submit') as submit:
            self.viewer.update_motor_position(1, 18.5)
            state = MotorState.from_dict(self.viewer._states[1].to_dict())
            state.trajectory.z = 7.5
            self.viewer.update_motor_state(state)
            self.assertEqual(detail._motor_position, 18.5)
            self.assertEqual(detail._trajectory.z, 7.5)
            submit.assert_not_called()
        self.viewer.select_tab(1)
        self.assertTrue(detail._active)
        self.assertEqual(detail._motor_position, 18.5)
        self.assertEqual(detail._z_var.get(), '7.5')

    def test_drive_roster_and_assignment_select_correct_tab(self):
        from monkeydrive.models import TrajectoryState
        self.viewer.select_tab(3)
        self.viewer.set_motor_states([MotorState(1), MotorState(2)])
        self.root.update()
        self.assertNotIn(3, self.viewer._pages)
        self.assertIn(2, self.viewer._pages)
        self.viewer.apply_trajectory(2, TrajectoryState(x=1, y=2, z=3), revision=1, select=True)
        self.assertEqual(self.viewer._selected, 2)
        self.assertEqual(self.viewer._panels[2]._trajectory_origin(), (1, 2, 3))
        self.viewer.apply_trajectory(2, TrajectoryState(), revision=0)
        self.assertEqual(self.viewer._states[2].trajectory.z, 3)

    def test_locked_probe_uses_latest_hidden_position_when_tab_is_reopened(self):
        state = self.viewer._states[1]
        state.absolute = 10
        state.trajectory.probe_location_set = True
        state.trajectory.probe_motor_position = 10
        self.viewer.select_tab(1)
        detail = self.viewer._panels[1]
        self.wait_for(lambda: len(detail._image_refs) == 3)
        self.viewer.select_tab('explorer')
        self.viewer.update_motor_position(1, 12)
        self.viewer.select_tab(1)
        self.assertEqual(float(detail._offset_var.get()), -2)
        np.testing.assert_allclose(detail._slice_center_world(detail._trajectory_basis()[2]), [0, 0, -2])
        self.viewer.select_tab('summary')
        self.assertEqual(self.viewer._panels['summary']._motor_states[1].absolute, 12)

    def test_continuous_slider_drag_displays_frames_before_drag_ends(self):
        original_render = self.explorer._reslicer.grayscale
        original_draw = self.explorer._draw_plane
        initial = dict(self.explorer._source_signatures)
        frames = []
        moves = []
        def slow_render(*args, **kwargs):
            time.sleep(.07)  # Slower than the 50 ms request cadence.
            return original_render(*args, **kwargs)
        def draw(axis, image):
            # All three images and source geometries must be installed together.
            self.assertEqual(set(self.explorer._image_refs), {0, 1, 2})
            centers = [self.explorer._source_signatures[a][3] for a in range(3)]
            self.assertEqual(centers[0], centers[1])
            self.assertEqual(centers[1], centers[2])
            frames.append((len(moves), axis, self.explorer._source_signatures[axis]))
            original_draw(axis, image)
        def move():
            moves.append(len(moves))
            self.explorer._rotate('ap_rotation', 10 + len(moves) * .5)
            if len(moves) < 60:
                self.root.after(10, move)
        with patch.object(self.explorer._reslicer, 'grayscale', side_effect=slow_render), \
                patch.object(self.explorer, '_draw_plane', side_effect=draw):
            move()
            self.wait_for(lambda: len(moves) == 60)
            changing_frames = [frame for frame in frames if frame[0] < 60 and frame[2] != initial[frame[1]]]
            self.assertGreaterEqual(len(changing_frames), 2)
            self.assertLessEqual(len(self.explorer._render_worker._pending), 1)
            self.wait_for(lambda: self.explorer._render_job is None and not self.explorer._frame_in_flight and all(
                self.explorer._source_signatures.get(axis) == self.explorer._render_signatures[axis][0]
                for axis in range(3)))
        self.assertEqual(self.explorer._trajectory.ap_rotation, 40)
        for axis in range(3):
            expected = tuple(tuple(v) if hasattr(v, '__iter__') else v for v in self.explorer._slice_geometry(axis))
            self.assertEqual(self.explorer._source_signatures[axis][3:], expected)

    def test_zoom_reuses_all_three_full_resolution_sources(self):
        sources = dict(self.explorer._source_images)
        with patch.object(self.explorer._reslicer, 'grayscale', side_effect=AssertionError('Zoom must reuse slices')):
            self.explorer._adjust_zoom(.1)
            self.wait_for(lambda: self.explorer._render_job is None and not self.explorer._frame_in_flight)
        for axis in range(3):
            self.assertIs(self.explorer._source_images[axis], sources[axis])
            self.assertEqual(self.explorer._image_refs[axis].width(), round(sources[axis].width * 1.1))

    def test_hiding_discards_frame_in_flight(self):
        import threading
        started, release = threading.Event(), threading.Event()
        original = self.explorer._reslicer.grayscale
        def blocked(*args, **kwargs):
            started.set()
            release.wait(2)
            return original(*args, **kwargs)
        with patch.object(self.explorer._reslicer, 'grayscale', side_effect=blocked):
            self.explorer._rotate('ap_rotation', 31)
            self.wait_for(started.is_set)
            self.viewer.select_tab('summary')
            before = dict(self.explorer._image_refs)
            release.set()
            self.wait_for(lambda: bool(self.viewer._panels['summary']._image_refs))
            self.assertEqual(self.explorer._image_refs, before)
            self.viewer.select_tab('explorer')
            self.wait_for(lambda: not self.explorer._frame_in_flight and self.explorer._render_job is None)
        self.assertFalse(self.explorer._frame_dirty)
