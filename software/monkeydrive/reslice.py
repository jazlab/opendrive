"""Compiled, read-only oblique reslicing with the legacy voxel-boundary policy."""
import numpy as np

from .anatomy import AnatomyDependencyError, AnatomySliceRenderer


class ObliqueReslicer:
    """Cache world-to-voxel mapping; independent slices can share this sampler."""
    def __init__(self, volume):
        try:
            from scipy.ndimage import affine_transform
        except ImportError as error:
            raise AnatomyDependencyError('MRI reslicing requires scipy. Install software/requirements.txt.') from error
        self._affine_transform = affine_transform
        self.volume = volume
        self.inverse = np.eye(4) if volume.affine is None else np.linalg.inv(np.asarray(volume.affine, dtype=float))

    def sample(self, center_world, horizontal_world, vertical_world, width_mm, height_mm,
               max_size=430, zoom=1.0):
        horizontal = AnatomySliceRenderer._unit_vector(horizontal_world)
        vertical = AnatomySliceRenderer._unit_vector(vertical_world)
        width_mm, height_mm = max(float(width_mm), 1.), max(float(height_mm), 1.)
        zoom = max(.25, min(float(zoom), 10.))
        largest = max(width_mm, height_mm)
        width = max(1, int(width_mm / largest * max_size * zoom))
        height = max(1, int(height_mm / largest * max_size * zoom))
        # scipy maps output indices back into input voxels. The first output
        # dimension is a singleton so a 3-D volume produces a single 2-D plane.
        start = np.asarray(center_world, dtype=float) - horizontal * width_mm / 2 + vertical * height_mm / 2
        offset = self.inverse[:3, :3] @ start + self.inverse[:3, 3]
        across = self.inverse[:3, :3] @ horizontal * (width_mm / (width - 1) if width > 1 else 0.)
        down = self.inverse[:3, :3] @ vertical * (-height_mm / (height - 1) if height > 1 else 0.)
        matrix = np.column_stack((np.zeros(3), down, across))
        image = self._affine_transform(self.volume.data, matrix, offset,
            output_shape=(1, height, width), output=np.float32, order=1,
            mode='constant', cval=0., prefilter=False)[0]
        # The original interpolator requires all eight neighbors to exist.
        # In particular, a sample on the last voxel face was black, not clamped.
        rows = np.arange(height, dtype=float)[:, None]
        columns = np.arange(width, dtype=float)[None, :]
        valid = np.ones((height, width), dtype=bool)
        for axis, size in enumerate(self.volume.shape):
            coordinate = offset[axis] + down[axis] * rows + across[axis] * columns
            valid &= (coordinate >= 0) & (coordinate < size - 1)
        image[~valid] = 0.
        return image

    def grayscale(self, *geometry, contrast_low=1., contrast_high=99., max_size=430):
        image = self.sample(*geometry, max_size=max_size)
        return AnatomySliceRenderer._contrast_to_uint8(image, contrast_low, contrast_high)
