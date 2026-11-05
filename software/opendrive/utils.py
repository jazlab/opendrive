"""Formatting helpers for OpenDrive."""

def format_mm(value: float) -> str:
    """Format a millimeter value to nearest micron."""
    return f"{value:.3f}"
