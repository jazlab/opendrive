"""The same shank bounds contain contact and smoothed activity."""
import unittest
import numpy as np
from opendrive.models import TrajectoryState
from opendrive.probe_map_colors import load_shank_lut
from opendrive.probe_overlay import ProbeOverlayRenderer, probe_body_points


class ProbeOverlayTests(unittest.TestCase):
    def test_deepest_contacts_are_half_a_millimeter_above_tip_in_both_modes(self):
        renderer = ProbeOverlayRenderer(load_shank_lut())
        for smooth in (False, True):
            for height_mm in (10, 20):
                with self.subTest(smooth=smooth, height_mm=height_mm):
                    state = TrajectoryState(probe_map_smooth=smooth)
                    image = renderer.render(state, [.1, .2], [0, 0], [0, .032],
                                            [0, 0], 1000, height_mm, 800)
                    rows = np.flatnonzero(np.any(np.asarray(image)[:, :, 3], axis=1))
                    expected = 800 - .5 * 1000 / height_mm
                    self.assertTrue(len(rows))
                    self.assertLessEqual(abs(rows[-1] - expected), 1)
                    self.assertTrue(np.all(np.asarray(image)[int(expected)+2:800, :, 3] == 0))

    def test_width_is_fixed_across_modes_and_contact_column_counts(self):
        renderer = ProbeOverlayRenderer(load_shank_lut())
        for width in (6, 24, 48, 128):
            for smooth in (False, True):
                state = TrajectoryState(probe_width_px=width, probe_map_smooth=smooth)
                image = renderer.render(state, [.1, .2, .3, .4], [1, 1, 2, 2], [0, .032, 0, .032],
                                        [0]*4, 430, 48, 240)
                self.assertEqual(image.size, (width, 430))
                self.assertTrue(np.all(np.asarray(image)[:, (0, -1), 3] == 0))

    def test_shank_tip_and_outline_preserve_position_and_selected_pixel_width(self):
        points = probe_body_points(100, 20, 300, 40)
        self.assertEqual(points[6:8], (100, 300))
        self.assertEqual(max(points[::2])-min(points[::2]), 40)

    def test_cached_smoothing_weights_survive_width_changes(self):
        renderer = ProbeOverlayRenderer(load_shank_lut())
        state = TrajectoryState(probe_map_smooth=True)
        args = ([.1, .2], [0, .02], [0, .032], [0, 0], 430, 48, 240)
        renderer.render(state, *args)
        plan = renderer.smoother
        state.probe_width_px = 40
        self.assertEqual(renderer.render(state, *args).width, 40)
        self.assertIs(renderer.smoother, plan)
