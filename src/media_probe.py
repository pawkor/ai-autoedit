"""Media probing helpers — THE single copy.

The audit found seven diverged creation-time readers (different extension
sets, timeouts, timestamp formats, fallbacks) and a dozen video-extension
lists. Consolidated here; every consumer must import from this module.
"""
import json
import subprocess
from datetime import datetime
from pathlib import Path

# Ordinary video sources the pipeline can scan/split directly.
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".ts"}
# Insta360 dual-fisheye recordings: only the front-lens file carries the
# recording's identity (LRV previews and _10_ companions share timestamps
# and would pollute cadence statistics).
INSV_EXT = ".insv"


def is_time_source(path: Path) -> bool:
    """Files whose creation_time may enter chronology statistics."""
    suf = path.suffix.lower()
    if suf in VIDEO_EXTS:
        return True
    return suf == INSV_EXT and "_00_" in path.name


def parse_creation_time(ts: str):
    """Timestamp string → epoch float, or None. Accepts ISO8601 (Z or
    numeric offset) plus the exiftool-style 'YYYY:MM:DD HH:MM:SSZ' some
    camera metadata uses."""
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    from datetime import timezone
    for fmt in ("%Y:%m:%d %H:%M:%SZ", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)\
                .replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def probe_nvenc(ffmpeg: str = "ffmpeg", timeout: float = 15.0) -> tuple[bool, str]:
    """Real NVENC runtime probe: encodes one frame to a null sink.

    `ffmpeg -encoders` only proves h264_nvenc is COMPILED in — it says
    nothing about whether the driver/CUDA context actually works right now
    (2026-10-06: a broken host driver left `-encoders` listing it fine while
    every real encode failed with cuInit CUDA_ERROR_UNKNOWN). Callers must
    use this, not a string search over `-encoders`, before trusting NVENC.
    Returns (ok, diagnostic) — diagnostic is ffmpeg's stderr tail on failure.

    256x256 test frame, not smaller: NVIDIA's documented H.264 NVENC minimum
    is 145x49 (Turing+), and a too-small probe frame risks a false negative
    on a perfectly healthy GPU — i.e. exactly the silent-failure class this
    function exists to catch, just inverted (2026-10-06 audit caught this in
    the first version, which used 64x64).
    """
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
             "-c:v", "h264_nvenc", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=timeout)
    except Exception as e:
        return False, f"probe failed to run: {e}"
    if r.returncode != 0:
        return False, r.stderr.decode(errors="replace").strip()[-400:]
    return True, ""


def creation_epoch(path: Path, ffprobe: str = "ffprobe",
                   timeout: float = 10.0):
    """format_tags.creation_time of a media file → epoch float, or None.
    No filesystem-mtime fallback here: per CLAUDE.md, metadata is the only
    trusted time source — callers wanting a fallback must opt in loudly."""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet",
             "-show_entries", "format_tags=creation_time",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout)
        tags = (json.loads(r.stdout or "{}").get("format", {})
                .get("tags") or {})
        return parse_creation_time(tags.get("creation_time", ""))
    except Exception:
        return None
