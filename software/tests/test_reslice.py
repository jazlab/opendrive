"""Compiled reslicing agrees with the reference interpolation and world mapping."""
from pathlib import Path
import unittest
import numpy as np
from opendrive.anatomy import AnatomyVolume, AnatomySliceRenderer
from opendrive.reslice import ObliqueReslicer


class ResliceTests(unittest.TestCase):
    def volume(self, data, affine=None):
        return AnatomyVolume(data, Path('synthetic.nii'), 'NIfTI', affine, (1, 1, 1))

    def test_transformed_oblique_sampling_matches_reference(self):
        rng = np.random.default_rng(42)
        data = rng.normal(size=(30, 28, 32)).astype(np.float32)
        affine = np.array([[0, -1.3, .2, 30], [.8, 0, .1, -10], [0, .1, 1.6, 4], [0, 0, 0, 1.]])
        reslicer = ObliqueReslicer(self.volume(data, affine))
        center = (affine @ [14, 13, 15, 1])[:3]
        h = np.array([1., .2, .1]); h /= np.linalg.norm(h)
        v = np.cross(h, [.1, .2, 1]); v /= np.linalg.norm(v)
        result = reslicer.sample(center, h, v, 14, 12, max_size=87)
        rows, cols = result.shape
        world = (center[:, None, None] + h[:, None, None] * np.linspace(-7, 7, cols)
                 + v[:, None, None] * np.linspace(6, -6, rows)[:, None])
        voxel = (reslicer.inverse @ np.vstack([world.reshape(3, -1), np.ones(rows * cols)]))[:3]
        expected = AnatomySliceRenderer._trilinear_sample(data, voxel).reshape(result.shape)
        np.testing.assert_allclose(result, expected, atol=2e-6)

    def test_voxel_boundary_policy_and_orientation(self):
        data = np.indices((5, 5, 5)).sum(axis=0).astype(np.float32)
        sampler = ObliqueReslicer(self.volume(data))
        result = sampler.sample([2, 2, 2], [1, 0, 0], [0, 1, 0], 4, 4, max_size=5)
        np.testing.assert_array_equal(result[0], 0)  # last Y face
        np.testing.assert_array_equal(result[:, -1], 0)  # last X face
        self.assertEqual(result[2, 2], 6)
        self.assertEqual(result[-1, 0], 2)
        self.assertTrue(np.all(sampler.sample([-10, -10, -10], [1, 0, 0], [0, 1, 0], 4, 4) == 0))

    def test_single_voxel_dimension_and_single_pixel_output(self):
        data = np.ones((1, 5, 5), np.float32)
        result = ObliqueReslicer(self.volume(data)).sample([0, 2, 2], [0, 1, 0], [0, 0, 1], 2, 2, max_size=1)
        self.assertEqual(result.shape, (1, 1))
        self.assertEqual(result[0, 0], 0)
