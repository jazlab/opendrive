"""Shared shank geometry and activity rendering for detail and summary views."""
import numpy as np
from PIL import Image, ImageDraw, ImageChops

from .probe_map_colors import color_from_lut
from .probe_map_layout import probe_map_columns
from .probe_map_smoothing import SpatialActivitySmoother, smoothed_activity_image


def probe_body_points(center_x, top, bottom, width, tip_height=6.):
    half = width / 2
    tip = min(tip_height, width * .4, max(0., (bottom - top) / 2))
    return (center_x-half, top, center_x+half, top, center_x+half, bottom-tip,
            center_x, bottom, center_x-half, bottom-tip)


def contact_activity_image(colors, depths, xs, shanks, height, height_mm, bottom, pitch_mm=.01):
    width, columns = probe_map_columns(len(colors), xs, shanks)
    image = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(image)
    scale = height / max(float(height_mm), 1.)
    pitch = pitch_mm * scale
    for channel, color in enumerate(colors):
        if len(depths):
            if channel >= len(depths):
                break
            center = bottom - depths[channel] * scale
            if center < 0 or center > bottom:
                continue
            contact_height = max(1, round(pitch))
            y0 = round(center-contact_height/2)
            y1 = min(bottom, y0+contact_height-1)
        else:
            y0, y1 = bottom-(channel+1)*pitch, bottom-channel*pitch
        y0, y1 = max(0, y0), min(height-1, y1)
        if y1 >= y0:
            x0, x1 = columns[channel]
            draw.rectangle((x0, y0, x1, y1), fill=color)
    return image


class ProbeOverlayRenderer:
    def __init__(self, lut):
        self.lut = lut
        self.rgb = np.asarray([[int(color[index:index+2], 16) for index in (1, 3, 5)] for color in lut], dtype=np.uint8)
        self.geometry = self.values_key = None
        self.smoother = None
        self.profiles = []

    def render(self, trajectory, values, depths, xs, shanks, height, height_mm, bottom,
               profiles=None, colors=None, limits=None):
        width = int(trajectory.probe_width_px)
        low, high = limits or (trajectory.probe_map_min_mv, trajectory.probe_map_max_mv)
        high = high if high > low else low+1
        scale = height / max(float(height_mm), 1.)
        # SpikeGLX site depths start at the deepest contact, 500 µm above
        # the physical tip. Keep this translation common to both map modes.
        contact_bottom = bottom - .5 * scale
        if trajectory.probe_map_smooth and profiles is None and len(values):
            geometry = (len(values), tuple(depths), tuple(shanks), trajectory.probe_map_smoothing_um)
            if geometry != self.geometry:
                self.smoother = SpatialActivitySmoother(*geometry)
                self.geometry, self.values_key = geometry, None
            key = tuple(values)
            if key != self.values_key:
                self.profiles = self.smoother.smooth(key)
                self.values_key = key
            profiles = self.profiles
        if trajectory.probe_map_smooth and len(values):
            source = smoothed_activity_image(profiles, height, height_mm, contact_bottom, self.rgb, low, high)
        else:
            if colors is None:
                colors = [color_from_lut(self.lut, value, low, high) for value in values]
            source = contact_activity_image(colors, depths, xs, shanks, height, height_mm, contact_bottom)
        # Both modes fill the same inset within the shank. Width never depends
        # on smoothing or on the number of contact columns.
        result = Image.new('RGBA', (width, height))
        source = source.resize((width-4, height), Image.Resampling.NEAREST)
        result.paste(source, (2, 0))
        mask = Image.new('L', (width, height))
        points = probe_body_points((width-1)/2, 0, bottom-1, width-3, min(6., .4 * scale))
        if bottom > 0:
            ImageDraw.Draw(mask).polygon(points, fill=255)
        result.putalpha(ImageChops.multiply(result.getchannel('A'), mask))
        return result
