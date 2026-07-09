"""Color utilities for SpikeGLX-style probe maps."""

from __future__ import annotations

import csv
import os
from pathlib import Path


_FALLBACK_STOPS = [
    (0.00, (0, 0, 0)),
    (0.18, (0, 0, 120)),
    (0.35, (90, 0, 220)),
    (0.52, (210, 0, 150)),
    (0.68, (255, 60, 0)),
    (0.84, (255, 190, 0)),
    (1.00, (255, 255, 255)),
]


def load_shank_lut() -> list[str]:
    """Load SpikeGLX's shank LUT when available, otherwise use a close fallback."""
    for path in _lut_search_paths():
        if not path.exists():
            continue
        try:
            with path.open("r", newline="") as handle:
                rows = list(csv.DictReader(handle))
            lut = ["#000000"] * 256
            for row in rows:
                index = int(row["Index"])
                red = int(row["Red"])
                green = int(row["Green"])
                blue = int(row["Blue"])
                if 0 <= index < 256:
                    lut[index] = f"#{red:02x}{green:02x}{blue:02x}"
            return lut
        except (OSError, KeyError, ValueError):
            continue
    return [_fallback_color(index / 255.0) for index in range(256)]


def color_from_lut(lut: list[str], value: float, min_value: float, max_value: float) -> str:
    """Map a value into a 256-color LUT using SpikeGLX-style clipping."""
    if max_value <= min_value:
        max_value = min_value + 1.0
    if value <= min_value:
        index = 0
    elif value >= max_value:
        index = 255
    else:
        index = int(255 * (value - min_value) / (max_value - min_value))
    return lut[max(0, min(255, index))]


def _lut_search_paths() -> list[Path]:
    paths: list[Path] = []
    env_path = os.environ.get("SPIKEGLX_SHANK_LUT", "").strip()
    if env_path:
        paths.append(Path(env_path))

    module_path = Path(__file__).resolve()
    repo_root = module_path.parents[2]
    paths.extend(
        [
            repo_root / "Resources" / "CommonResources" / "shanklut.csv",
            repo_root / "SpikeGLX" / "Resources" / "CommonResources" / "shanklut.csv",
            repo_root.parent / "SpikeGLX" / "Resources" / "CommonResources" / "shanklut.csv",
        ]
    )
    return paths


def _fallback_color(value: float) -> str:
    value = max(0.0, min(1.0, value))
    for idx in range(1, len(_FALLBACK_STOPS)):
        left_value, left_color = _FALLBACK_STOPS[idx - 1]
        right_value, right_color = _FALLBACK_STOPS[idx]
        if value <= right_value:
            fraction = (value - left_value) / max(1e-9, right_value - left_value)
            red = int(left_color[0] + (right_color[0] - left_color[0]) * fraction)
            green = int(left_color[1] + (right_color[1] - left_color[1]) * fraction)
            blue = int(left_color[2] + (right_color[2] - left_color[2]) * fraction)
            return f"#{red:02x}{green:02x}{blue:02x}"
    return "#ffffff"
