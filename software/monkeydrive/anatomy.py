"""MRI anatomy loading and slice rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Optional


class AnatomyDependencyError(ImportError):
    """Raised when MRI dependencies are not installed."""


def _import_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise AnatomyDependencyError(
            "MRI support requires numpy and nibabel. "
            "Install the packages in software/requirements.txt."
        ) from exc
    return np


@dataclass
class AnatomyVolume:
    """Loaded anatomical image volume."""

    data: object
    source_path: Path
    source_type: str
    affine: Optional[object] = None
    voxel_sizes: Optional[tuple] = None
    orientation_codes: Optional[tuple] = None

    @property
    def shape(self) -> tuple:
        """Return the data shape."""
        return tuple(self.data.shape)

    @property
    def display_name(self) -> str:
        """Return a compact display name for the volume."""
        return f"{self.source_type}: {self.source_path.name}"

    def world_to_voxel(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        """Convert MRI/world millimeter coordinates to floating voxel coordinates."""
        np = _import_numpy()
        if self.affine is None:
            return (float(x), float(y), float(z))

        inverse = np.linalg.inv(np.asarray(self.affine, dtype=float))
        voxel = inverse @ np.asarray([x, y, z, 1.0], dtype=float)
        return (float(voxel[0]), float(voxel[1]), float(voxel[2]))

    def world_axis_to_index(self, axis: int, value: float) -> int:
        """Convert a canonical MRI/world coordinate to a clamped voxel index."""
        point = [0.0, 0.0, 0.0]
        point[axis] = float(value)
        voxel = self.world_to_voxel(point[0], point[1], point[2])
        max_index = max(0, self.shape[axis] - 1)
        return max(0, min(max_index, int(round(voxel[axis]))))


@dataclass(frozen=True)
class OrientationSpec:
    """Subject/scanner orientation as a signed axis permutation."""

    key: str
    label: str
    matrix: tuple


class AnatomyOrientation:
    """Helpers for correcting mismatched scanner and actual subject orientation."""

    SUPINE_HEAD_FIRST = "supine_head_first"
    PRONE_HEAD_FIRST = "prone_head_first"
    SUPINE_FEET_FIRST = "supine_feet_first"
    PRONE_FEET_FIRST = "prone_feet_first"
    SPHINX_HEAD_FIRST = "sphinx_head_first"
    SPHINX_FEET_FIRST = "sphinx_feet_first"

    _SPECS = {
        SUPINE_HEAD_FIRST: OrientationSpec(
            SUPINE_HEAD_FIRST, "Supine Head First",
            ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        ),
        PRONE_HEAD_FIRST: OrientationSpec(
            PRONE_HEAD_FIRST, "Prone Head First",
            ((-1, 0, 0), (0, -1, 0), (0, 0, 1)),
        ),
        SUPINE_FEET_FIRST: OrientationSpec(
            SUPINE_FEET_FIRST, "Supine Feet First",
            ((-1, 0, 0), (0, 1, 0), (0, 0, -1)),
        ),
        PRONE_FEET_FIRST: OrientationSpec(
            PRONE_FEET_FIRST, "Prone Feet First",
            ((1, 0, 0), (0, -1, 0), (0, 0, -1)),
        ),
        SPHINX_HEAD_FIRST: OrientationSpec(
            SPHINX_HEAD_FIRST, "Sphinx Head First",
            ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
        ),
        SPHINX_FEET_FIRST: OrientationSpec(
            SPHINX_FEET_FIRST, "Sphinx Feet First",
            ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
        ),
    }

    @classmethod
    def actual_options(cls) -> tuple[OrientationSpec, ...]:
        """Return orientations available for the actual subject position."""
        return (
            cls._SPECS[cls.SUPINE_HEAD_FIRST],
            cls._SPECS[cls.PRONE_HEAD_FIRST],
            cls._SPECS[cls.SUPINE_FEET_FIRST],
            cls._SPECS[cls.PRONE_FEET_FIRST],
            cls._SPECS[cls.SPHINX_HEAD_FIRST],
            cls._SPECS[cls.SPHINX_FEET_FIRST],
        )

    @classmethod
    def scanner_options(cls) -> tuple[OrientationSpec, ...]:
        """Return standard orientations declared by scanner operators."""
        return (
            cls._SPECS[cls.SUPINE_HEAD_FIRST],
            cls._SPECS[cls.PRONE_HEAD_FIRST],
            cls._SPECS[cls.SUPINE_FEET_FIRST],
            cls._SPECS[cls.PRONE_FEET_FIRST],
        )

    @classmethod
    def label(cls, key: str) -> str:
        """Return the display label for an orientation key."""
        return cls._SPECS[key].label

    @classmethod
    def correction_matrix(cls, actual_key: str, scanner_key: str):
        """Return the signed axis transform used for an orientation correction."""
        np = _import_numpy()
        actual = np.asarray(cls._SPECS[actual_key].matrix, dtype=int)
        scanner = np.asarray(cls._SPECS[scanner_key].matrix, dtype=int)
        return actual @ scanner.T

    @classmethod
    def world_from_corrected_matrix(cls, actual_key: str, scanner_key: str):
        """Return a matrix mapping corrected/anatomical world vectors to scanner-world vectors."""
        np = _import_numpy()
        return np.asarray(cls.correction_matrix(actual_key, scanner_key), dtype=float).T

    @classmethod
    def correct_volume(cls, volume: AnatomyVolume, actual_key: str, scanner_key: str) -> AnatomyVolume:
        """Return a display-corrected copy of a volume."""
        np = _import_numpy()
        transform = cls.correction_matrix(actual_key, scanner_key)

        corrected_data, axis_order, flipped_axes = cls._apply_signed_permutation(
            volume.data, transform
        )

        voxel_sizes = None
        if volume.voxel_sizes and len(volume.voxel_sizes) >= 3:
            voxel_sizes = tuple(volume.voxel_sizes[axis] for axis in axis_order)

        affine = None
        if volume.affine is not None:
            old_from_new = cls._old_from_new_voxel_transform(
                volume.data.shape, axis_order, flipped_axes
            )
            affine = np.asarray(volume.affine, dtype=float) @ old_from_new

        return AnatomyVolume(
            data=corrected_data,
            source_path=volume.source_path,
            source_type=volume.source_type,
            affine=affine,
            voxel_sizes=voxel_sizes,
            orientation_codes=volume.orientation_codes,
        )

    @staticmethod
    def _apply_signed_permutation(data, matrix):
        """Apply a 3D signed permutation matrix to array axes."""
        np = _import_numpy()
        matrix = np.asarray(matrix, dtype=int)
        if matrix.shape != (3, 3):
            raise ValueError("Orientation transform must be 3x3.")

        axis_order = []
        flipped_axes = []
        for new_axis in range(3):
            old_axes = np.nonzero(matrix[new_axis])[0]
            if len(old_axes) != 1:
                raise ValueError("Orientation transform must contain one source axis per output axis.")
            old_axis = int(old_axes[0])
            axis_order.append(old_axis)
            if matrix[new_axis, old_axis] < 0:
                flipped_axes.append(new_axis)

        transformed = np.transpose(data, axes=axis_order)
        for axis in flipped_axes:
            transformed = np.flip(transformed, axis=axis)

        return transformed.copy(), tuple(axis_order), tuple(flipped_axes)

    @staticmethod
    def _old_from_new_voxel_transform(old_shape, axis_order, flipped_axes):
        """Return a homogeneous matrix mapping corrected voxel coords to original voxel coords."""
        np = _import_numpy()
        matrix = np.eye(4, dtype=float)
        matrix[:3, :3] = 0.0
        matrix[:3, 3] = 0.0

        flipped_axes = set(flipped_axes)
        for new_axis, old_axis in enumerate(axis_order):
            if new_axis in flipped_axes:
                matrix[old_axis, new_axis] = -1.0
                matrix[old_axis, 3] = float(old_shape[old_axis] - 1)
            else:
                matrix[old_axis, new_axis] = 1.0

        return matrix


class AnatomyLoader:
    """Load standard MRI anatomy files."""

    NIFTI_SUFFIXES = (".nii", ".nii.gz")

    @staticmethod
    def load(path: str | Path) -> AnatomyVolume:
        """Load supported NIfTI anatomy; reject other formats explicitly."""
        source = Path(path)
        if source.is_dir():
            raise ValueError('Open a NIfTI file (.nii or .nii.gz), not a folder.')

        lower_name = source.name.lower()
        if lower_name.endswith(AnatomyLoader.NIFTI_SUFFIXES):
            return AnatomyLoader.load_nifti(source)

        raise ValueError('Only NIfTI anatomy (.nii or .nii.gz) is supported.')

    @staticmethod
    def load_nifti(path: Path) -> AnatomyVolume:
        """Load a NIfTI anatomy file."""
        np = _import_numpy()
        try:
            import nibabel as nib
        except ImportError as exc:
            raise AnatomyDependencyError(
                "NIfTI support requires nibabel. Install software/requirements.txt."
            ) from exc

        image = nib.load(str(path))
        image = nib.as_closest_canonical(image)
        data = np.asarray(image.get_fdata(dtype=np.float32))
        data = AnatomyLoader._ensure_3d(data)

        try:
            voxel_sizes = tuple(float(v) for v in image.header.get_zooms()[:3])
        except Exception:
            voxel_sizes = None

        return AnatomyVolume(
            data=data,
            source_path=path,
            source_type="NIfTI",
            affine=image.affine,
            voxel_sizes=voxel_sizes,
            orientation_codes=tuple(nib.aff2axcodes(image.affine)),
        )

    @staticmethod
    def _ensure_3d(data):
        np = _import_numpy()
        data = np.squeeze(data)
        if data.ndim == 2:
            return data[:, :, np.newaxis]
        if data.ndim == 3:
            return data
        if data.ndim == 4:
            return data[:, :, :, 0]
        raise ValueError(f"Expected a 2D/3D MRI volume, got shape {data.shape}")



class AnatomySliceRenderer:
    """Convert anatomy slices to Tk-compatible grayscale PPM images."""

    @staticmethod
    def slice_to_ppm(
        volume: AnatomyVolume,
        axis: int,
        index: int,
        max_size: int = 360,
        contrast_low: float = 1.0,
        contrast_high: float = 99.0,
        zoom: float = 1.0,
        screen_rotation_deg: float = 0.0,
    ) -> bytes:
        """Return a PPM image byte string for one anatomical slice."""
        image = AnatomySliceRenderer.slice_to_uint8(
            volume, axis, index, contrast_low, contrast_high, screen_rotation_deg
        )
        image = AnatomySliceRenderer._resize_nearest(image, max_size, zoom)
        np = _import_numpy()
        height, width = image.shape
        rgb_image = np.repeat(image[:, :, np.newaxis], 3, axis=2)
        header = f"P6\n{width} {height}\n255\n".encode("ascii")
        return header + rgb_image.tobytes()

    @staticmethod
    def slice_to_uint8(
        volume: AnatomyVolume,
        axis: int,
        index: int,
        contrast_low: float = 1.0,
        contrast_high: float = 99.0,
        screen_rotation_deg: float = 0.0,
    ):
        """Return a contrast-adjusted 8-bit slice, optionally rotated on screen."""
        np = _import_numpy()
        data = volume.data
        axis = max(0, min(axis, 2))
        index = max(0, min(index, data.shape[axis] - 1))
        if axis == 0:
            image = data[index, :, :]
        elif axis == 1:
            image = data[:, index, :]
        else:
            image = data[:, :, index]

        image = np.asarray(image, dtype=np.float32)
        image = np.rot90(image)

        finite = image[np.isfinite(image)]
        if finite.size == 0:
            image = np.zeros(image.shape, dtype=np.uint8)
        else:
            contrast_low = max(0.0, min(float(contrast_low), 100.0))
            contrast_high = max(0.0, min(float(contrast_high), 100.0))
            if contrast_high <= contrast_low:
                contrast_high = min(100.0, contrast_low + 1.0)

            low, high = np.percentile(finite, (contrast_low, contrast_high))
            if high <= low:
                low, high = float(np.min(finite)), float(np.max(finite))
            if high <= low:
                image = np.zeros(image.shape, dtype=np.uint8)
            else:
                image = np.clip((image - low) / (high - low), 0.0, 1.0)
                image = (image * 255.0).astype(np.uint8)

        if abs(screen_rotation_deg) > 0.001:
            image = AnatomySliceRenderer._rotate_screen_nearest(image, screen_rotation_deg)
        return image

    @staticmethod
    def slice_to_pgm(volume: AnatomyVolume, axis: int, index: int, max_size: int = 520) -> bytes:
        """Return a PPM image byte string for backward compatibility."""
        return AnatomySliceRenderer.slice_to_ppm(volume, axis, index, max_size=max_size)

    @staticmethod
    def oblique_slice_to_ppm(
        volume: AnatomyVolume,
        center_world: tuple[float, float, float],
        horizontal_world: tuple[float, float, float],
        vertical_world: tuple[float, float, float],
        width_mm: float,
        height_mm: float,
        max_size: int = 430,
        contrast_low: float = 1.0,
        contrast_high: float = 99.0,
        zoom: float = 1.0,
    ) -> bytes:
        """Sample an oblique MRI slice in world coordinates and return PPM bytes."""
        from .reslice import ObliqueReslicer
        image = ObliqueReslicer(volume).sample(center_world, horizontal_world, vertical_world,
                                               width_mm, height_mm, max_size=max_size, zoom=zoom)
        image = AnatomySliceRenderer._contrast_to_uint8(image, contrast_low, contrast_high)
        return AnatomySliceRenderer._uint8_to_ppm(image)

    @staticmethod
    def _resize_nearest(image, max_size: int, zoom: float = 1.0):
        np = _import_numpy()
        height, width = image.shape
        largest = max(height, width)
        zoom = max(0.25, min(float(zoom), 10.0))
        base_scale = min(1.0, max_size / largest)
        scale = base_scale * zoom
        if abs(scale - 1.0) < 0.01:
            return image

        new_height = max(1, int(height * scale))
        new_width = max(1, int(width * scale))
        y_idx = np.linspace(0, height - 1, new_height).astype(int)
        x_idx = np.linspace(0, width - 1, new_width).astype(int)
        return image[np.ix_(y_idx, x_idx)]

    @staticmethod
    def _contrast_to_uint8(image, contrast_low: float, contrast_high: float):
        np = _import_numpy()
        image = np.asarray(image, dtype=np.float32)
        finite = image[np.isfinite(image)]
        if finite.size == 0:
            return np.zeros(image.shape, dtype=np.uint8)

        contrast_low = max(0.0, min(float(contrast_low), 100.0))
        contrast_high = max(0.0, min(float(contrast_high), 100.0))
        if contrast_high <= contrast_low:
            contrast_high = min(100.0, contrast_low + 1.0)

        low, high = np.percentile(finite, (contrast_low, contrast_high))
        if high <= low:
            low, high = float(np.min(finite)), float(np.max(finite))
        if high <= low:
            return np.zeros(image.shape, dtype=np.uint8)

        image = np.clip((image - low) / (high - low), 0.0, 1.0)
        return (image * 255.0).astype(np.uint8)

    @staticmethod
    def _uint8_to_ppm(image) -> bytes:
        np = _import_numpy()
        height, width = image.shape
        rgb_image = np.repeat(image[:, :, np.newaxis], 3, axis=2)
        header = f"P6\n{width} {height}\n255\n".encode("ascii")
        return header + rgb_image.tobytes()

    @staticmethod
    def _trilinear_sample(data, voxel):
        np = _import_numpy()
        x = voxel[0]
        y = voxel[1]
        z = voxel[2]
        shape = data.shape

        x0 = np.floor(x).astype(int)
        y0 = np.floor(y).astype(int)
        z0 = np.floor(z).astype(int)
        x1 = x0 + 1
        y1 = y0 + 1
        z1 = z0 + 1

        valid = (
            (x0 >= 0) & (x1 < shape[0]) &
            (y0 >= 0) & (y1 < shape[1]) &
            (z0 >= 0) & (z1 < shape[2])
        )

        x0c = np.clip(x0, 0, shape[0] - 1)
        x1c = np.clip(x1, 0, shape[0] - 1)
        y0c = np.clip(y0, 0, shape[1] - 1)
        y1c = np.clip(y1, 0, shape[1] - 1)
        z0c = np.clip(z0, 0, shape[2] - 1)
        z1c = np.clip(z1, 0, shape[2] - 1)

        xd = x - x0
        yd = y - y0
        zd = z - z0

        c000 = data[x0c, y0c, z0c]
        c100 = data[x1c, y0c, z0c]
        c010 = data[x0c, y1c, z0c]
        c110 = data[x1c, y1c, z0c]
        c001 = data[x0c, y0c, z1c]
        c101 = data[x1c, y0c, z1c]
        c011 = data[x0c, y1c, z1c]
        c111 = data[x1c, y1c, z1c]

        c00 = c000 * (1 - xd) + c100 * xd
        c10 = c010 * (1 - xd) + c110 * xd
        c01 = c001 * (1 - xd) + c101 * xd
        c11 = c011 * (1 - xd) + c111 * xd
        c0 = c00 * (1 - yd) + c10 * yd
        c1 = c01 * (1 - yd) + c11 * yd
        sampled = c0 * (1 - zd) + c1 * zd
        sampled = np.asarray(sampled, dtype=np.float32)
        sampled[~valid] = 0.0
        return sampled

    @staticmethod
    def _unit_vector(vector):
        np = _import_numpy()
        vector = np.asarray(vector, dtype=float)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise ValueError("Slice basis vector cannot be zero.")
        return vector / norm

    @staticmethod
    def rotation_matrix(axis: tuple[float, float, float], angle_deg: float):
        """Return a 3D right-hand-rule rotation matrix."""
        np = _import_numpy()
        axis = AnatomySliceRenderer._unit_vector(axis)
        angle = math.radians(float(angle_deg))
        x, y, z = axis
        c = math.cos(angle)
        s = math.sin(angle)
        one_c = 1.0 - c
        return np.asarray([
            [c + x * x * one_c, x * y * one_c - z * s, x * z * one_c + y * s],
            [y * x * one_c + z * s, c + y * y * one_c, y * z * one_c - x * s],
            [z * x * one_c - y * s, z * y * one_c + x * s, c + z * z * one_c],
        ], dtype=float)

    @staticmethod
    def _rotate_screen_nearest(image, angle_deg: float):
        """Rotate an image by degrees in screen coordinates; positive is clockwise."""
        np = _import_numpy()
        height, width = image.shape
        angle = np.deg2rad(float(angle_deg))
        cos_a = np.cos(angle)
        sin_a = np.sin(angle)

        corners = np.array([
            [-(width - 1) / 2.0, -(height - 1) / 2.0],
            [(width - 1) / 2.0, -(height - 1) / 2.0],
            [-(width - 1) / 2.0, (height - 1) / 2.0],
            [(width - 1) / 2.0, (height - 1) / 2.0],
        ])
        rotated_corners = np.column_stack((
            (corners[:, 0] * cos_a) + (corners[:, 1] * sin_a),
            (-corners[:, 0] * sin_a) + (corners[:, 1] * cos_a),
        ))
        new_width = max(1, int(np.ceil(rotated_corners[:, 0].max() - rotated_corners[:, 0].min())) + 1)
        new_height = max(1, int(np.ceil(rotated_corners[:, 1].max() - rotated_corners[:, 1].min())) + 1)

        y_out, x_out = np.indices((new_height, new_width))
        x_centered = x_out - ((new_width - 1) / 2.0)
        y_centered = y_out - ((new_height - 1) / 2.0)

        src_x = (x_centered * cos_a) - (y_centered * sin_a) + ((width - 1) / 2.0)
        src_y = (x_centered * sin_a) + (y_centered * cos_a) + ((height - 1) / 2.0)
        src_x = np.rint(src_x).astype(int)
        src_y = np.rint(src_y).astype(int)

        rotated = np.zeros((new_height, new_width), dtype=np.uint8)
        mask = (src_x >= 0) & (src_x < width) & (src_y >= 0) & (src_y < height)
        rotated[mask] = image[src_y[mask], src_x[mask]]
        return rotated
