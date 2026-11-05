"""Equal-size activity pads positioned using the probe's contact geometry."""
import math


def _pitch(values, fallback):
    ordered = sorted(set(round(value, 6) for value in values))
    return min((b - a for a, b in zip(ordered, ordered[1:]) if b > a), default=fallback)


def probe_map_columns(count, x_positions, shanks, contact_width=10, spacing=2):
    """Return strip width and each contact's horizontal bounds for MRI overlays."""
    xs = [round(value, 6) for value in x_positions[:count]] if len(x_positions) >= count else [0.] * count
    if not all(math.isfinite(value) for value in xs):
        xs = [0.] * count
    shanks = list(shanks[:count]) if len(shanks) >= count else [0] * count
    columns = {}
    cursor = 0
    for shank in sorted(set(shanks)):
        positions = sorted({xs[channel] for channel in range(count) if shanks[channel] == shank})
        for position in positions:
            columns[shank, position] = (cursor, cursor + contact_width - 1)
            cursor += contact_width + spacing
        cursor += spacing
    bounds = [columns[shanks[channel], xs[channel]] for channel in range(count)]
    return max((right + 1 for _, right in bounds), default=contact_width), bounds


def probe_map_layout(count, x_positions, depths, shanks, left, right, height,
                     horizontal_pixels=20, vertical_pixels=6, spacing=0):
    """Return shank bounds and pads at the user's exact pixel dimensions.

    Geometry is in mm. X is relative to each shank, depth increases away from
    the tip. Keep physical spacing/staggering while giving every pad the same
    integer pixel dimensions, including the outermost rows and columns.
    """
    def coordinates(values, fallback):
        if len(values) >= count and all(math.isfinite(value) for value in values[:count]):
            return list(values[:count])
        return fallback

    xs = coordinates(x_positions, [0.] * count)
    zs = coordinates(depths, [channel * .01 for channel in range(count)])
    shanks = list(shanks[:count]) if len(shanks) >= count else [0] * count
    shank_ids = sorted(set(shanks)) or [0]
    groups = {shank: [channel for channel in range(count) if shanks[channel] == shank] for shank in shank_ids}
    x_pitch = min((_pitch([xs[c] for c in channels], .016) for channels in groups.values()), default=.016)
    z_pitch = min((_pitch([zs[c] for c in channels], .01) for channels in groups.values()), default=.01)
    x_extent = max((max(xs[c] for c in channels) - min(xs[c] for c in channels) + x_pitch
                    for channels in groups.values() if channels), default=x_pitch)
    min_z, max_z = (min(zs), max(zs)) if zs else (0., 0.)
    top = 12.
    pad_width, pad_height = int(horizontal_pixels), int(vertical_pixels)
    spacing = int(spacing)
    if pad_width < 1 or pad_height < 1 or spacing < 0:
        raise ValueError('Pad dimensions must be positive and spacing nonnegative.')
    columns = x_extent / x_pitch
    gap = max(14, spacing * 2)
    x_scale, z_scale = (pad_width + spacing) / x_pitch, (pad_height + spacing) / z_pitch
    body_width = (columns - 1) * (pad_width + spacing) + pad_width + 12
    group_width = body_width * len(groups) + gap * (len(groups) - 1)
    group_left = max(left + 12, (left + right - group_width) / 2)
    bottom = top + (max_z - min_z) * z_scale + pad_height
    bodies, pads = [], []
    for index, shank in enumerate(shank_ids):
        body_left = group_left + index * (body_width + gap)
        bodies.append((shank, body_left, body_left + body_width, top - 8, bottom + 8))
        channels = groups[shank]
        if not channels:
            continue
        mid_x = (min(xs[c] for c in channels) + max(xs[c] for c in channels)) / 2
        min_x = min(xs[c] for c in channels)
        x_origin = round(body_left + body_width / 2 + (min_x - mid_x) * x_scale - pad_width / 2)
        y_origin = round(bottom - pad_height)
        for channel in channels:
            x0 = x_origin + round((xs[channel] - min_x) * x_scale)
            y0 = y_origin - round((zs[channel] - min_z) * z_scale)
            pads.append((channel, (x0, y0, x0 + pad_width, y0 + pad_height)))
    return bodies, pads
