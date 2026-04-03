#!/usr/bin/env python3
import pandas as pd
import subprocess
import sys
import os
import configparser
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_cfg = configparser.ConfigParser()
_script_dir = Path(__file__).resolve().parent
_cfg.read([_script_dir / "config.ini", Path.cwd() / "config.ini"])

THRESHOLD        = float(sys.argv[1]) if len(sys.argv) > 1 else _cfg.getfloat("scene_selection", "threshold",        fallback=0.148)
MAX_SCENE_SEC    = float(sys.argv[2]) if len(sys.argv) > 2 else _cfg.getfloat("scene_selection", "max_scene_sec",    fallback=10)
MAX_PER_FILE_SEC = float(sys.argv[3]) if len(sys.argv) > 3 else _cfg.getfloat("scene_selection", "max_per_file_sec", fallback=45)
MIN_TAKE_SEC     = _cfg.getfloat("scene_selection", "min_take_sec", fallback=0.5)
WORKERS          = _cfg.getint("scene_selection",   "workers",      fallback=min(os.cpu_count() or 1, 12))
X264_CRF         = str(_cfg.getint("video", "x264_crf",    fallback=15))
X264_PRESET      = _cfg.get("video",        "x264_preset", fallback="fast")

SCENES_DIR  = os.environ.get("SCENES_DIR",  "autocut/")
TRIMMED_DIR = os.environ.get("TRIMMED_DIR", "trimmed/")
SCORES_CSV  = os.environ.get("OUTPUT_CSV",  "scene_scores.csv")
OUTPUT_LIST = os.environ.get("OUTPUT_LIST", "selected_scenes.txt")
CAM_SOURCES      = os.environ.get("CAM_SOURCES",      "")
CSV_DIR          = os.environ.get("CSV_DIR",          "")
AUDIO_CAM        = os.environ.get("AUDIO_CAM",        "")
MANUAL_OVERRIDES = os.environ.get("MANUAL_OVERRIDES", "")
TIMESTAMP_MATCH_SEC = _cfg.getfloat("scene_selection", "timestamp_match_sec", fallback=30.0)
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

import re as _re
import json as _json

# ── Persistent ffprobe duration cache ────────────────────────────────────────
# DRY_RUN: built here and saved to disk so binary-search iterations are fast.
# Always: loaded (if present) to derive per-file CSV inflation ratios used by
# timestamp matching — PySceneDetect "Start Time (seconds)" is ~10x too large
# for VFR files due to container timebase mismatch.
_dur_cache: dict[str, float] = {}
_dur_cache_path: Path | None = None
if SCENES_DIR:
    _dur_cache_path = Path(SCENES_DIR).parent / "duration_cache.json"
    if _dur_cache_path.exists():
        try:
            _dur_cache = _json.loads(_dur_cache_path.read_text())
        except Exception:
            _dur_cache = {}
_ov = {}
if MANUAL_OVERRIDES and os.path.exists(MANUAL_OVERRIDES):
    _ov = _json.load(open(MANUAL_OVERRIDES))
force_include = {k for k, v in _ov.items() if v == 'include'}
force_exclude = {k for k, v in _ov.items() if v == 'exclude'}
if force_include or force_exclude:
    print(f"Manual overrides: +{len(force_include)} forced in, -{len(force_exclude)} forced out")

os.makedirs(TRIMMED_DIR, exist_ok=True)

df = pd.read_csv(SCORES_CSV)
df['source'] = df['scene'].str.replace(r'-scene-\d+$', '', regex=True)
cam_map = {}
if CAM_SOURCES and os.path.exists(CAM_SOURCES):
    cdf = pd.read_csv(CAM_SOURCES)
    cam_map = dict(zip(cdf['source'], cdf['camera']))

df['camera'] = df['source'].map(cam_map).fillna('default')
dual_cam = len(set(cam_map.values())) > 1

# Normalization only made sense when both cameras were scored — skip it now
# (scores CSV contains main cam only; back cam is selected by timestamp, not score)

df_all = df.copy()  # keep full df (with camera) for force-include lookups

# Apply force-exclude before selection
if force_exclude:
    df = df[~df['scene'].isin(force_exclude)]


_dur_cache_dirty = False

def get_duration(scene_file):
    global _dur_cache_dirty
    key = Path(scene_file).name
    if key in _dur_cache:
        return _dur_cache[key]
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", scene_file],
            capture_output=True, text=True
        )
        dur = float(result.stdout.strip())
        if DRY_RUN:
            _dur_cache[key] = dur
            _dur_cache_dirty = True
        return dur
    except:
        return None


# ── Pre-fetch durations in parallel ──────────────────────────────────────────
candidates = df[df['score'] >= THRESHOLD].copy()
candidates['file'] = SCENES_DIR + candidates['scene'] + '.mp4'
candidates = candidates[candidates['file'].apply(os.path.exists)]

with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    futures = {ex.submit(get_duration, row['file']): row['scene']
               for _, row in candidates.iterrows()}
    duration_map = {}
    for future in as_completed(futures):
        scene = futures[future]
        duration_map[scene] = future.result()



def select_from_group(group_df):
    file_total = 0
    result = []
    for _, row in group_df[group_df['score'] >= THRESHOLD].sort_values('score', ascending=False).iterrows():
        if file_total >= MAX_PER_FILE_SEC:
            break

        duration = duration_map.get(row['scene'])
        if duration is None:
            continue

        take = min(duration, MAX_SCENE_SEC)
        take = min(take, MAX_PER_FILE_SEC - file_total)
        if take < MIN_TAKE_SEC:
            continue

        scene_file = f"{SCENES_DIR}{row['scene']}.mp4"
        result.append((row['scene'], scene_file, duration, take, row['score'], row['camera']))
        file_total += take
    return result


# ── Select scenes per source file ────────────────────────────────────────────
all_selected = []
for source, group in df.groupby('source'):
    all_selected.extend(select_from_group(group))

# ── Apply force-includes (add scenes not already selected) ───────────────────
if force_include:
    selected_scenes = {s[0] for s in all_selected}
    for scene in force_include:
        if scene in selected_scenes:
            continue
        scene_file = f"{SCENES_DIR}{scene}.mp4"
        if not os.path.exists(scene_file):
            continue
        dur = get_duration(scene_file)
        if not dur:
            continue
        row = df_all[df_all['scene'] == scene]
        score  = float(row['score'].iloc[0])  if len(row) else 0.0
        camera = str(row['camera'].iloc[0])   if len(row) else 'default'
        take   = min(dur, MAX_SCENE_SEC)
        all_selected.append((scene, scene_file, dur, take, score, camera))
        print(f"  Force-include: {scene} ({take:.0f}s, score {score:.3f})")

def _scene_timestamp(scene_tuple):
    """Sortable key: (file_prefix, scene_number) from scene name."""
    name = scene_tuple[0]
    m = _re.search(r'(\d{8}_\d{6}[^-]*)-scene-(\d+)', name)
    if m:
        return (m.group(1), int(m.group(2)))
    return (name, 0)


# ── Dual-cam: timestamp-based pairing ────────────────────────────────────────
cam_a_name = None

if dual_cam:
    # Build cam list from cam_map (not df — back cam no longer appears in scores CSV)
    all_cam_names = sorted(set(cam_map.values()))
    if AUDIO_CAM and AUDIO_CAM in all_cam_names:
        all_cam_names = [AUDIO_CAM] + [c for c in all_cam_names if c != AUDIO_CAM]
    cam_a_name  = all_cam_names[0]
    other_cams  = all_cam_names[1:]

    # ── Build absolute timestamp map from PySceneDetect CSVs ─────────────────
    # Primary: creation_time from MP4 metadata (epoch seconds) — works for all
    # cameras including Osmo Action which doesn't encode time in filenames.
    # Fallback: HHMMSS parsed from filename (seconds since midnight).

    ts_map: dict[str, float] = {}
    if os.path.isdir(CSV_DIR):
        _work_dir = Path(os.getcwd())
        _ffprobe  = _cfg.get("paths", "ffprobe", fallback="ffprobe")

        def _get_file_start(stem: str) -> float | None:
            """Return file start time: creation_time epoch (primary), filename HHMMSS (fallback)."""
            cam = cam_map.get(stem)
            if cam:
                for ext in (".mp4", ".MP4", ".mov", ".MOV"):
                    p = _work_dir / cam / (stem + ext)
                    if p.exists():
                        try:
                            out = subprocess.check_output(
                                [_ffprobe, "-v", "quiet", "-print_format", "json",
                                 "-show_format", str(p)],
                                stderr=subprocess.DEVNULL, timeout=10,
                            )
                            tags = _json.loads(out)["format"].get("tags", {})
                            ct = tags.get("creation_time", "")
                            if ct:
                                from datetime import datetime
                                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                                return dt.timestamp()
                        except Exception:
                            pass
            # Fallback: HHMMSS from filename
            m = _re.search(r'_(\d{6})(?:_\d+)*$', stem)
            if m:
                hms = m.group(1)
                return int(hms[0:2]) * 3600 + int(hms[2:4]) * 60 + int(hms[4:6])
            return None

        for csv_path in sorted(Path(CSV_DIR).glob("*-Scenes.csv")):
            stem = csv_path.stem[:-len("-Scenes")]
            file_start = _get_file_start(stem)
            if file_start is None:
                print(f"  Warning: no timestamp for {stem} (no creation_time, no filename timestamp)")
                continue
            try:
                sdf = pd.read_csv(csv_path, skiprows=1)
                for _, row in sdf.iterrows():
                    snum = int(row["Scene Number"])
                    key  = f"{stem}-scene-{snum:03d}"
                    secs = float(row.get("Start Time (seconds)", 0) or 0)
                    ts_map[key] = file_start + secs
            except Exception:
                pass

    ts_coverage = sum(1 for s in all_selected if ts_map.get(s[0]) is not None)
    if not ts_map:
        print(f"  Warning: no timestamps available (CSV_DIR={CSV_DIR!r}), using score-only order")

    # ── Select from main cam only; back cam matched by timestamp ─────────────
    main_sel = sorted([s for s in all_selected if s[5] == cam_a_name],
                      key=_scene_timestamp)
    print(f"  Main cam ({cam_a_name}): {len(main_sel)} scenes selected")

    # Build back-cam scene list from filesystem (not scores CSV — back cam isn't scored)
    back_sources = {src for src, cam in cam_map.items() if cam in other_cams}
    back_rows = []
    for sc_file in sorted(Path(SCENES_DIR).glob("*.mp4")):
        stem = sc_file.stem
        src  = _re.sub(r'-scene-\d+$', '', stem)
        if src in back_sources:
            back_rows.append({'scene': stem, 'source': src,
                              'camera': cam_map[src], 'score': 0.0})
    back_df = pd.DataFrame(back_rows) if back_rows else pd.DataFrame(
        columns=['scene', 'source', 'camera', 'score'])

    # Pre-fetch durations for all back-cam scenes (they weren't all fetched above)
    missing_scenes = [row['scene'] for _, row in back_df.iterrows()
                      if row['scene'] not in duration_map
                      and os.path.exists(f"{SCENES_DIR}{row['scene']}.mp4")]
    if missing_scenes:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(get_duration, f"{SCENES_DIR}{sc}.mp4"): sc
                    for sc in missing_scenes}
            for fut in as_completed(futs):
                duration_map[futs[fut]] = fut.result()

    # ── Detect clock offset between cameras ──────────────────────────────────
    # If back cam clock was wrong (e.g. DST off by 1h), all back timestamps will
    # be shifted by a constant. Detect by trying candidate offsets and picking the
    # one that maximises matching pairs within ±TIMESTAMP_MATCH_SEC.
    main_ts_vals = [ts_map[s[0]] for s in all_selected
                    if s[5] == cam_a_name and ts_map.get(s[0]) is not None]
    back_ts_sources = set()
    back_ts_raw: list[float] = []
    for _, row in back_df.iterrows():
        sc = row['scene']
        t = ts_map.get(sc)
        if t is not None:
            back_ts_raw.append(t)
            back_ts_sources.add(sc)

    cam_clock_offset = 0.0
    if main_ts_vals and back_ts_raw:
        best_count, best_off = 0, 0.0
        # Try whole-hour candidates ±12h and also 0
        candidates = [h * 3600 for h in range(-12, 13)]
        tol = TIMESTAMP_MATCH_SEC
        for off in candidates:
            adjusted = [t - off for t in back_ts_raw]
            count = sum(
                1 for bt in adjusted
                if any(abs(bt - mt) <= tol for mt in main_ts_vals)
            )
            if count > best_count:
                best_count, best_off = count, off
        if best_off != 0.0 and best_count > 0:
            cam_clock_offset = best_off
            h = int(best_off / 3600)
            print(f"  Clock offset detected: back cam is {h:+d}h — correcting timestamps")

    # Build back-cam entry list with timestamps, sorted by timestamp for fast scan
    back_entries = []
    for _, row in back_df.iterrows():
        sc  = row['scene']
        dur = duration_map.get(sc)
        if not dur:
            continue
        take = min(dur, MAX_SCENE_SEC)
        if take < MIN_TAKE_SEC:
            continue
        fp = f"{SCENES_DIR}{sc}.mp4"
        if not os.path.exists(fp):
            continue
        raw_ts = ts_map.get(sc)
        adj_ts = (raw_ts - cam_clock_offset) if raw_ts is not None else None
        back_entries.append({
            "tuple": (sc, fp, dur, take, float(row['score']), row['camera']),
            "ts":    adj_ts,
        })
    back_entries.sort(key=lambda e: (e["ts"] or 0))

    # ── Pair each main-cam scene with closest back-cam scene ─────────────────
    used_back: set[str] = set()
    paired: list[tuple] = []   # (main_tuple, back_tuple | None)
    no_match = 0

    for ms in main_sel:
        main_ts = ts_map.get(ms[0])
        best, best_dist = None, float("inf")
        for be in back_entries:
            if be["tuple"][0] in used_back:
                continue
            if main_ts is None or be["ts"] is None:
                continue
            dist = abs(be["ts"] - main_ts)
            if dist <= TIMESTAMP_MATCH_SEC and dist < best_dist:
                best_dist = dist
                best = be
        if best:
            paired.append((ms, best["tuple"]))
            used_back.add(best["tuple"][0])
        else:
            paired.append((ms, None))
            no_match += 1

    paired_count = len(paired) - no_match
    other_str    = "/".join(other_cams)
    print(f"  Timestamp match (→{other_str}): {paired_count}/{len(paired)} paired "
          f"(±{TIMESTAMP_MATCH_SEC:.0f}s)")
    if no_match:
        print(f"  {no_match} main-cam scene(s) had no back-cam match within ±{TIMESTAMP_MATCH_SEC:.0f}s")

    # ── Ensure back-cam scenes appear in chronological order ─────────────────
    # VFR timebase inflation causes ts_map values to drift, so timestamp-based
    # pairing may assign back-cam scenes from the same source file out of scene
    # order.  Re-sort assignments within each source file so the video stays
    # chronological even if individual pairings are approximate.
    from collections import defaultdict
    _back_src_idx: dict[str, list[int]] = defaultdict(list)
    for _i, (_ms, _bs) in enumerate(paired):
        if _bs is not None:
            _src = _re.sub(r'-scene-\d+$', '', _bs[0])
            _back_src_idx[_src].append(_i)
    for _src, _idxs in _back_src_idx.items():
        if len(_idxs) < 2:
            continue
        _back_tups = [paired[_i][1] for _i in _idxs]
        _back_tups.sort(key=lambda t: int(_re.search(r'-scene-(\d+)$', t[0]).group(1)))
        for _k, _i in enumerate(_idxs):
            paired[_i] = (paired[_i][0], _back_tups[_k])

    # ── Interleave: main[0], back[0], main[1], back[1], … ───────────────────
    selected = []
    for ms, bs in paired:
        selected.append(ms)
        if bs:
            selected.append(bs)

    final_counts = {}
    for s in selected:
        final_counts[s[5]] = final_counts.get(s[5], 0) + 1
    print(f"Multi-cam ({len(all_cam_names)} cams): " +
          ", ".join(f"{c}={final_counts.get(c, 0)}" for c in all_cam_names))

else:
    selected = sorted(all_selected, key=_scene_timestamp)

_prep_counter = 0
_prep_lock = threading.Lock()
_prep_total = 0


def prepare_clip(scene, scene_file, duration, take, camera):
    needs_trim = duration > take

    suffix = f"_t{take:.1f}" if needs_trim else "_enc"
    out = f"{TRIMMED_DIR}{scene}{suffix}.mp4"

    if os.path.exists(out):
        if get_duration(out) is not None:
            return out
        os.remove(out)  # corrupt (e.g. killed mid-encode) — re-encode

    global _prep_counter
    with _prep_lock:
        _prep_counter += 1
        n = _prep_counter
    op = "trim" if needs_trim else "enc"
    print(f"  [{n}/{_prep_total}] {scene} ({op})", flush=True)

    cmd = ["ffmpeg"]
    if needs_trim:
        start = duration / 2 - take / 2
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", scene_file]
    if needs_trim:
        cmd += ["-t", f"{take:.3f}"]
    cmd += ["-c:v", "libx264", "-crf", X264_CRF, "-preset", X264_PRESET,
            "-bf", "0",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-vsync", "cfr"]

    cmd += ["-avoid_negative_ts", "make_zero", out, "-y", "-loglevel", "quiet"]
    subprocess.run(cmd)
    return out


total = sum(t for _, _, _, t, _, _ in selected)
print(f"Threshold: {THRESHOLD}")
print(f"Selected: {len(selected)} scenes")
print(f"Total: {int(total//60)}:{int(total%60):02d} ({total:.1f}s)")

# Persist all newly-probed durations (candidates + inflation probes) to cache
if DRY_RUN and _dur_cache_dirty and _dur_cache_path:
    try:
        _dur_cache_path.write_text(_json.dumps(_dur_cache))
    except Exception:
        pass

if DRY_RUN:
    sys.exit(0)

# ── Prepare clips in parallel, preserve order ─────────────────────────────────
_prep_total = len(selected)
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    clip_futures = [
        ex.submit(prepare_clip, scene, scene_file, duration, take, camera)
        for scene, scene_file, duration, take, score, camera in selected
    ]
    clips = [f.result() for f in clip_futures]

# ── Write concat list ─────────────────────────────────────────────────────────
with open(OUTPUT_LIST, "w") as f:
    for clip in clips:
        f.write(f"file '{clip}'\n")
for scene, _, duration, take, score, camera in selected:
    cam_tag = f"[{camera}] " if dual_cam else ""
    print(f"  {score:.3f}  {take:.0f}s  {cam_tag}{scene}")
