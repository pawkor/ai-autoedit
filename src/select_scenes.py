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
BACK_CAM_MAX_SEC = _cfg.getfloat("scene_selection", "back_cam_max_sec", fallback=10.0)
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
EMBEDDINGS_FILE  = os.environ.get("EMBEDDINGS_FILE",
                       str(Path(SCORES_CSV).parent / "scene_embeddings.npz"))
DUPLICATES_FILE  = os.environ.get("DUPLICATES_FILE",
                       str(Path(SCORES_CSV).parent / "scene_duplicates.json"))
DEDUP_SIM    = _cfg.getfloat("scene_selection", "dedup_sim",    fallback=0.97)
DEDUP_WINDOW = _cfg.getint("scene_selection",   "dedup_window", fallback=5)
TIMESTAMP_MATCH_SEC = _cfg.getfloat("scene_selection", "timestamp_match_sec", fallback=30.0)
MIN_GAP_SEC  = float(os.environ.get("MIN_GAP_SEC", "") or _cfg.getfloat("scene_selection", "min_gap_sec", fallback=0))
_CAM_OFFSETS: dict[str, float] = {}
try:
    _raw_offsets = os.environ.get("CAM_OFFSETS", "")
    if _raw_offsets:
        import json as _json_tmp
        _CAM_OFFSETS = {k: float(v) for k, v in _json_tmp.loads(_raw_offsets).items() if float(v) != 0}
except Exception:
    pass
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
TARGET_RESOLUTION = os.environ.get("TARGET_RESOLUTION", "")  # e.g. "3840:2160"
TARGET_FRAMERATE  = os.environ.get("TARGET_FRAMERATE",  "")  # e.g. "60"
USE_NVENC         = os.environ.get("USE_NVENC", "0") == "1"

import re as _re
import json as _json
import numpy as np

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
df['source'] = df['scene'].str.replace(r'-(scene|clip)-\d+$', '', regex=True)
cam_map = {}
if CAM_SOURCES and os.path.exists(CAM_SOURCES):
    cdf = pd.read_csv(CAM_SOURCES)
    cam_map = dict(zip(cdf['source'], cdf['camera']))

df['camera'] = df['source'].map(cam_map).fillna('default')
dual_cam = len(set(cam_map.values())) > 1

# Normalization only made sense when both cameras were scored — skip it now
# (scores CSV contains main cam only; back cam is selected by timestamp, not score)

df_all = df.copy()  # keep full df (with camera) for force-include lookups

# ── Near-duplicate detection via CLIP embeddings ─────────────────────────────
dup_scenes: set[str] = set()
emb_dict: dict[str, np.ndarray] = {}
if os.path.exists(EMBEDDINGS_FILE):
    try:
        _data = np.load(EMBEDDINGS_FILE, allow_pickle=False)
        for _name, _emb in zip(_data['names'].tolist(), _data['embeddings']):
            emb_dict[_name] = _emb
        print(f"Dedup: loaded {len(emb_dict)} embeddings (sim≥{DEDUP_SIM}, window={DEDUP_WINDOW})")
    except Exception as _e:
        print(f"  Warning: could not load embeddings for dedup: {_e}")

if emb_dict:
    _score_map = dict(zip(df['scene'], df['score']))
    for _source, _group in df.groupby('source'):
        _scenes = sorted(
            [s for s in _group['scene'].tolist() if s in emb_dict],
            key=lambda s: int(_re.search(r'-scene-(\d+)$', s).group(1))
                          if _re.search(r'-scene-(\d+)$', s) else 0,
        )
        for _i in range(len(_scenes)):
            for _j in range(_i + 1, min(_i + DEDUP_WINDOW + 1, len(_scenes))):
                _sim = float(np.dot(emb_dict[_scenes[_i]], emb_dict[_scenes[_j]]))
                if _sim >= DEDUP_SIM:
                    # Keep higher-scored; if tied, keep earlier
                    _si = _score_map.get(_scenes[_i], 0.0)
                    _sj = _score_map.get(_scenes[_j], 0.0)
                    _dup = _scenes[_j] if _si >= _sj else _scenes[_i]
                    if _dup not in force_include:
                        dup_scenes.add(_dup)
    if dup_scenes:
        print(f"  Dedup: {len(dup_scenes)} near-duplicate scene(s) removed")

if not DRY_RUN:
    try:
        if dup_scenes:
            Path(DUPLICATES_FILE).write_text(_json.dumps(sorted(dup_scenes)))
        elif os.path.exists(DUPLICATES_FILE):
            os.remove(DUPLICATES_FILE)
    except Exception:
        pass

# Apply force-exclude and dedup before selection
if force_exclude or dup_scenes:
    df = df[~df['scene'].isin(force_exclude | dup_scenes)]


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
# In DRY_RUN with a warm cache, accept scenes whose duration is cached even if the
# autocut file has been cleaned (avoids returning duration=0 when cache is valid).
if DRY_RUN and _dur_cache:
    candidates = candidates[
        candidates.apply(lambda r: os.path.exists(r['file']) or (Path(r['file']).name in _dur_cache), axis=1)
    ]
else:
    candidates = candidates[candidates['file'].apply(os.path.exists)]

if DRY_RUN:
    _exist = candidates['file'].apply(os.path.exists).sum()
    print(f"Candidates: {len(candidates)} (score≥{THRESHOLD:.4f}), files_exist={_exist}, cache={len(_dur_cache)}, scenes_dir={SCENES_DIR!r}")

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


# ── Build absolute timestamp map (used by gap filter and dual-cam pairing) ───
# Timeline position = creation_time of MP4 + scene start offset from CSV.
# Built here (not inside dual_cam block) so gap filtering works for single-cam too.
ts_map: dict[str, float] = {}
if CSV_DIR and os.path.isdir(CSV_DIR):
    _work_dir = Path(os.getcwd())
    _ffprobe  = _cfg.get("paths", "ffprobe", fallback="ffprobe")

    def _get_file_start(stem: str) -> float | None:
        cam = cam_map.get(stem)
        dirs_to_try = [_work_dir / cam] if cam else []
        dirs_to_try.append(_work_dir)
        for d in dirs_to_try:
            for ext in (".mp4", ".MP4", ".mov", ".MOV"):
                p = d / (stem + ext)
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
                    # Fallback: use file mtime when creation_time is absent
                    try:
                        mtime = p.stat().st_mtime
                        print(f"  [mtime fallback] {p.name}: {mtime:.0f}")
                        return mtime
                    except Exception:
                        pass
        return None

    for _csv_path in sorted(Path(CSV_DIR).glob("*-Scenes.csv")):
        _stem = _csv_path.stem[:-len("-Scenes")]
        _file_start = _get_file_start(_stem)
        if _file_start is None:
            continue
        try:
            _sdf = pd.read_csv(_csv_path)
            if "Scene Number" not in _sdf.columns:
                _sdf = pd.read_csv(_csv_path, skiprows=1)
            for _, _row in _sdf.iterrows():
                _snum = int(_row["Scene Number"])
                _key  = f"{_stem}-scene-{_snum:03d}"
                _secs = float(_row.get("Start Time (seconds)", 0) or 0)
                ts_map[_key] = _file_start + _secs
        except Exception:
            pass

    if _CAM_OFFSETS and ts_map:
        for _key in ts_map:
            _src = _re.sub(r'-scene-\d+$', '', _key)
            _cam = cam_map.get(_src, 'default')
            if _cam in _CAM_OFFSETS:
                ts_map[_key] += _CAM_OFFSETS[_cam]


# ── Minimum-gap filter (auto-selected scenes only) ───────────────────────────
# Applied after threshold + per-file cap + force-includes.
# force_include scenes are always kept regardless of gap.
if MIN_GAP_SEC > 0 and ts_map:
    _sorted = sorted(all_selected, key=lambda s: ts_map.get(s[0], float('inf')))
    _kept: list = []
    _last_ts: float | None = None
    _skipped = 0
    for _s in _sorted:
        _ts = ts_map.get(_s[0])
        if _s[0] in force_include:
            _kept.append(_s)
            if _ts is not None:
                _last_ts = _ts + _s[3]   # ts + take
            continue
        if _ts is None:
            _kept.append(_s)             # no timestamp → keep, can't judge gap
            continue
        if _last_ts is None or (_ts - _last_ts) >= MIN_GAP_SEC:
            _kept.append(_s)
            _last_ts = _ts + _s[3]
        else:
            _skipped += 1
    if _skipped:
        print(f"Min-gap filter ({MIN_GAP_SEC:.0f}s): removed {_skipped} scene(s) too close to predecessor")
    all_selected = _kept
elif MIN_GAP_SEC > 0 and not ts_map:
    print(f"  Warning: min_gap_sec={MIN_GAP_SEC:.0f} set but no timestamps available — gap filter skipped")


# ── Dual-cam: timestamp-based pairing ────────────────────────────────────────
cam_a_name = None

if dual_cam:
    # Build cam list from cam_map (not df — back cam no longer appears in scores CSV)
    all_cam_names = sorted(set(cam_map.values()))
    if AUDIO_CAM and AUDIO_CAM in all_cam_names:
        all_cam_names = [AUDIO_CAM] + [c for c in all_cam_names if c != AUDIO_CAM]
    cam_a_name  = all_cam_names[0]
    other_cams  = all_cam_names[1:]

    # ts_map already built above (outside dual_cam block) — reused here.
    if _CAM_OFFSETS and ts_map:
        print(f"  Cam offsets applied: " + ", ".join(f"{c}={v:+.0f}s" for c, v in _CAM_OFFSETS.items()))

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

    # Build back-cam entry list with timestamps, sorted by timestamp for fast scan.
    # ts_map is already clock-corrected per camera (filename drift applied above).
    back_entries = []
    for _, row in back_df.iterrows():
        sc  = row['scene']
        dur = duration_map.get(sc)
        if not dur:
            continue
        take = min(dur, MAX_SCENE_SEC, BACK_CAM_MAX_SEC)
        if take < MIN_TAKE_SEC:
            continue
        fp = f"{SCENES_DIR}{sc}.mp4"
        if not os.path.exists(fp):
            continue
        back_entries.append({
            "tuple": (sc, fp, dur, take, float(row['score']), row['camera']),
            "ts":    ts_map.get(sc),
            "dur":   dur,
        })
    back_entries.sort(key=lambda e: (e["ts"] or 0))

    # ── Pair each main-cam scene with closest back-cam scene ─────────────────
    other_str = "/".join(other_cams)
    back_has_ts = any(e["ts"] is not None for e in back_entries)

    if not back_has_ts and back_entries:
        # Fallback: no creation_time on back-cam (e.g. drone) — randomly interleave
        import random as _random
        _pool = list(back_entries)
        _random.shuffle(_pool)
        _bi = iter(_pool)
        _next_back = next(_bi, None)
        paired: list[tuple] = []
        for ms in main_sel:
            if _next_back is not None:
                paired.append((ms, _next_back["tuple"]))
                used_back: set[str] = set()
                used_back.add(_next_back["tuple"][0])
                _next_back = next(_bi, None)
            else:
                paired.append((ms, None))
        sampled = sum(1 for _, bs in paired if bs is not None)
        print(f"  No timestamps for {other_str} — random fallback: {sampled} scene(s) sampled")
    else:
        used_back: set[str] = set()
        paired: list[tuple] = []   # (main_tuple, back_tuple | None)
        no_match = 0

        for ms in main_sel:
            main_ts = ts_map.get(ms[0])
            # Target: moment right after the helmet clip ends — back cam shows what
            # happens next, not the same instant (ms[3] = take = how much we cut).
            target_ts = (main_ts + ms[3]) if main_ts is not None else None
            best, best_dist = None, float("inf")
            for be in back_entries:
                if be["tuple"][0] in used_back:
                    continue
                if target_ts is None or be["ts"] is None:
                    continue
                # Distance is 0 if target_ts falls inside the back scene's time range,
                # otherwise the gap to the nearer edge (start or end).
                be_end = be["ts"] + be["dur"]
                if target_ts < be["ts"]:
                    dist = be["ts"] - target_ts
                elif target_ts > be_end:
                    dist = target_ts - be_end
                else:
                    dist = 0.0
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
        print(f"  Timestamp match (→{other_str}): {paired_count}/{len(paired)} paired "
              f"(±{TIMESTAMP_MATCH_SEC:.0f}s)")
        if no_match:
            print(f"  {no_match} main-cam scene(s) had no back-cam match within ±{TIMESTAMP_MATCH_SEC:.0f}s")

        # Timestamps present but all mismatched (e.g. camera clock off by >30s) →
        # fall back to random interleaving so back-cam footage is still used.
        if paired_count == 0 and back_entries:
            import random as _random
            _pool = list(back_entries)
            _random.shuffle(_pool)
            _bi = iter(_pool)
            paired = []
            for ms in main_sel:
                _nb = next(_bi, None)
                paired.append((ms, _nb["tuple"] if _nb else None))
            sampled = sum(1 for _, bs in paired if bs is not None)
            print(f"  Timestamp match failed — random fallback: {sampled} back-cam scene(s) sampled")

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
_nvenc_sem = threading.Semaphore(3)  # max concurrent NVENC sessions (consumer GPU limit)


def _probe_video_format(path: str) -> tuple[int, int, float] | None:
    """Return (width, height, fps) of a video file, or None on failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10,
        )
        parts = r.stdout.strip().split(",")
        if len(parts) >= 3:
            w, h = int(parts[0]), int(parts[1])
            num, den = parts[2].split("/")
            fps = float(num) / float(den)
            return w, h, fps
    except Exception:
        pass
    return None


def prepare_clip(scene, scene_file, duration, take, camera):
    needs_trim = duration > take

    suffix = f"_t{take:.1f}" if needs_trim else "_enc"
    out = f"{TRIMMED_DIR}{scene}{suffix}.mp4"

    if os.path.exists(out):
        # Strict validity check: ffprobe must report no errors AND return a duration.
        # get_duration() with -v quiet can return a value for corrupt files (no moov atom).
        _chk = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", out],
            capture_output=True, text=True,
        )
        if _chk.returncode == 0 and not _chk.stderr.strip():
            try:
                float(_chk.stdout.strip())
                return out
            except (ValueError, TypeError):
                pass
        os.remove(out)  # corrupt (e.g. killed mid-encode or missing moov atom) — re-encode

    global _prep_counter
    with _prep_lock:
        _prep_counter += 1
        n = _prep_counter
    op = "trim" if needs_trim else "enc"
    print(f"  [{n}/{_prep_total}] {scene} ({op})", flush=True)

    # Detect if clip needs resolution/fps normalization
    vf_parts = []
    r_arg = []
    if TARGET_RESOLUTION and TARGET_FRAMERATE:
        fmt = _probe_video_format(scene_file)
        if fmt is not None:
            src_w, src_h, src_fps = fmt
            try:
                tgt_w, tgt_h = (int(x) for x in TARGET_RESOLUTION.split(":"))
                tgt_fps = float(TARGET_FRAMERATE)
            except Exception:
                tgt_w = tgt_h = tgt_fps = None
            if tgt_w and tgt_h and (src_w != tgt_w or src_h != tgt_h):
                vf_parts.append(
                    f"scale={tgt_w}:{tgt_h}:flags=lanczos"
                    f":force_original_aspect_ratio=decrease,"
                    f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2:color=black"
                )
            if tgt_fps and abs(src_fps - tgt_fps) > 0.1:
                r_arg = ["-r", str(int(tgt_fps) if tgt_fps == int(tgt_fps) else tgt_fps)]

    cmd = ["ffmpeg"]
    use_nvenc_here = USE_NVENC and not vf_parts
    if use_nvenc_here:
        cmd += ["-hwaccel", "cuda"]
    if needs_trim:
        start = duration / 2 - take / 2
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", scene_file]
    if needs_trim:
        cmd += ["-t", f"{take:.3f}"]
    if use_nvenc_here:
        cmd += ["-c:v", "h264_nvenc", "-rc", "constqp", "-qp", X264_CRF,
                "-preset", "p4", "-bf", "0", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                "-vsync", "cfr"]
    else:
        cmd += ["-c:v", "libx264", "-crf", X264_CRF, "-preset", X264_PRESET,
                "-bf", "0",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                "-vsync", "cfr"]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    cmd += r_arg

    cmd += ["-avoid_negative_ts", "make_zero", out, "-y", "-loglevel", "error"]
    if use_nvenc_here:
        with _nvenc_sem:
            result = subprocess.run(cmd, capture_output=True, text=True)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(out):
        err = result.stderr.strip().splitlines()
        print(f"  [ERROR] {scene} encode failed (rc={result.returncode}): {' | '.join(err[-3:]) if err else 'no output'}", flush=True)
        if os.path.exists(out):
            os.remove(out)
        return None
    # Validate output (catch silent moov-less writes)
    _val = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
        capture_output=True, text=True,
    )
    if _val.returncode != 0 or _val.stderr.strip():
        print(f"  [ERROR] {scene} output invalid after encode: {_val.stderr.strip()[:120]}", flush=True)
        os.remove(out)
        return None
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
failed_clips = sum(1 for c in clips if c is None)
if failed_clips:
    print(f"  [WARN] {failed_clips} clip(s) failed to encode — skipped from output", flush=True)
with open(OUTPUT_LIST, "w") as f:
    for clip in clips:
        if clip is not None:
            f.write(f"file '{clip}'\n")
for (scene, _, duration, take, score, camera), clip in zip(selected, clips):
    if clip is None:
        continue
    cam_tag = f"[{camera}] " if dual_cam else ""
    print(f"  {score:.3f}  {take:.0f}s  {cam_tag}{scene}")
