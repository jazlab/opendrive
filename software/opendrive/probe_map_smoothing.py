"""Spatial smoothing of calibrated activity, independent of acquisition timing."""
from dataclasses import dataclass
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class ActivityProfile:
    shank: int
    depths_mm: np.ndarray
    values_mv: np.ndarray


class SpatialActivitySmoother:
    """Cache Gaussian weights in physical depth; average columns at each depth.

    Width is the Gaussian full width at half maximum in micrometers. Separate
    shanks stay separate, and gaps beyond four sigma remain unsupported.
    """
    def __init__(self, count, depths_mm, shanks, fwhm_um):
        sigma = float(fwhm_um) / 1000 / np.sqrt(8 * np.log(2))
        if not np.isfinite(sigma) or sigma <= 0:
            raise ValueError('Smoothing width must be finite and positive.')
        depths = np.asarray(depths_mm[:count] if len(depths_mm) >= count else np.arange(count) * .01, dtype=float)
        shanks = np.asarray(shanks[:count] if len(shanks) >= count else np.zeros(count, dtype=int))
        self.count, self.groups = count, []
        for shank in sorted(set(shanks.tolist())):
            channels = np.flatnonzero((shanks == shank) & np.isfinite(depths))
            if not len(channels):
                continue
            unique, inverse = np.unique(np.round(depths[channels], 6), return_inverse=True)
            points = max(1, min(4096, int(np.ceil((unique[-1] - unique[0]) / .01)) + 1))
            grid = np.linspace(unique[0], unique[-1], points)
            distance = (grid[:, None] - unique[None, :]) / sigma
            weights = np.exp(-.5 * distance**2)
            weights[np.abs(distance) > 4] = 0
            self.groups.append((shank, channels, unique, inverse, grid, weights))

    def smooth(self, values_mv):
        values = np.asarray(values_mv, dtype=float)
        if len(values) != self.count:
            raise ValueError('Activity and geometry channel counts differ.')
        profiles = []
        for shank, channels, unique, inverse, grid, weights in self.groups:
            data = values[channels]
            finite = np.isfinite(data)
            counts = np.bincount(inverse, weights=finite, minlength=len(unique))
            totals = np.bincount(inverse, weights=np.where(finite, data, 0), minlength=len(unique))
            means = np.divide(totals, counts, out=np.zeros_like(totals), where=counts > 0)
            numerator = np.einsum('ij,j->i', weights, means)
            denominator = np.einsum('ij,j->i', weights, (counts > 0).astype(float))
            result = np.divide(numerator, denominator, out=np.full(len(grid), np.nan), where=denominator > 1e-12)
            profiles.append(ActivityProfile(shank, grid, result))
        return profiles


def smoothed_activity_image(profiles, image_height, height_mm, bottom, lut_rgb, low, high):
    """Rasterize one continuous band per shank at the MRI's physical scale."""
    band_width, gap = 12, 2
    width = max(1, len(profiles) * (band_width + gap) - gap)
    pixels = np.zeros((image_height, width, 4), dtype=np.uint8)
    scale = image_height / max(float(height_mm), 1.)
    depth_at_row = (bottom - np.arange(image_height) - .5) / scale
    for index, profile in enumerate(profiles):
        depths, values = profile.depths_mm, profile.values_mv
        activity = np.interp(depth_at_row, depths, values)
        margin = max(.005, .5 / scale)
        valid = ((depth_at_row >= depths[0] - margin) & (depth_at_row <= depths[-1] + margin)
                 & np.isfinite(activity))
        normalized = np.clip((np.nan_to_num(activity) - low) / max(high - low, 1e-12), 0, 1)
        colors = lut_rgb[(normalized * 255).astype(np.uint8)]
        start = index * (band_width + gap)
        pixels[valid, start:start + band_width, :3] = colors[valid, None, :]
        pixels[valid, start:start + band_width, 3] = 255
    return Image.fromarray(pixels)
