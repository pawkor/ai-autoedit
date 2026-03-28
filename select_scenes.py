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
TIER1_CUTOFF     = _cfg.getfloat("scene_selection", "tier1_cutoff", fallback=0.145)
TIER1_LIMIT      = _cfg.getfloat("scene_selection", "tier1_limit",  fallback=10)
TIER2_CUTOFF     = _cfg.getfloat("scene_selection", "tier2_cutoff", fallback=0.150)
TIER2_LIMIT      = _cfg.getfloat("scene_selection", "tier2_limit",  fallback=20)
MIN_TAKE_SEC     = _cfg.getfloat("scene_selection", "min_take_sec", fallback=0.5)
WORKERS          = _cfg.getint("scene_selection",   "workers",      fallback=min(os.cpu_count() or 1, 12))
X264_CRF         = str(_cfg.getint("video", "x264_crf",    fallback=15))
X264_PRESET      = _cfg.get("video",        "x264_preset", fallback="fast")

SCENES_DIR  = os.environ.get("SCENES_DIR",  "autocut/")
TRIMMED_DIR = os.environ.get("TRIMMED_DIR", "trimmed/")
SCORES_CSV  = os.environ.get("OUTPUT_CSV",  "scene_scores.csv")
OUTPUT_LIST = os.environ.get("OUTPUT_LIST", "selected_scenes.txt")
CAM_SOURCES = os.environ.get("CAM_SOURCES", "")
AUDIO_CAM   = os.environ.get("AUDIO_CAM",   "")

os.makedirs(TRIMMED_DIR, exist_ok=True)

df = pd.read_csv(SCORES_CSV)
df['source'] = df['scene'].str.replace(r'-scene-\d+$', '', regex=True)

cam_map = {}
if CAM_SOURCES and os.path.exists(CAM_SOURCES):
    cdf = pd.read_csv(CAM_SOURCES)
    cam_map = dict(zip(cdf['source'], cdf['camera']))

df['camera'] = df['source'].map(cam_map).fillna('default')
dual_cam = len(cam_map) > 0 and df['camera'].nunique() > 1


def get_duration(scene_file):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", scene_file],
            capture_output=True, text=True
        )
        return float(result.stdout.strip())
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
    top_score = group_df['score'].max()
    if top_score < TIER1_CUTOFF:
        file_limit = TIER1_LIMIT
    elif top_score < TIER2_CUTOFF:
        file_limit = TIER2_LIMIT
    else:
        file_limit = MAX_PER_FILE_SEC

    file_total = 0
    result = []
    for _, row in group_df[group_df['score'] >= THRESHOLD].sort_values('score', ascending=False).iterrows():
        if file_total >= file_limit:
            break

        duration = duration_map.get(row['scene'])
        if duration is None:
            continue

        take = min(duration, MAX_SCENE_SEC)
        take = min(take, file_limit - file_total)
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

# ── Interleave cameras if dual-cam mode ──────────────────────────────────────
cam_a_name = None
cam_b_name = None

if dual_cam:
    cameras = df['camera'].unique().tolist()
    if AUDIO_CAM and AUDIO_CAM in cameras:
        cam_a_name = AUDIO_CAM
        cam_b_name = next(c for c in cameras if c != AUDIO_CAM)
    else:
        cam_a_name = cameras[0]
        cam_b_name = cameras[1]

    cam_a = sorted([s for s in all_selected if s[5] == cam_a_name], key=lambda x: x[0])
    cam_b = sorted([s for s in all_selected if s[5] == cam_b_name], key=lambda x: x[0])
    other = sorted([s for s in all_selected if s[5] not in (cam_a_name, cam_b_name)], key=lambda x: x[0])

    interleaved = []
    for i in range(max(len(cam_a), len(cam_b))):
        if i < len(cam_a): interleaved.append(cam_a[i])
        if i < len(cam_b): interleaved.append(cam_b[i])
    selected = interleaved + other

    print(f"Dual-cam interleave: {cam_a_name}={len(cam_a)} scenes, {cam_b_name}={len(cam_b)} scenes")
else:
    selected = sorted(all_selected, key=lambda x: x[0])

audio_cam = cam_a_name if dual_cam else None


_prep_counter = 0
_prep_lock = threading.Lock()
_prep_total = 0


def prepare_clip(scene, scene_file, duration, take, camera):
    needs_trim = duration > take
    needs_mute = dual_cam and camera != audio_cam

    suffix  = "_trimmed" if needs_trim else ""
    suffix += "_muted"   if needs_mute else ""
    if not suffix:
        suffix = "_enc"
    out = f"{TRIMMED_DIR}{scene}{suffix}.mp4"

    if os.path.exists(out):
        return out

    ops = []
    if needs_trim: ops.append("trim")
    if needs_mute: ops.append("mute")
    if not ops:    ops.append("enc")
    global _prep_counter
    with _prep_lock:
        _prep_counter += 1
        n = _prep_counter
    print(f"  [{n}/{_prep_total}] {scene} ({', '.join(ops)})", flush=True)

    cmd = ["ffmpeg"]
    if needs_trim:
        start = duration / 2 - take / 2
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", scene_file]

    if needs_mute:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += ["-t", f"{take:.3f}"]
        cmd += ["-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-crf", X264_CRF, "-preset", X264_PRESET,
                "-bf", "0",
                "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        if needs_trim:
            cmd += ["-t", f"{take:.3f}"]
        cmd += ["-c:v", "libx264", "-crf", X264_CRF, "-preset", X264_PRESET,
                "-bf", "0",
                "-c:a", "copy"]

    cmd += ["-avoid_negative_ts", "make_zero", out, "-y", "-loglevel", "quiet"]
    subprocess.run(cmd)
    return out


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

total = sum(t for _, _, _, t, _, _ in selected)
print(f"Threshold: {THRESHOLD}")
print(f"Selected: {len(selected)} scenes")
print(f"Total: {total:.1f}s ({total/60:.1f} min)")
for scene, _, duration, take, score, camera in selected:
    cam_tag = f"[{camera}] " if dual_cam else ""
    print(f"  {score:.3f}  {take:.0f}s  {cam_tag}{scene}")
