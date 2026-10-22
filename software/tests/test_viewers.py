"""Two-window regression checks with synthetic MRI and no hardware or saved state."""
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np

from monkeydrive.anatomy import AnatomyVolume, AnatomySliceRenderer
from monkeydrive.anatomy_viewer import AnatomyViewer
from monkeydrive.models import MotorState, TrajectoryState
from monkeydrive.trajectory_viewer import TrajectoryViewer
from monkeydrive.trajectory_summary_viewer import TrajectorySummaryViewer


class ViewerIntegrationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk desktop unavailable: {error}')
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        data = np.indices((48, 48, 48)).sum(axis=0).astype(np.float32)
        affine = np.eye(4)
        affine[:3, 3] = -24
        self.volume = AnatomyVolume(data, Path('synthetic.nii'), 'NIfTI', affine, (1, 1, 1))
        self.loader = patch('monkeydrive.anatomy.AnatomyLoader.load', return_value=self.volume)
        self.loader.start()
        trajectory = TrajectoryState(probe_location_set=True, probe_motor_position=10, probe_offset=0,
                                     target_location_set=True, target_offset=-5)
        self.state = MotorState(1, absolute=10, trajectory=trajectory)
        self.saves = []
        self.detail = TrajectoryViewer(self.root, 1, trajectory, 10, 'synthetic.nii', '', '',
                                       lambda motor, value: self.saves.append(value.to_dict()))
        self.summary = TrajectorySummaryViewer(self.root, [self.state], 'synthetic.nii')
        self.detail.withdraw()
        self.summary.withdraw()
        self.wait_for(lambda: len(self.detail._image_refs) == 3 and 1 in self.summary._image_refs)

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('Viewer condition timed out')
            self.root.update()
            time.sleep(.002)
        self.root.update()

    def tearDown(self):
        if not hasattr(self, 'loader'):
            return
        self.loader.stop()
        for job in self.root.tk.call('after', 'info'):
            self.root.after_cancel(job)
        self.root.destroy()
        self.assertEqual(self.errors, [])

    @staticmethod
    def blue_line(canvas, tag):
        return next(canvas.coords(item) for item in canvas.find_withtag(tag)
                    if canvas.type(item) == 'line' and canvas.itemcget(item, 'fill') == '#1f6fff')

    def test_both_probe_overlays_move_without_resampling_summary_mri(self):
        detail_before = self.blue_line(self.detail._canvases[0], 'trajectory')
        summary_before = self.blue_line(self.summary._canvases[1], 'summary_overlay')
        with patch.object(self.summary._render_worker, 'submit', wraps=self.summary._render_worker.submit) as submit:
            for value in range(101):
                position = 10 + value / 100
                self.detail.update_motor_position(position)
                self.summary.update_motor_position(1, position)
            self.assertEqual(submit.call_count, 0)
        self.assertEqual(self.detail._motor_position, 11)
        self.assertEqual(self.summary._motor_states[1].absolute, 11)
        self.assertNotEqual(detail_before, self.blue_line(self.detail._canvases[0], 'trajectory'))
        self.assertNotEqual(summary_before, self.blue_line(self.summary._canvases[1], 'summary_overlay'))

    def test_unlocked_probe_scroll_publishes_new_position_to_summary(self):
        self.detail._trajectory.probe_location_set = False
        self.detail._on_perpendicular_scroll(SimpleNamespace(delta=120))
        self.assertEqual(self.saves[-1]['perpendicular_offset'], .5)
        self.state.trajectory = TrajectoryState.from_dict(self.saves[-1])
        self.summary.update_motor_state(self.state)
        self.assertEqual(self.summary._active_depth_offset(self.summary._motor_states[1]), .5)

    def test_slow_mri_render_does_not_block_window_events_or_probe_updates(self):
        entered, release = threading.Event(), threading.Event()
        original = AnatomySliceRenderer.oblique_slice_to_ppm
        def slow(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)
        try:
            with patch.object(AnatomySliceRenderer, 'oblique_slice_to_ppm', side_effect=slow):
                self.detail._trajectory.x = 1
                self.detail._render_all()
                self.assertTrue(entered.wait(1))
                tick = []
                self.root.after(5, lambda: tick.append(True))
                self.summary.update_motor_position(1, 12)
                self.detail.update_motor_position(12)
                self.wait_for(lambda: tick, timeout=.5)
                self.assertEqual(self.detail._motor_position, 12)
                self.assertEqual(self.summary._motor_states[1].absolute, 12)
                release.set()
                self.wait_for(lambda: self.detail._source_signatures.get(0) == self.detail._render_signatures[0][0])
        finally:
            release.set()

    def test_zoom_reuses_sampled_slices(self):
        with patch.object(AnatomySliceRenderer, 'oblique_slice_to_ppm', wraps=AnatomySliceRenderer.oblique_slice_to_ppm) as render:
            self.state.trajectory.zoom = 2
            self.summary.update_motor_state(self.state)
            self.detail._zoom_var.set(2)
            self.detail._on_zoom_change()
            old_width = self.detail._image_refs[0].width()
            self.wait_for(lambda: self.detail._image_refs[0].width() > old_width)
            self.assertEqual(render.call_count, 0)

    def test_anatomy_loading_is_background_work(self):
        entered, release = threading.Event(), threading.Event()
        anatomy = AnatomyViewer(self.root)
        anatomy.withdraw()
        def slow(_path):
            entered.set()
            release.wait(2)
            return self.volume
        try:
            with patch('monkeydrive.anatomy.AnatomyLoader.load', side_effect=slow):
                anatomy._load_path('synthetic.nii')
                self.assertTrue(entered.wait(1))
                tick = []
                self.root.after(5, lambda: tick.append(True))
                self.wait_for(lambda: tick, timeout=.5)
                release.set()
                self.wait_for(lambda: len(anatomy._image_refs) == 3)
        finally:
            release.set()
            anatomy.destroy()



if __name__ == '__main__':
    unittest.main()
