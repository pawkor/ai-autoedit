"""Scene-name identity helpers — THE single copy.

The audit found 16 diverged copies of the "strip -scene-/-clip- suffix"
regex across the codebase (some missing -photo-, one matching only clip).
Diverged copies are exactly how fixed bugs return: every consumer must
import from here.
"""
import re

# Terminal suffix produced by the pipeline's extractors/injectors.
SCENE_SUFFIX_RE = re.compile(r'-(?:scene|clip|photo)-\d+$')

# JS mirror (webapp/static/js): keep in sync manually —
#   /-(?:scene|clip|photo)-\d+$/


def source_of(scene: str) -> str:
    """'VID_x_00_001-clip-014' → 'VID_x_00_001'; names without a known
    suffix are returned unchanged."""
    return SCENE_SUFFIX_RE.sub('', scene)


def is_photo(scene: str) -> bool:
    return bool(re.search(r'-photo-\d+$', scene))
