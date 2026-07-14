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

    # Collect GPS fields as parallel lists — handles both interleaved (Insta360)
    # and grouped (Ace Pro 2: all timestamps first, then all lats, etc.) output.
    ts_list:  list[str]   = []
    lat_list: list[float] = []
    lon_list: list[float] = []
    alt_list: list[float] = []

    for line in r.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()

        if key == "GPS Date/Time":
            ts_list.append(val)
        elif key == "GPS Latitude":
            lat = _parse_dms(val)
            if not math.isnan(lat):
                lat_list.append(lat)
        elif key == "GPS Longitude":
            lon = _parse_dms(val)
            if not math.isnan(lon):
                lon_list.append(lon)
        elif key == "GPS Altitude":
            try:
                alt_list.append(float(val.split()[0]))
            except (ValueError, IndexError):
                alt_list.append(0.0)

    # Zip by position (truncate to shortest list), deduplicate by second
    n = min(len(ts_list), len(lat_list), len(lon_list))
    if n == 0:
        return []

    samples: dict[str, dict] = {}
    for i in range(n):
        ts_str = ts_list[i]
        samples[ts_str] = {
            "lat": lat_list[i],
            "lon": lon_list[i],
            "alt": alt_list[i] if i < len(alt_list) else 0.0,
        }

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

    # Compute speed (km/h), turn rate (deg/s), altitude change rate (m/s)
    for i, pt in enumerate(track):
        if i == 0:
            pt["speed_kmh"]    = 0.0
            pt["turn_deg_s"]   = 0.0
            pt["alt_change_ms"] = 0.0
            continue
        prev = track[i - 1]
        dt = pt["ts"] - prev["ts"]
        if dt <= 0:
            pt["speed_kmh"]    = 0.0
            pt["turn_deg_s"]   = 0.0
            pt["alt_change_ms"] = 0.0
            continue
        dist_m = _haversine_m(prev["lat"], prev["lon"], pt["lat"], pt["lon"])
        pt["speed_kmh"]    = (dist_m / dt) * 3.6
        pt["alt_change_ms"] = abs(pt["alt"] - prev["alt"]) / dt
        if i >= 2:
            b_prev = _bearing(track[i - 2]["lat"], track[i - 2]["lon"],
                               prev["lat"], prev["lon"])
            b_cur  = _bearing(prev["lat"], prev["lon"], pt["lat"], pt["lon"])
            pt["turn_deg_s"] = _bearing_diff(b_cur, b_prev) / dt
        else:
            pt["turn_deg_s"] = 0.0

    return track


def gps_excitement_series(
    track: list[dict],
    creation_time: float,
    frame_offsets: list[float],
    altitude_threshold_m: float = 400.0,
) -> list[float]:
    """
    Compute GPS excitement for each frame (given as offsets in seconds from video start).
    Excitement = turn_rate + alt_change + altitude_bonus, normalized to 0-1.
    Returns [0.0]*len(frame_offsets) when no GPS data.
    """
    if not track or not frame_offsets:
        return [0.0] * len(frame_offsets)

    # Raw excitement per GPS point
    ts_excite: list[tuple[float, float]] = []
    for pt in track:
        turn = pt.get("turn_deg_s",    0.0)
        ac   = pt.get("alt_change_ms", 0.0)
        alt  = pt.get("alt",           0.0)
        base = turn * 2.0 + ac * 4.0
        if alt > altitude_threshold_m:
            base *= 1.0 + min((alt - altitude_threshold_m) / 700.0, 0.6)
        ts_excite.append((pt["ts"], base))

    # Smooth ±3s window
    smoothed: list[tuple[float, float]] = []
    n = len(ts_excite)
    for i, (ts, _) in enumerate(ts_excite):
        window = [ts_excite[j][1] for j in range(max(0, i - 3), min(n, i + 4))]
        smoothed.append((ts, sum(window) / len(window)))

    # Normalize to 0-1 (99th percentile)
    vals = sorted(v for _, v in smoothed)
    p99  = vals[max(0, int(0.99 * len(vals)) - 1)] if vals else 1.0
    if p99 < 0.001:
        p99 = 1.0
    ts_arr = [t for t, _ in smoothed]
    ex_arr = [min(v / p99, 1.0) for _, v in smoothed]

    # Nearest GPS lookup per frame offset
    result: list[float] = []
    for offset in frame_offsets:
        abs_ts = creation_time + offset
        lo, hi, best = 0, len(ts_arr) - 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            best = mid
            if ts_arr[mid] < abs_ts:
                lo = mid + 1
            else:
                hi = mid - 1
        candidates = [i for i in (best - 1, best, best + 1) if 0 <= i < len(ts_arr)]
        closest = min(candidates, key=lambda i: abs(ts_arr[i] - abs_ts))
        result.append(ex_arr[closest] if abs(ts_arr[closest] - abs_ts) <= 30 else 0.0)

    return result


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
            # Invalidate cache if it predates alt_change_ms field
            _sample = next((v[1] for v in data.values() if len(v) > 1), None)
            if _sample and "alt_change_ms" not in _sample:
                print("  GPS index: cache outdated (no alt_change_ms) — rebuilding")
            else:
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
        and not f.stem.endswith("_preview")
        and not any(seg in f.stem for seg in ("-md_v", "-v0", "-v1", "-v2", "-v3", "-v4", "-v5"))
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
    speeds   = [p["speed_kmh"]     for p in pts]
    alts     = [p["alt"]           for p in pts]
    alt_chgs = [p.get("alt_change_ms", 0.0) for p in pts]
    # Turn rate only from moving samples (≥5 km/h) — eliminates GPS calibration
    # artifacts where the device appears to spin at 180°/s while stationary.
    moving = [p for p in pts if p.get("speed_kmh", 0) >= 5.0]
    turns  = [p["turn_deg_s"] for p in moving] if moving else [0.0]
    return {
        "gps_speed_avg":      round(sum(speeds)   / len(speeds), 1),
        "gps_speed_max":      round(max(speeds),                 1),
        "gps_turn_max":       round(max(turns),                  1),
        "gps_altitude_avg":   round(sum(alts)     / len(alts),   1),
        "gps_alt_change_max": round(max(alt_chgs),               2),
    }


# ── CSV annotation ────────────────────────────────────────────────────────────

GPS_COLS = ["gps_speed_avg", "gps_speed_max", "gps_turn_max",
            "gps_altitude_avg", "gps_alt_change_max"]
_STEM_RE = re.compile(r"-(scene|clip)-\d+$")

def annotate_scores_csv(
    scores_csv: Path,
    autocut_dir: Path,
    gps_index: dict[str, list[dict]],
    ffprobe: str = "ffprobe",
    cam_offsets: dict[str, float] | None = None,
    work_dir: Path | None = None,
) -> bool:
    """
    Add GPS columns to scores_csv in-place.
    cam_offsets: {camera_name: offset_seconds} — same as [cam_offsets] in config.ini.
    work_dir: used to find source files when clips lack creation_time (NVENC strips it).
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

        # GPS track timestamps are absolute UTC (from satellites) — reliable
        # even when device clock is wrong (Insta360 default-date bug).
        # Skip ffprobe on clips: NVENC strips creation_time anyway, saving
        # 1 subprocess call per clip (hundreds of calls per project).
        try:
            clip_start = track[0]["ts"] + float(row.get("offset_sec") or 0)
        except (TypeError, ValueError, IndexError):
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
