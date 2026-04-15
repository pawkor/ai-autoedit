#!/usr/bin/env python3
"""
gps_index.py — Extract GPS track from Insta360 (and compatible) MP4 files,
compute speed and turn rate per second, annotate scene_scores.csv.

Adds columns to scene_scores.csv (only when GPS data found):
  gps_speed_avg  — average speed (km/h) during the clip
  gps_speed_max  — peak speed (km/h) during the clip
  gps_turn_max   — peak turn rate (deg/s) — proxy for corner intensity

Pipeline integration:
  Called automatically after clip_score.py if GPS data is present.
  If exiftool is missing or no GPS tracks found, CSV is left unchanged.
"""
from __future__ import annotations

import csv
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS-84 points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (degrees, 0-360) from point 1 to point 2."""
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _bearing_diff(b1: float, b2: float) -> float:
    """Absolute angular difference between two bearings, in [0, 180]."""
    d = abs(b1 - b2)
    return d if d <= 180 else 360 - d


# ── DMS parser ────────────────────────────────────────────────────────────────

_DMS_RE = re.compile(r'(\d+)\s+deg\s+(\d+)\'\s+([\d.]+)"?\s*([NSEW])', re.I)

def _parse_dms(s: str) -> float:
    """'54 deg 30\' 10.30" N' → 54.502861"""
    m = _DMS_RE.match(s.strip())
    if not m:
        return float("nan")
    d, mn, sec, hemi = int(m[1]), int(m[2]), float(m[3]), m[4].upper()
    val = d + mn / 60 + sec / 3600
    return -val if hemi in ("S", "W") else val


# ── GPS extraction from MP4 ───────────────────────────────────────────────────

_TS_FMTS = (
    "%Y:%m:%d %H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
)

def _parse_ts(s: str) -> float | None:
    for fmt in _TS_FMTS:
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def extract_gps_track(mp4_path: Path, exiftool: str = "exiftool") -> list[dict]:
    """
    Extract GPS track from a single MP4 using exiftool -ee.
    Returns list of dicts: {ts, lat, lon, alt, speed_kmh, turn_deg_s}
    sorted by ts. Deduplicates by second (Insta360 stores ~10 entries/s at 60fps).
    Returns [] if no GPS or exiftool not available.
    """
    try:
        r = subprocess.run(
            [exiftool, "-ee", "-gpsdatetime", "-gpslatitude", "-gpslongitude",
             "-gpsaltitude", str(mp4_path)],
            capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError:
        return []   # exiftool not installed
    except subprocess.TimeoutExpired:
        print(f"  GPS: exiftool timeout on {mp4_path.name}", file=sys.stderr)
        return []

    # Parse lines: build one sample per unique GPS Date/Time
    # Each second is repeated N times (once per video frame) — keep last seen lat/lon
    samples: dict[str, dict] = {}
    cur_ts: str | None = None

    for line in r.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()

        if key == "GPS Date/Time":
            cur_ts = val
            if cur_ts not in samples:
                samples[cur_ts] = {}
        elif cur_ts is None:
            continue
        elif key == "GPS Latitude":
            lat = _parse_dms(val)
            if not math.isnan(lat):
                samples[cur_ts]["lat"] = lat
        elif key == "GPS Longitude":
            lon = _parse_dms(val)
            if not math.isnan(lon):
                samples[cur_ts]["lon"] = lon
        elif key == "GPS Altitude":
            try:
                samples[cur_ts]["alt"] = float(val.split()[0])
            except (ValueError, IndexError):
                pass

    # Build sorted list of valid points
    track: list[dict] = []
    for ts_str, d in samples.items():
        if "lat" not in d or "lon" not in d:
            continue
        ts = _parse_ts(ts_str)
        if ts is None:
            continue
        track.append({"ts": ts, "lat": d["lat"], "lon": d["lon"],
                       "alt": d.get("alt", 0.0)})

    track.sort(key=lambda x: x["ts"])

    # Compute speed (km/h) and turn rate (deg/s) between consecutive 1-second samples
    for i, pt in enumerate(track):
        if i == 0:
            pt["speed_kmh"] = 0.0
            pt["turn_deg_s"] = 0.0
            continue
        prev = track[i - 1]
        dt = pt["ts"] - prev["ts"]
        if dt <= 0:
            pt["speed_kmh"] = 0.0
            pt["turn_deg_s"] = 0.0
            continue
        dist_m = _haversine_m(prev["lat"], prev["lon"], pt["lat"], pt["lon"])
        pt["speed_kmh"] = (dist_m / dt) * 3.6
        if i >= 2:
            b_prev = _bearing(track[i - 2]["lat"], track[i - 2]["lon"],
                               prev["lat"], prev["lon"])
            b_cur  = _bearing(prev["lat"], prev["lon"], pt["lat"], pt["lon"])
            pt["turn_deg_s"] = _bearing_diff(b_cur, b_prev) / dt
        else:
            pt["turn_deg_s"] = 0.0

    return track


# ── Index builder ─────────────────────────────────────────────────────────────

_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts"}

def build_gps_index(
    work_dir: Path,
    exiftool: str = "exiftool",
    rebuild: bool = False,
) -> dict[str, list[dict]]:
    """
    Extract GPS tracks from all source MP4 files in work_dir.
    Caches result to _autoframe/gps_index.json.
    Returns {source_stem: [track_points]}.
    """
    auto_dir   = work_dir / "_autoframe"
    cache_path = auto_dir / "gps_index.json"

    if not rebuild and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            total = sum(len(v) for v in data.values())
            print(f"  GPS index: loaded {total} samples from cache ({len(data)} files)")
            return data
        except Exception:
            pass

    mp4_files = [
        f for f in sorted(work_dir.rglob("*"))
        if f.suffix.lower() in _VIDEO_EXT
        and "_autoframe" not in f.parts
        and not f.name.lower().endswith(".lrv")
    ]
    if not mp4_files:
        return {}

    print(f"  GPS index: extracting from {len(mp4_files)} source files…")
    index: dict[str, list[dict]] = {}
    found = 0
    for mp4 in mp4_files:
        track = extract_gps_track(mp4, exiftool=exiftool)
        if track:
            index[mp4.stem] = track
            found += 1
            print(f"    ✓ {mp4.name}: {len(track)} samples  "
                  f"speed max {max(p['speed_kmh'] for p in track):.0f} km/h")
        else:
            print(f"    – {mp4.name}: no GPS")

    if found == 0:
        print("  GPS index: no GPS data found — skipping annotation")
        return {}

    auto_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(index, separators=(",", ":")))
    print(f"  GPS index: saved {sum(len(v) for v in index.values())} samples → {cache_path.name}")
    return index


def load_gps_index(auto_dir: Path) -> dict[str, list[dict]]:
    """Load cached GPS index. Returns {} if not found."""
    cache_path = auto_dir / "gps_index.json"
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text())
    except Exception:
        return {}


# ── Clip time helpers ─────────────────────────────────────────────────────────

def _clip_start_ts(clip_path: Path, ffprobe: str = "ffprobe") -> float | None:
    """Return clip creation_time as unix timestamp, or None."""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format_tags=creation_time",
             "-of", "csv=p=0", str(clip_path)],
            capture_output=True, text=True, timeout=10,
        )
        return _parse_ts(r.stdout.strip())
    except Exception:
        return None


def _clip_duration(clip_path: Path, ffprobe: str = "ffprobe") -> float:
    """Return clip duration in seconds, or 10.0 as fallback."""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(clip_path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return 10.0


# ── GPS metrics for a clip ────────────────────────────────────────────────────

def _gps_metrics(track: list[dict], clip_start: float, clip_dur: float) -> dict:
    """
    Aggregate GPS metrics for a clip's time window [clip_start, clip_start+clip_dur].
    Returns {} if no GPS samples overlap (clip_start may be off — don't annotate).
    """
    clip_end = clip_start + clip_dur
    pts = [p for p in track if clip_start <= p["ts"] <= clip_end]
    if not pts:
        return {}
    speeds = [p["speed_kmh"] for p in pts]
    turns  = [p["turn_deg_s"] for p in pts]
    return {
        "gps_speed_avg": round(sum(speeds) / len(speeds), 1),
        "gps_speed_max": round(max(speeds), 1),
        "gps_turn_max":  round(max(turns),  1),
    }


# ── CSV annotation ────────────────────────────────────────────────────────────

GPS_COLS = ["gps_speed_avg", "gps_speed_max", "gps_turn_max"]
_STEM_RE = re.compile(r"-(scene|clip)-\d+$")

def annotate_scores_csv(
    scores_csv: Path,
    autocut_dir: Path,
    gps_index: dict[str, list[dict]],
    ffprobe: str = "ffprobe",
    cam_offsets: dict[str, float] | None = None,
) -> bool:
    """
    Add GPS columns to scores_csv in-place.
    cam_offsets: {camera_name: offset_seconds} — same as [cam_offsets] in config.ini.
    Returns True if at least one clip was annotated.
    """
    if not scores_csv.exists() or not gps_index:
        return False

    text = scores_csv.read_text()
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        return False

    existing_cols = list(rows[0].keys())
    cam_offsets = cam_offsets or {}
    annotated = 0

    for row in rows:
        scene = row["scene"]
        src_stem = _STEM_RE.sub("", scene)
        track = gps_index.get(src_stem)
        if not track:
            for col in GPS_COLS:
                row.setdefault(col, "")
            continue

        clip_path = autocut_dir / f"{scene}.mp4"
        clip_start = _clip_start_ts(clip_path, ffprobe=ffprobe)
        if clip_start is None:
            for col in GPS_COLS:
                row.setdefault(col, "")
            continue

        # Apply camera time offset (e.g. helmet cam drift)
        # Detect camera from source stem → camera_sources.csv or stem subdirectory
        # For simplicity, check which camera directory this source belongs to
        _cam_dir = None
        for cam_name in cam_offsets:
            if (clip_path.parent.parent / cam_name / f"{src_stem}.mp4").exists():
                _cam_dir = cam_name
                break
        if _cam_dir and _cam_dir in cam_offsets:
            clip_start += cam_offsets[_cam_dir]

        clip_dur = _clip_duration(clip_path, ffprobe=ffprobe)
        metrics  = _gps_metrics(track, clip_start, clip_dur)
        if metrics:
            row.update(metrics)
            annotated += 1
        else:
            for col in GPS_COLS:
                row.setdefault(col, "")

    if annotated == 0:
        print("  GPS annotate: no clips matched GPS track "
              "(check cam_offsets or creation_time metadata)")
        return False

    # Write back — preserve existing columns, append new GPS ones
    all_cols = existing_cols + [c for c in GPS_COLS if c not in existing_cols]
    lines = [",".join(all_cols)]
    for row in rows:
        lines.append(",".join(str(row.get(c, "")) for c in all_cols))
    scores_csv.write_text("\n".join(lines) + "\n")

    print(f"  GPS annotate: {annotated}/{len(rows)} clips ← "
          f"speed/turn from {len(gps_index)} source files")
    return True


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import configparser

    ap = argparse.ArgumentParser(
        description="Extract GPS from Insta360 MP4s and annotate scene_scores.csv")
    ap.add_argument("work_dir", help="Job work directory (same as pipeline)")
    ap.add_argument("--exiftool", default="exiftool")
    ap.add_argument("--ffprobe",  default="ffprobe")
    ap.add_argument("--rebuild",  action="store_true",
                    help="Rebuild GPS index even if gps_index.json cache exists")
    args = ap.parse_args()

    work_dir = Path(args.work_dir)
    auto_dir = work_dir / "_autoframe"

    cfg = configparser.ConfigParser()
    cfg.read([str(Path(__file__).parent.parent / "config.ini"),
              str(work_dir / "config.ini")])
    ffprobe  = cfg.get("paths", "ffprobe",  fallback=args.ffprobe)
    exiftool = cfg.get("paths", "exiftool", fallback=args.exiftool)

    cam_offsets: dict[str, float] = {}
    if cfg.has_section("cam_offsets"):
        for k, v in cfg.items("cam_offsets"):
            try:
                cam_offsets[k] = float(v)
            except ValueError:
                pass

    gps_index = build_gps_index(work_dir, exiftool=exiftool, rebuild=args.rebuild)
    if not gps_index:
        print("No GPS data found.")
        sys.exit(0)

    allcam_csv = auto_dir / "scene_scores_allcam.csv"
    scores_csv = allcam_csv if allcam_csv.exists() else auto_dir / "scene_scores.csv"
    autocut_dir = auto_dir / "autocut"

    ok = annotate_scores_csv(
        scores_csv, autocut_dir, gps_index,
        ffprobe=ffprobe, cam_offsets=cam_offsets,
    )
    print("Done." if ok else "No changes made.")
