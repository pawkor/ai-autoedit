"""Per-project color correction — build ffmpeg vf chain from 5 sliders.

Used by music_driven.py, make_shorts.py, and the /picture-preview endpoint
so the live preview, pool thumbnails, and final render share one source of truth.
"""
from __future__ import annotations

# Default values — neutral (no-op chain).
DEFAULTS = {
    "brightness":  0.0,   # eq brightness:  -0.30 .. +0.30
    "gamma":       1.0,   # eq gamma:        0.40 .. 1.60  (1.0 neutral)
    "contrast":    1.0,   # eq contrast:     0.70 .. 1.50
    "saturation":  1.0,   # eq saturation:   0.40 .. 1.60
    "temperature": 0.0,   # colorbalance:   -1.00 .. +1.00 (warm > 0, cool < 0)
}

# eps — treat values within this distance of default as no-op.
_EPS = 1e-3


def _close(a: float, b: float) -> bool:
    return abs(a - b) < _EPS


def build_vf_chain(brightness: float = 0.0,
                   gamma: float = 1.0,
                   contrast: float = 1.0,
                   saturation: float = 1.0,
                   temperature: float = 0.0) -> str:
    """Return ffmpeg -vf chain string (or '' when all params are at defaults)."""
    eq_parts: list[str] = []
    if not _close(brightness, 0.0):
        eq_parts.append(f"brightness={brightness:.3f}")
    if not _close(contrast, 1.0):
        eq_parts.append(f"contrast={contrast:.3f}")
    if not _close(gamma, 1.0):
        eq_parts.append(f"gamma={gamma:.3f}")
    if not _close(saturation, 1.0):
        eq_parts.append(f"saturation={saturation:.3f}")

    parts: list[str] = []
    if eq_parts:
        parts.append("eq=" + ":".join(eq_parts))
    if not _close(temperature, 0.0):
        # Warm (positive) shifts red up, blue down. Scale 0.2 keeps changes subtle at ±1.
        rh = temperature * 0.20
        bh = -temperature * 0.20
        parts.append(f"colorbalance=rh={rh:.3f}:bh={bh:.3f}")

    return ",".join(parts)


def chain_from_cp(cp, section: str = "color_correct") -> str:
    """Read 5 keys from configparser and build chain. Honours legacy vf_chain override."""
    explicit = cp.get(section, "vf_chain", fallback="").strip()
    if explicit:
        return explicit
    return build_vf_chain(
        brightness  = cp.getfloat(section, "brightness",  fallback=DEFAULTS["brightness"]),
        gamma       = cp.getfloat(section, "gamma",       fallback=DEFAULTS["gamma"]),
        contrast    = cp.getfloat(section, "contrast",    fallback=DEFAULTS["contrast"]),
        saturation  = cp.getfloat(section, "saturation",  fallback=DEFAULTS["saturation"]),
        temperature = cp.getfloat(section, "temperature", fallback=DEFAULTS["temperature"]),
    )
