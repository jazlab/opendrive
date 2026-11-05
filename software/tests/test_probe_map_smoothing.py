"""Physical-distance smoothing preserves units, geometry, and missing regions."""
import unittest
import numpy as np
from opendrive.probe_map_smoothing import SpatialActivitySmoother, smoothed_activity_image


class SpatialActivityTests(unittest.TestCase):
    def test_constant_activity_is_preserved_including_boundaries(self):
        depths = np.repeat(np.arange(192) * .02, 2)
        plan = SpatialActivitySmoother(384, depths, [0]*384, 150)
        profile = plan.smooth([.2]*384)[0]
        np.testing.assert_allclose(profile.values_mv, .2, atol=1e-12)

    def test_columns_are_averaged_at_same_depth_and_large_gaps_are_not_filled(self):
        plan = SpatialActivitySmoother(4, [0, 0, 1, 1], [0]*4, 150)
        profile = plan.smooth([1, 3, 4, 8])[0]
        self.assertAlmostEqual(profile.values_mv[0], 2)
        self.assertAlmostEqual(profile.values_mv[-1], 6)
        self.assertTrue(np.isnan(profile.values_mv[len(profile.values_mv)//2]))

    def test_distance_and_shank_geometry_matter_not_channel_order(self):
        plan = SpatialActivitySmoother(4, [0, .01, 1, 1.01], [0, 0, 1, 1], 150)
        profiles = plan.smooth([.1, .1, .8, .8])
        np.testing.assert_allclose(profiles[0].values_mv, .1)
        np.testing.assert_allclose(profiles[1].values_mv, .8)
        permuted = SpatialActivitySmoother(4, [1.01, .01, 1, 0], [1, 0, 1, 0], 150).smooth([.8, .1, .8, .1])
        for first, second in zip(profiles, permuted):
            np.testing.assert_allclose(first.values_mv, second.values_mv)

    def test_wider_kernel_softens_single_contact_peaks(self):
        depths = np.arange(51) * .02
        values = np.zeros(51); values[25] = 1
        narrow = SpatialActivitySmoother(51, depths, [0]*51, 50).smooth(values)[0]
        broad = SpatialActivitySmoother(51, depths, [0]*51, 200).smooth(values)[0]
        self.assertGreater(np.nanmax(narrow.values_mv), np.nanmax(broad.values_mv))
        self.assertTrue(np.all(broad.values_mv >= 0))

    def test_raster_uses_calibrated_range_and_transparent_unsupported_regions(self):
        profile = SpatialActivitySmoother(2, [0, 10], [0, 0], 150).smooth([.2, .2])
        lut = np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1)
        image = smoothed_activity_image(profile, 1000, 100, 900, lut, 0, 1)
        self.assertEqual(image.width, 12)
        np.testing.assert_allclose(image.getpixel((4, 899)), (51, 51, 51, 255), atol=1)
        self.assertEqual(image.getpixel((4, 850))[3], 0)
        self.assertEqual(image.getpixel((4, 10))[3], 0)

    def test_nonfinite_channels_do_not_poison_other_contacts(self):
        plan = SpatialActivitySmoother(4, [0, 0, .02, .02], [0]*4, 150)
        np.testing.assert_allclose(plan.smooth([float('nan'), .3, .3, float('nan')])[0].values_mv, .3)
