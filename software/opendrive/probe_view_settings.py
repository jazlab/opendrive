"""Defaults and validation shared by probe windows and workspace persistence."""
import math


DEFAULT_PROBE_VIEW_SETTINGS = dict(min_mv=0., max_mv=.2, update_ms=200,
                                   horizontal_pixels=20, vertical_pixels=6, spacing=0)


def probe_view_settings(values=None, fallback=None, strict=False):
    base = {**DEFAULT_PROBE_VIEW_SETTINGS, **(fallback or {})}
    if not isinstance(values, dict):
        if strict:
            raise ValueError('Probe view settings must be an object.')
        return base
    result = dict(base)
    for key in base:
        if key not in values:
            continue
        try:
            if isinstance(values[key], bool):
                raise ValueError
            number = float(values[key])
            if not math.isfinite(number):
                raise ValueError
            if key not in ('min_mv', 'max_mv'):
                if number != int(number):
                    raise ValueError
                number = int(number)
                minimum = 0 if key == 'spacing' else 100 if key == 'update_ms' else 1
                if number < minimum or (key != 'update_ms' and number > 256):
                    raise ValueError
            result[key] = number
        except (ValueError, TypeError, OverflowError) as error:
            if strict:
                raise ValueError(f'Invalid probe view setting: {key}.') from error
    if result['max_mv'] <= result['min_mv']:
        if strict:
            raise ValueError('Probe view maximum must be greater than its minimum.')
        result['min_mv'], result['max_mv'] = base['min_mv'], base['max_mv']
    result['update_ms'] = result['update_ms'] // 100 * 100
    return result
