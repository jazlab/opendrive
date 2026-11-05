"""Two-window regression checks with synthetic MRI and no hardware or saved state."""
from pathlib import Path
import gc
import threading
import time
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np

from opendrive.anatomy import AnatomyVolume, AnatomySliceRenderer
from opendrive.anatomy_viewer import AnatomyViewer
from opendrive.models import MotorState, TrajectoryState
from opendrive.trajectory_viewer import TrajectoryViewer
from opendrive.trajectory_summary_viewer import TrajectorySummaryViewer


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
        self.loader = patch('opendrive.anatomy.AnatomyLoader.load', return_value=self.volume)
        self.loader.start()
        self.discovery = patch('opendrive.trajectory_viewer._TrajectoryView._refresh_available_probes')
        self.discovery.start()
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
        self.discovery.stop()
        workers = [self.detail._render_worker, self.summary._render_worker]
        if self.detail._probe_discovery_worker:
            workers.append(self.detail._probe_discovery_worker)
        self.detail.destroy()
        self.summary.destroy()
        for worker in workers:
            worker._thread.join(2)
        for job in self.root.tk.call('after', 'info'):
            self.root.after_cancel(job)
        self.root.destroy()
        self.detail = self.summary = self.root = None
        workers = worker = None
        gc.collect()
        self.assertEqual(self.errors, [])

    @staticmethod
    def probe_outline(canvas, tag):
        return next(canvas.coords(item) for item in canvas.find_withtag(tag)
                    if canvas.type(item) == 'polygon' and 'probe_body' in canvas.gettags(item))

    def test_both_probe_overlays_move_without_resampling_summary_mri(self):
        detail_before = self.probe_outline(self.detail._canvases[0], 'trajectory')
        summary_before = self.probe_outline(self.summary._canvases[1], 'summary_overlay')
        with patch.object(self.summary._render_worker, 'submit', wraps=self.summary._render_worker.submit) as submit:
            for value in range(101):
                position = 10 + value / 100
                self.detail.update_motor_position(position)
                self.summary.update_motor_position(1, position)
            self.assertEqual(submit.call_count, 0)
        self.assertEqual(self.detail._motor_position, 11)
        self.assertEqual(self.summary._motor_states[1].absolute, 11)
        self.assertNotEqual(detail_before, self.probe_outline(self.detail._canvases[0], 'trajectory'))
        self.assertNotEqual(summary_before, self.probe_outline(self.summary._canvases[1], 'summary_overlay'))

    def test_unlocked_probe_scroll_publishes_new_position_to_summary(self):
        self.detail._trajectory.probe_location_set = False
        self.detail._on_perpendicular_scroll(SimpleNamespace(delta=120))
        self.assertEqual(self.saves[-1]['perpendicular_offset'], .5)
        self.state.trajectory = TrajectoryState.from_dict(self.saves[-1])
        self.summary.update_motor_state(self.state)
        self.assertEqual(self.summary._active_depth_offset(self.summary._motor_states[1]), .5)

    def test_probe_heatmaps_present_together_and_reuse_items_during_motion(self):
        detail = self.detail
        detail._trajectory.probe_map_enabled = True
        detail._probe_map_enabled_var.set(True)
        detail._probe_map_values_mv = [.05, .1, .2]
        detail._probe_map_depths_mm = [0, .5, 1]
        detail._redraw_probe_map_overlay_only()
        items = dict(detail._probe_map_items)
        self.assertEqual(set(items), {0, 1})
        old_palette = detail._probe_map_colors
        # Both PhotoImages must exist before either visible image is replaced.
        import opendrive.trajectory_viewer as module
        original_photo = module.ImageTk.PhotoImage
        prepared, presented = [], []
        def prepare(*args, **kwargs):
            image = original_photo(*args, **kwargs)
            prepared.append(image)
            return image
        def present(axis, original, item, **kwargs):
            if item == items[axis] and 'image' in kwargs:
                self.assertEqual(len(prepared), 2)
                self.assertEqual(set(detail._probe_map_image_refs), {0, 1})
                presented.append(axis)
            return original(item, **kwargs)
        first, second = detail._canvases[0], detail._canvases[1]
        originals = first.itemconfigure, second.itemconfigure
        with patch.object(module.ImageTk, 'PhotoImage', side_effect=prepare), \
                patch.object(first, 'itemconfigure', side_effect=lambda item, **kw: present(0, originals[0], item, **kw)), \
                patch.object(second, 'itemconfigure', side_effect=lambda item, **kw: present(1, originals[1], item, **kw)), \
                patch.object(module, 'color_from_lut', wraps=module.color_from_lut) as color:
            detail.update_motor_position(11)
            detail._cancel_probe_map_overlay_draw()
            detail._redraw_probe_map_overlay_only()
            color.assert_not_called()
        self.assertEqual(presented, [0, 1])
        self.assertEqual(detail._probe_map_items, items)
        self.assertIs(detail._probe_map_colors, old_palette)

    def test_probe_map_interval_commits_on_focus_out_and_survives_motion(self):
        detail = self.detail
        detail._probe_map_enabled_var.set(True)
        detail._probe_map_update_ms_var.set('1200')
        with patch.object(detail, '_restart_probe_map_worker') as restart:
            detail._commit_probe_map_restart_from_event()
            restart.assert_called_once()
        self.assertEqual(self.saves[-1]['probe_map_update_ms'], 1200)
        detail._probe_map_worker_running = True
        detail._probe_map_worker_settings = (0, 1200)
        with patch.object(detail, '_restart_probe_map_worker') as restart:
            detail._commit_probe_map_restart_from_event()  # Enter followed by FocusOut.
            for position in (10.1, 10.2, 10.3):
                detail.update_motor_position(position)
            detail._run_live_update()
            detail._probe_map_max_var.set('.5')
            detail._commit_probe_map_redraw_from_event()
            restart.assert_not_called()
        self.assertEqual(detail._trajectory.probe_map_update_ms, 1200)
        self.assertEqual(detail._probe_map_update_ms_var.get(), '1200')
        # Verify the binding on the actual input, not just the event handler.
        def children(widget):
            for child in widget.winfo_children():
                yield child
                yield from children(child)
        entry = next(widget for widget in children(detail) if isinstance(widget, tk.Entry)
                     and str(widget.cget('textvariable')) == str(detail._probe_map_update_ms_var))
        self.assertTrue(entry.bind('<FocusOut>'))
        detail._probe_map_worker_running = False

    def test_motor_movement_does_not_refresh_heatmap_between_activity_snapshots(self):
        detail = self.detail
        detail._trajectory.probe_map_enabled = True
        detail._trajectory.probe_map_update_ms = 1000
        detail._probe_map_values_mv = [.05, .1, .2]
        detail._probe_map_depths_mm = [0, .5, 1]
        detail._cancel_probe_map_overlay_draw()
        detail._redraw_probe_map_overlay_only()
        before_images = dict(detail._probe_map_image_refs)
        before_line = self.probe_outline(detail._canvases[0], 'trajectory')
        import opendrive.trajectory_viewer as module
        with patch.object(module.ImageTk, 'PhotoImage', wraps=module.ImageTk.PhotoImage) as photo:
            for position in (10.1, 10.2, 10.3, 10.4):
                detail.update_motor_position(position)
                detail._run_live_update()
                detail._render_all()  # A full state echo must not refresh cached maps either.
                self.root.update()
            # Perpendicular MRI may render, but the two heatmap PhotoImages stay
            # exactly the same. Only an activity snapshot may replace them.
            self.assertEqual(detail._probe_map_image_refs, before_images)
            self.assertIsNone(detail._probe_map_draw_job)
        self.assertNotEqual(before_line, self.probe_outline(detail._canvases[0], 'trajectory'))
        detail._handle_probe_map_event(('snapshot', detail._probe_map_generation,
            SimpleNamespace(peak_to_peak_mv=[.1, .2, .3], channel_depths_mm=[0, .5, 1], data_source='test')))
        self.root.update()
        self.assertTrue(all(detail._probe_map_image_refs[axis] is not before_images[axis] for axis in (0, 1)))

    def test_external_trajectory_refresh_keeps_probe_map_controls_in_sync(self):
        detail = self.detail
        trajectory = TrajectoryState.from_dict(detail._trajectory.to_dict())
        trajectory.probe_map_enabled = True
        trajectory.probe_map_probe = 2
        trajectory.probe_map_update_ms = 900
        trajectory.probe_map_max_mv = .3
        with patch.object(detail, '_restart_probe_map_worker') as restart:
            detail.update_trajectory(trajectory)
            restart.assert_called_once()
        self.assertEqual(detail._probe_map_probe_var.get(), '2')
        self.assertEqual(detail._probe_map_update_ms_var.get(), '900')
        self.assertEqual(detail._probe_map_max_var.get(), '0.3')
        self.assertEqual(detail._probe_map_probe_label_var.get(), 'imec2')

    def test_probe_dropdown_lists_only_discovered_probes_and_saves_numeric_selection(self):
        detail = self.detail
        detail._set_available_probes(4)
        self.assertEqual(str(detail._probe_map_combo.cget('state')), 'readonly')
        self.assertEqual(detail._probe_map_combo.cget('values'), ('imec0', 'imec1', 'imec2', 'imec3'))
        detail._probe_map_probe_label_var.set('imec2')
        detail._select_probe_map_probe()
        self.assertEqual(detail._trajectory.probe_map_probe, 2)
        self.assertEqual(self.saves[-1]['probe_map_probe'], 2)
        detail._set_available_probes(1)
        self.assertEqual(detail._probe_map_combo.cget('values'), ('imec0',))
        self.assertEqual(detail._trajectory.probe_map_probe, 2)
        self.assertEqual(detail._probe_map_probe_label_var.get(), 'imec2')

    def test_probe_discovery_does_not_block_tk(self):
        entered, release = threading.Event(), threading.Event()
        def connect():
            entered.set()
            release.wait(2)
            return SimpleNamespace(probe_count=3)
        from unittest.mock import Mock
        client = Mock()
        client.connect.side_effect = connect
        try:
            with patch('opendrive.trajectory_viewer.SpikeGLXLiveClient', return_value=client):
                self.discovery.temp_original(self.detail)
                self.assertTrue(entered.wait(1))
                ticks = []
                self.root.after(5, lambda: ticks.append(True))
                self.wait_for(lambda: bool(ticks), timeout=.5)
                self.assertTrue(self.detail._probe_discovery_pending)
                release.set()
                self.wait_for(lambda: not self.detail._probe_discovery_pending)
                self.assertEqual(self.detail._probe_map_combo.cget('values'), ('imec0', 'imec1', 'imec2'))
                client.close.assert_called_once()
        finally:
            release.set()

    def test_invalid_probe_map_edit_does_not_partially_change_settings(self):
        detail = self.detail
        before = detail._trajectory.to_dict()
        detail._probe_map_probe_var.set('4')
        detail._probe_map_min_var.set('nan')
        self.assertFalse(detail._apply_probe_map_fields(show_warning=False))
        self.assertEqual(detail._trajectory.to_dict(), before)

    def test_trajectory_heatmap_keeps_same_depth_contacts_in_separate_columns(self):
        detail = self.detail
        detail._probe_map_depths_mm = [10, 10, 20, 20]
        detail._probe_map_x_mm = [.0, .032, .0, .032]
        detail._probe_map_shanks = [0, 0, 0, 0]
        colors = ('#ff0000', '#0000ff', '#00ff00', '#ffff00')
        raster = detail._probe_map_image(100, 100, 90, colors)
        self.assertEqual(raster.width, 22)
        self.assertEqual(raster.getpixel((4, 80)), (255, 0, 0, 255))
        self.assertEqual(raster.getpixel((17, 80)), (0, 0, 255, 255))
        self.assertEqual(raster.getpixel((4, 70)), (0, 255, 0, 255))
        self.assertEqual(raster.getpixel((17, 70)), (255, 255, 0, 255))
        self.assertEqual(raster.getpixel((10, 80))[3], 0)
        detail._trajectory.probe_map_enabled = True
        detail._probe_map_values_mv = [.05, .1, .15, .2]
        detail._redraw_probe_map_overlay_only()
        self.assertEqual([detail._probe_map_image_refs[axis].width() for axis in (0, 1)], [24, 24])

    def test_smoothing_toggle_reuses_sampler_and_computes_once_for_both_planes(self):
        detail = self.detail
        detail._trajectory.probe_map_enabled = True
        detail._probe_map_enabled_var.set(True)
        detail._probe_map_values_mv = [.1, .2, .3, .4]
        detail._probe_map_depths_mm = [0, 0, .2, .2]
        detail._probe_map_x_mm = [0, .032, 0, .032]
        detail._probe_map_shanks = [0]*4
        detail._probe_map_worker_running = True
        detail._probe_map_worker_settings = (0, 500)
        with patch.object(detail, '_restart_probe_map_worker') as restart:
            detail._probe_map_smooth_var.set(True)
            detail._on_probe_map_settings_change()
            self.root.update()
            self.assertEqual([detail._probe_map_image_refs[axis].width() for axis in (0, 1)], [24, 24])
            self.assertTrue(self.saves[-1]['probe_map_smooth'])
            plan = detail._probe_smoother
            detail._probe_map_values_mv = [.2, .3, .4, .5]
            with patch.object(plan, 'smooth', wraps=plan.smooth) as smooth:
                detail._redraw_probe_map_overlay_only()
                self.assertEqual(smooth.call_count, 1)
                detail._redraw_probe_map_overlay_only()
                self.assertEqual(smooth.call_count, 1)
            detail._probe_map_smoothing_var.set('300')
            detail._on_probe_map_settings_change()
            self.root.update()
            self.assertIsNot(detail._probe_smoother, plan)
            self.assertEqual(self.saves[-1]['probe_map_smoothing_um'], 300)
            detail._probe_width_var.set('40')
            detail._on_probe_map_settings_change()
            self.root.update()
            self.assertEqual([detail._probe_map_image_refs[axis].width() for axis in (0, 1)], [40, 40])
            outline = self.probe_outline(detail._canvases[0], 'trajectory')
            self.assertEqual(max(outline[::2])-min(outline[::2]), 40)
            detail._x_var.set('1')
            self.assertTrue(detail._apply_fields(show_warning=False))
            self.assertTrue(detail._trajectory.probe_map_smooth)
            self.assertEqual(detail._trajectory.probe_map_smoothing_um, 300)
            detail._probe_map_smooth_var.set(False)
            detail._on_probe_map_settings_change()
            self.root.update()
            self.assertEqual([detail._probe_map_image_refs[axis].width() for axis in (0, 1)], [40, 40])
            restart.assert_not_called()
        detail._probe_map_worker_running = False

    def test_summary_uses_shared_renderer_and_preserves_frame_when_probe_moves(self):
        summary = self.summary
        state = MotorState.from_dict(self.state.to_dict())
        state.trajectory.probe_map_enabled = True
        state.trajectory.probe_map_smooth = True
        state.trajectory.probe_width_px = 36
        with patch.object(summary, '_sync_probe_map_workers'):
            summary.update_motor_state(state)
            stream = summary._probe_stream_key(state)
            summary._probe_map_values_mv[stream] = [.1, .2, .3, .4]
            summary._probe_map_depths_mm[stream] = [0, 0, .2, .2]
            summary._probe_map_x_mm[stream] = [0, .032, 0, .032]
            summary._probe_map_shanks[stream] = [0]*4
            summary._render_probe_map_motors(stream)
            self.root.update()
            smooth = summary._probe_map_image_refs[1]
            self.assertEqual(smooth.width(), 36)
            self.assertIsNotNone(summary._probe_map_renderers[1].smoother)
            body = self.probe_outline(summary._canvases[1], 'summary_overlay')
            summary.update_motor_position(1, 11)
            self.root.update()
            self.assertIs(summary._probe_map_image_refs[1], smooth)
            self.assertNotEqual(self.probe_outline(summary._canvases[1], 'summary_overlay'), body)
            state.absolute = 11
            state.trajectory.probe_map_smooth = False
            summary.update_motor_state(state)
            self.root.update()
            self.assertEqual(summary._probe_map_image_refs[1].width(), 36)
            self.assertIsNot(summary._probe_map_image_refs[1], smooth)

    def test_summary_shares_only_identical_probe_and_sampling_window(self):
        summary = self.summary
        states = [MotorState(index) for index in (1, 2, 3)]
        for state, interval in zip(states, (200, 200, 1000)):
            state.trajectory.probe_map_enabled = True
            state.trajectory.probe_map_update_ms = interval
        summary._motor_states = {state.motor_id: state for state in states}
        def start(stream, interval):
            summary._probe_map_stop_events[stream] = threading.Event()
            summary._probe_map_intervals_ms[stream] = interval
        with patch.object(summary, '_start_probe_map_worker', side_effect=start) as worker:
            summary._sync_probe_map_workers()
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(set(summary._probe_map_stop_events), {(0, 200), (0, 1000)})
            summary._sync_probe_map_workers()
            self.assertEqual(worker.call_count, 2)

    def test_summary_worker_uses_full_window_spikeglx_sampler(self):
        from unittest.mock import Mock
        summary = self.summary
        client = Mock()
        client.iter_heatmaps.return_value = iter([SimpleNamespace(peak_to_peak_mv=[.1])])
        stop = threading.Event()
        with patch('opendrive.trajectory_summary_viewer.SpikeGLXLiveClient', return_value=client):
            summary._probe_map_worker_loop((2, 1000), 9, stop, 1.)
        client.iter_heatmaps.assert_called_once_with(stop, 1000)
        client.fetch_snapshot.assert_not_called()
        client.close.assert_called_once()

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

    def test_z_updates_detail_and_summary_and_survives_field_editing(self):
        trajectory = TrajectoryState.from_dict(self.state.trajectory.to_dict())
        trajectory.z = 7.25
        self.detail.update_trajectory(trajectory)
        self.assertEqual(self.detail._z_var.get(), '7.25')
        self.assertEqual(self.detail._trajectory_origin(), (0, 0, 7.25))
        self.detail._x_var.set('1.5')
        self.assertTrue(self.detail._apply_fields())
        self.assertEqual(self.detail._trajectory.z, 7.25)
        self.state.trajectory = self.detail._trajectory
        self.summary.update_motor_state(self.state)
        np.testing.assert_allclose(self.summary._slice_geometry(1, self.state.trajectory)[0], [1.5, 0, 7.25])
        self.assertEqual(TrajectoryState.from_dict({}).z, 0)
        self.assertEqual(TrajectoryState.from_dict(trajectory.to_dict()).z, 7.25)
        self.detail._z_var.set('nan')
        with patch('opendrive.trajectory_viewer.messagebox.showwarning'):
            self.assertFalse(self.detail._apply_fields())
        self.assertEqual(self.detail._trajectory.z, 7.25)

    def test_anatomy_loading_is_background_work(self):
        entered, release = threading.Event(), threading.Event()
        anatomy = AnatomyViewer(self.root)
        anatomy.withdraw()
        def slow(_path):
            entered.set()
            release.wait(2)
            return self.volume
        try:
            with patch('opendrive.anatomy.AnatomyLoader.load', side_effect=slow):
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
