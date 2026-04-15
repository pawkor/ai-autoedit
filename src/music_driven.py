#!/usr/bin/env python3
"""
music_driven.py — Music-to-Motion Alignment highlight assembler.

Instead of selecting scenes and dropping music on top, this module:
  1. Analyses the music track → beats + energy envelope
  2. Builds a cut schedule: high-energy sections → fast cuts, low → slow
  3. Computes motion profiles for top CLIP-scored clips (OpenCV frame diff)
  4. Matches clips to slots: high-energy slot → high CLIP + high motion clip
  5. Aligns each clip's motion peak to the beat hit (Motion Anchor)
  6. Renders directly — chronological order ignored

Inputs:
  _autoframe/autocut/*.mp4     — clips from clip_scan
  _autoframe/scene_scores.csv  — CLIP scores
  <music_file>                 — track to drive the edit

Output:
  _autoframe/highlight_music_driven.mp4
"""
from __future__ import annotations

import csv
import os
import random
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

_WORKERS = min(os.cpu_count() or 4, 12)


# ── Music analysis ────────────────────────────────────────────────────────────

def analyze_music(music_path: Path) -> dict:
    """
    Returns: duration, tempo, beat_times[], beat_energy[] (normalised 0-1).
    """
    import warnings
    warnings.filterwarnings("ignore", message=".*PySoundFile.*")
    warnings.filterwarnings("ignore", message=".*audioread.*")
    import librosa

    print(f"  Music: {music_path.name} …", end="", flush=True)
    # Decode via ffmpeg → temp WAV so PySoundFile handles it (avoids audioread deprecation)
    ffmpeg = "ffmpeg"
    _tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    _tmp.close()
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(music_path),
             "-ar", "22050", "-ac", "1", _tmp.name],
            check=True,
        )
        y, sr = librosa.load(_tmp.name, sr=None, mono=True)
    finally:
        Path(_tmp.name).unlink(missing_ok=True)
    duration = len(y) / sr

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.squeeze(tempo))  # librosa ≥0.10 returns 0-dim array
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()

    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)

    # Smooth ~1 s window so chorus/verse boundaries are clear
    win = max(1, int(sr / hop))
    rms_smooth = np.convolve(rms, np.ones(win) / win, mode="same")

    beat_energy = np.interp(beat_times, rms_times, rms_smooth)
    e_min, e_max = beat_energy.min(), beat_energy.max()
    if e_max > e_min:
        beat_energy = (beat_energy - e_min) / (e_max - e_min)
    else:
        beat_energy = np.ones(len(beat_times)) * 0.5

    print(f" {duration:.1f}s  {tempo:.0f} BPM  {len(beat_times)} beats")
    return {
        "duration": duration,
        "tempo":      tempo,
        "beat_times": beat_times,
        "beat_energy": beat_energy.tolist(),
    }


# ── Cut schedule ──────────────────────────────────────────────────────────────

def build_schedule(
    beat_times: list[float],
    beat_energy: list[float],
    beats_fast: int = 3,
    beats_mid: int = 4,
    beats_slow: int = 6,
) -> list[dict]:
    """
    Group consecutive beats into shot slots.
    High energy  (>0.65) → beats_fast beats/shot  (~chorus)
    Medium energy         → beats_mid  beats/shot
    Low energy   (<0.35) → beats_slow beats/shot  (~verse/scenic)
    """
    schedule: list[dict] = []
    n = len(beat_times)
    i = 0
    while i < n - 1:
        energy = beat_energy[i]
        n_beats = beats_fast if energy > 0.65 else (beats_slow if energy < 0.35 else beats_mid)
        end_i = min(i + n_beats, n - 1)
        dur = beat_times[end_i] - beat_times[i]
        if dur >= 0.4:
            schedule.append({
                "start":    beat_times[i],
                "end":      beat_times[end_i],
                "duration": dur,
                "energy":   float(energy),
                "n_beats":  n_beats,
            })
        i = end_i

    n_fast = sum(1 for s in schedule if s["n_beats"] == beats_fast)
    n_mid  = sum(1 for s in schedule if s["n_beats"] == beats_mid)
    n_slow = sum(1 for s in schedule if s["n_beats"] == beats_slow)
    print(f"  Schedule: {len(schedule)} slots  "
          f"fast={n_fast}({beats_fast}b)  mid={n_mid}({beats_mid}b)  slow={n_slow}({beats_slow}b)  "
          f"total={schedule[-1]['end']:.1f}s")
    return schedule


# ── Motion analysis ───────────────────────────────────────────────────────────

def motion_profile(clip_path: Path, n_samples: int = 24) -> tuple[float, float]:
    """
    (motion_peak_time, mean_motion_level) for a clip.
    motion_peak_time  — seconds where inter-frame diff is highest
    mean_motion_level — average diff magnitude (unnormalised)
    """
    import cv2

    cap = cv2.VideoCapture(str(clip_path))
    fps   = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 2:
        cap.release()
        return 0.0, 0.0

    indices = np.linspace(0, total - 1, min(n_samples, total), dtype=int)
    frames: list[tuple[float, np.ndarray]] = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if ok:
            small = cv2.resize(frame, (160, 90))
            gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            frames.append((int(idx) / fps, gray))
    cap.release()

    if len(frames) < 2:
        return 0.0, 0.0

    diffs = [(frames[i][0], float(np.mean(np.abs(frames[i][1] - frames[i-1][1]))))
             for i in range(1, len(frames))]

    diff_vals = [d for _, d in diffs]
    peak_t    = diffs[int(np.argmax(diff_vals))][0]
    return peak_t, float(np.mean(diff_vals))


def analyse_clips(autocut_dir: Path, scene_scores: dict,
                  top_percent: float, ffprobe: str,
                  stem_to_camera: dict | None = None,
                  stem_to_time: dict | None = None) -> list[dict]:
    """
    Compute motion profiles for the top_percent% of CLIP-scored clips.
    Returns list sorted by CLIP score descending.
    """
    sorted_scenes = sorted(scene_scores.items(), key=lambda x: x[1], reverse=True)
    cutoff     = max(1, int(len(sorted_scenes) * top_percent))
    candidates = list(sorted_scenes[:cutoff])
    print(f"  Motion pass: top {top_percent*100:.0f}% → {len(candidates)}/{len(sorted_scenes)} clips")

    # Guarantee each camera source has at least MIN_PER_SOURCE candidates
    # (prevents low-scoring cameras like drone from being completely excluded)
    _MIN_PER_SOURCE = 3
    _in_candidates = {s for s, _ in candidates}
    _by_source: dict[str, list] = {}
    for scene, score in sorted_scenes:
        _by_source.setdefault(_clip_source(scene), []).append((scene, score))
    _rescued = 0
    for src, scenes in _by_source.items():
        _count = sum(1 for s, _ in scenes if s in _in_candidates)
        for scene, score in scenes:
            if _count >= _MIN_PER_SOURCE:
                break
            if scene not in _in_candidates:
                candidates.append((scene, score))
                _in_candidates.add(scene)
                _count += 1
                _rescued += 1
    if _rescued:
        print(f"  Per-source rescue: +{_rescued} clips to ensure {_MIN_PER_SOURCE}/source minimum")

    def _analyse_one(item):
        i, (scene, score) = item
        clip_path = autocut_dir / f"{scene}.mp4"
        if not clip_path.exists():
            return None
        try:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(clip_path)],
                capture_output=True, text=True, timeout=5
            )
            clip_dur = float(r.stdout.strip())
        except Exception:
            clip_dur = 0.0
        if clip_dur < 0.5:
            return None
        peak_t, motion_lvl = motion_profile(clip_path)
        src = _clip_source(scene)
        return {
            "scene":          scene,
            "score":          score,
            "path":           clip_path,
            "duration":       clip_dur,
            "motion_peak":    peak_t,
            "motion_level":   motion_lvl,
            "camera":         stem_to_camera.get(src, "unknown") if stem_to_camera else "unknown",
            "clip_time_norm": stem_to_time.get(src) if stem_to_time else None,
        }

    clips: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {pool.submit(_analyse_one, item): item for item in enumerate(candidates)}
        for fut in as_completed(futures):
            done += 1
            result = fut.result()
            if result:
                clips.append(result)
            if done % 25 == 0:
                print(f"    {done}/{len(candidates)}…")
    # Restore original score order (as_completed is unordered)
    clips.sort(key=lambda c: c["score"], reverse=True)

    # Normalise motion_level to [0, 1]
    if clips:
        ml = [c["motion_level"] for c in clips]
        lo, hi = min(ml), max(ml)
        for c in clips:
            c["motion_norm"] = (c["motion_level"] - lo) / (hi - lo + 1e-6)

    print(f"  Clips ready: {len(clips)}")
    return clips


# ── Matching ──────────────────────────────────────────────────────────────────

import re as _re

def _clip_source(scene: str) -> str:
    """Base source file stem: strip trailing -(clip|scene)-NNN."""
    return _re.sub(r'-(clip|scene)-\d+$', '', scene)


def _parse_cam_pattern(pattern: str, cameras: list[str]) -> list[str] | None:
    """
    Parse a camera pattern string like "aabaab" into a list of camera names.
    Letters mapped in order of first appearance to cameras list (as provided — respects cam_a/cam_b order).
    e.g. pattern="aabaab", cameras=["helmet","back"] → ["helmet","helmet","back","helmet","helmet","back"]
         (a=helmet=cam_a, b=back=cam_b)
    Returns None if pattern is empty or fewer than 2 cameras available.
    """
    pattern = pattern.strip().lower()
    if not pattern or len(cameras) < 2:
        return None
    # Build letter→camera mapping: first unique letter = cameras[0], second = cameras[1], etc.
    letter_order: list[str] = []
    for ch in pattern:
        if ch not in letter_order:
            letter_order.append(ch)
    letter_to_cam = {letter_order[i]: cameras[i]
                     for i in range(min(len(letter_order), len(cameras)))}
    resolved = [letter_to_cam.get(ch) for ch in pattern]
    if any(r is None for r in resolved):
        return None
    return resolved  # type: ignore[return-value]


def match_clips(schedule: list[dict], clips: list[dict],
                chron_weight: float = 0.0,
                cam_pattern: str = "",
                cam_order: list[str] | None = None) -> list[dict]:
    """
    Assign best clip to each slot.
    Scoring per candidate (when chron_weight=0):
        CLIP_score × 0.60  +  energy_match × 0.40
    With chronological arc (chron_weight=0.20):
        CLIP_score × 0.50  +  energy_match × 0.30  +  chron_match × 0.20
    Camera diversity:
        cam_pattern set → cyclic pattern (e.g. "aabaab" → back/back/helmet repeating)
        cam_pattern empty → group-based (2-3 shots per camera, then switch)
    Source diversity: avoid repeating same source file within rolling window.
    """
    import collections
    used: set[str] = set()
    edit: list[dict] = []

    num_sources  = len({_clip_source(c["scene"]) for c in clips})
    # Use cam_order from config (cam_a first) — fall back to alphabetical
    _cam_set = {c.get("camera", "unknown") for c in clips}
    if cam_order:
        cameras = [c for c in cam_order if c in _cam_set] + \
                  sorted(c for c in _cam_set if c not in cam_order)
    else:
        cameras = sorted(_cam_set)
    num_cameras  = len(cameras)
    total_music_dur = schedule[-1]["end"] if schedule else 1.0
    # Only use chronological arc if clips actually have time info
    _has_time = any(c.get("clip_time_norm") is not None for c in clips)
    _chron_w  = chron_weight if _has_time else 0.0
    if _chron_w > 0:
        _timed = sum(1 for c in clips if c.get("clip_time_norm") is not None)
        print(f"  Chronological arc active: weight={_chron_w:.2f}  "
              f"timed clips={_timed}/{len(clips)}")

    # Rolling window: at least 2 sources worth of slots, minimum 4
    _src_window = max(4, num_sources * 2)
    recent_sources: collections.deque = collections.deque(maxlen=_src_window)

    # Camera pattern (cyclic). Empty = no camera preference (pure score-driven).
    _resolved_pattern = _parse_cam_pattern(cam_pattern, cameras)
    if _resolved_pattern:
        print(f"  Camera pattern: '{cam_pattern}' → {_resolved_pattern[:8]}… "
              f"(repeating every {len(_resolved_pattern)} slots)")
    else:
        print(f"  Camera pattern: none (score-driven, {num_cameras} camera(s))")
    _slot_idx = 0   # counts placed slots (for pattern indexing)

    for slot in schedule:
        dur    = slot["duration"]
        energy = slot["energy"]

        _desired_cam = (_resolved_pattern[_slot_idx % len(_resolved_pattern)]
                        if _resolved_pattern else None)

        def _pool(relax_dur: bool = False,
                  camera_filter: bool = True,
                  source_filter: bool = True) -> list[dict]:
            min_dur = dur if relax_dur else dur + 0.2
            return [
                c for c in clips
                if c["duration"] >= min_dur
                and c["scene"] not in used
                and (not source_filter or _clip_source(c["scene"]) not in recent_sources)
                and (not camera_filter or _desired_cam is None
                     or c.get("camera", "unknown") == _desired_cam)
            ]

        pool = _pool()
        if not pool: pool = _pool(relax_dur=True)
        if not pool: pool = _pool(source_filter=False)
        if not pool: pool = _pool(relax_dur=True, source_filter=False)
        if not pool: pool = _pool(camera_filter=False)
        if not pool: pool = _pool(relax_dur=True, camera_filter=False)
        if not pool:
            pool = [c for c in clips if c["duration"] >= dur and c["scene"] not in used]
        if not pool:
            continue  # skip slot — no reuse ever

        def rank(c: dict) -> float:
            motion_match = 1.0 - abs(energy - c.get("motion_norm", 0.5))
            if _chron_w > 0 and c.get("clip_time_norm") is not None:
                music_pos   = slot["start"] / total_music_dur
                chron_match = 1.0 - abs(music_pos - c["clip_time_norm"])
                return (c["score"] * 0.50
                        + motion_match  * 0.30
                        + chron_match   * _chron_w)
            return c["score"] * 0.60 + motion_match * 0.40

        best = max(pool, key=rank)
        used.add(best["scene"])
        recent_sources.append(_clip_source(best["scene"]))
        _slot_idx += 1

        # Motion anchor: place peak_motion at ~30% into the slot so the
        # "climax" of the action lands just after the beat hit
        anchor = dur * 0.3
        ideal_ss = best["motion_peak"] - anchor
        ss = max(0.0, min(ideal_ss, best["duration"] - dur))

        edit.append({
            "music_start": slot["start"],
            "duration":    dur,
            "energy":      energy,
            "n_beats":     slot["n_beats"],
            "scene":       best["scene"],
            "clip_path":   str(best["path"]),
            "clip_ss":     round(ss, 3),
            "clip_score":  round(best["score"], 4),
            "motion_peak": round(best["motion_peak"], 3),
        })

    covered = sum(e["duration"] for e in edit)
    unique  = len({e["scene"] for e in edit})
    # Camera distribution summary
    cam_counts: dict[str, int] = {}
    for c in clips:
        cam = c.get("camera", "unknown")
        if c["scene"] in {e["scene"] for e in edit}:
            cam_counts[cam] = cam_counts.get(cam, 0) + 1
    cam_str = "  ".join(f"{k}={v}" for k, v in sorted(cam_counts.items()))
    print(f"  Matched: {len(edit)} slots  {covered:.1f}s  unique={unique}  [{cam_str}]")
    return edit


# ── Rendering ────────────────────────────────────────────────────────────────

def render(edit: list[dict], music_path: Path, music_ss: float,
           output: Path, ffmpeg: str, nvenc: bool = True,
           resolution: str = "", framerate: str = "60") -> None:
    """
    Trim each clip to its slot duration, concat, overlay music.
    Uses NVENC if available (detected by nvenc flag).
    resolution: e.g. "3840:2160" — scale all clips to this; empty = preserve source.
    """
    enc_v = (
        ["-c:v", "h264_nvenc", "-rc", "constqp", "-qp", "22", "-preset", "p4",
         "-profile:v", "high", "-pix_fmt", "yuv420p"]
        if nvenc else
        ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p"]
    )

    # Build -vf filter: normalise resolution + fps so all clips are compatible for concat
    if resolution:
        w, h = resolution.split(":")
        _vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
               f"fps={framerate}")
        vf_args = ["-vf", _vf]
    else:
        vf_args = []

    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(_tmp)

        def _trim_one(args):
            i, entry = args
            out = tmp / f"s{i:04d}.mp4"
            cmd = [
                ffmpeg, "-y",
                "-ss", str(entry["clip_ss"]),
                "-t",  str(entry["duration"]),
                "-i",  entry["clip_path"],
                *enc_v, *vf_args, "-an",
                str(out)
            ]
            r = subprocess.run(cmd, capture_output=True)
            return (i, out) if (r.returncode == 0 and out.exists()) else (i, None)

        # Limit NVENC parallel encodes — GPU encoder has a session cap (typically 3-5)
        trim_workers = 3 if nvenc else _WORKERS
        results = {}
        with ThreadPoolExecutor(max_workers=trim_workers) as pool:
            for i, out in pool.map(_trim_one, list(enumerate(edit))):
                results[i] = out

        trimmed: list[Path] = []
        for i in range(len(edit)):
            out = results.get(i)
            if out:
                trimmed.append(out)
            else:
                print(f"  WARN: trim failed for {edit[i]['scene']}")

        if not trimmed:
            raise RuntimeError("All clip trims failed")

        print(f"  Trimmed {len(trimmed)}/{len(edit)} clips")

        # Concat video-only
        clist = tmp / "list.txt"
        clist.write_text("\n".join(f"file '{p}'" for p in trimmed))
        vid = tmp / "vid.mp4"
        subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(clist),
             "-c", "copy", str(vid)],
            check=True, capture_output=True
        )

        # Probe actual duration
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(vid)],
            capture_output=True, text=True
        )
        video_dur = float(r.stdout.strip()) if r.stdout.strip() else sum(e["duration"] for e in edit)

        # Overlay music with fade-out
        fade_st = max(0.0, video_dur - 3.0)
        cmd = [
            ffmpeg, "-y",
            "-i", str(vid),
            "-ss", str(music_ss), "-t", str(video_dur + 1.0), "-i", str(music_path),
            "-filter_complex",
            f"[1:a]afade=t=out:st={fade_st:.2f}:d=3.0,volume=0.85[music]",
            "-map", "0:v", "-map", "[music]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(output)
        ]
        subprocess.run(cmd, check=True, capture_output=True)

    print(f"  → {output.name}  ({video_dur:.1f}s  {len(trimmed)} shots)")


# ── Entry point ───────────────────────────────────────────────────────────────

def assemble(
    work_dir:       Path,
    music_path:     Path,
    output:         Path | None    = None,
    top_percent:    float          = 0.40,
    ffmpeg:         str            = "ffmpeg",
    ffprobe:        str            = "ffprobe",
    nvenc:          bool           = True,
) -> Path:
    import configparser as _cp_mod
    _cp = _cp_mod.ConfigParser()
    _cp.read([str(Path(__file__).parent.parent / "config.ini"), str(work_dir / "config.ini")])
    _resolution  = _cp.get("video",        "resolution",  fallback="3840:2160")
    _framerate   = _cp.get("video",        "framerate",   fallback="60")
    _cam_pattern = _cp.get("music_driven", "cam_pattern", fallback="")
    # Camera order from config: cam_a = 'a', cam_b = 'b' in pattern.
    # Falls back to alphabetical if not configured.
    _cam_a = _cp.get("job", "cam_a", fallback="")
    _cam_b = _cp.get("job", "cam_b", fallback="")
    _cameras_raw = _cp.get("job", "cameras", fallback="")
    _cam_order: list[str] = []
    if _cameras_raw:
        _cam_order = [c.strip() for c in _cameras_raw.split(",") if c.strip()]
    elif _cam_a:
        _cam_order = [c for c in [_cam_a, _cam_b] if c]

    auto_dir    = work_dir / "_autoframe"
    autocut_dir = auto_dir / "autocut"

    # Multicam: use all-cam scores when available
    allcam_csv = auto_dir / "scene_scores_allcam.csv"
    scores_csv = allcam_csv if allcam_csv.exists() else auto_dir / "scene_scores.csv"

    if not scores_csv.exists():
        raise FileNotFoundError("scene_scores.csv not found — run Analyze first")

    # Load CLIP scores
    scene_scores: dict[str, float] = {}
    with open(scores_csv) as f:
        for row in csv.DictReader(f):
            try:
                scene_scores[row["scene"]] = float(row["score"])
            except (KeyError, ValueError):
                pass
    if not scene_scores:
        raise ValueError("scene_scores.csv is empty")

    print(f"\n[music-driven] {len(scene_scores)} clips  music={music_path.name}  "
          f"scores={scores_csv.name}  res={_resolution}@{_framerate}fps")

    # 1. Analyse music
    music_info = analyze_music(music_path)
    beat_times = music_info["beat_times"]
    beat_energy = music_info["beat_energy"]

    # Full highlight always starts from 0 — find_best_offset is for shorts only
    music_ss = 0.0

    # Shift beat_times to start from music_ss
    start_idx = next((i for i, t in enumerate(beat_times) if t >= music_ss), 0)
    beat_times  = [t - music_ss for t in beat_times[start_idx:]]
    beat_energy = beat_energy[start_idx:]

    # 2. Build cut schedule (beats per shot configurable via [music_driven] in config.ini)
    _beats_fast = int(_cp.get("music_driven", "beats_fast", fallback="3"))
    _beats_mid  = int(_cp.get("music_driven", "beats_mid",  fallback="4"))
    _beats_slow = int(_cp.get("music_driven", "beats_slow", fallback="6"))
    schedule = build_schedule(beat_times, beat_energy, _beats_fast, _beats_mid, _beats_slow)
    if not schedule:
        raise RuntimeError("Could not build cut schedule from music")

    # Fill tail: librosa often misses beats in fade-out/outro sections.
    # Extend schedule with 4s slow slots up to the actual music duration.
    avail_dur = music_info["duration"] - music_ss  # how much track we have after skip
    tail_gap = avail_dur - schedule[-1]["end"]
    if tail_gap > 1.5:
        t = schedule[-1]["end"]
        while t < avail_dur - 1.0:
            slot_dur = min(4.0, avail_dur - t)
            if slot_dur < 1.0:
                break
            schedule.append({
                "start": t, "end": t + slot_dur,
                "duration": slot_dur, "energy": 0.2, "n_beats": 4,
            })
            t += slot_dur
        print(f"  Tail fill: +{tail_gap:.1f}s  schedule now {len(schedule)} slots")

    # Build stem → camera mapping for camera-level diversity in match_clips()
    stem_to_camera: dict[str, str] = {}
    cam_sources_csv = auto_dir / "camera_sources.csv"
    if cam_sources_csv.exists():
        with open(cam_sources_csv) as _f:
            for _row in csv.DictReader(_f):
                if "source" in _row and "camera" in _row:
                    stem_to_camera[_row["source"]] = _row["camera"]
    else:
        # Fallback: scan work_dir subdirectories
        _video_ext = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts"}
        for _sub in sorted(work_dir.iterdir()):
            if _sub.is_dir() and not _sub.name.startswith("_"):
                for _vf in _sub.glob("*"):
                    if _vf.suffix.lower() in _video_ext:
                        stem_to_camera[_vf.stem] = _sub.name
    _cams = sorted(set(stem_to_camera.values()))
    if _cams:
        print(f"  Camera map: {len(stem_to_camera)} sources → {_cams}")

    # Build stem → normalised creation_time [0, 1] for chronological arc
    # 0 = first recording of the day, 1 = last recording of the day
    stem_to_time: dict[str, float] = {}
    _video_ext2 = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts"}
    # Read cam_offsets from config (same keys as [cam_offsets] in config.ini)
    _cam_offsets: dict[str, float] = {}
    if _cp.has_section("cam_offsets"):
        for _k, _v in _cp.items("cam_offsets"):
            try:
                _cam_offsets[_k] = float(_v)
            except ValueError:
                pass
    for _vf in sorted(work_dir.rglob("*")):
        if _vf.suffix.lower() not in _video_ext2:
            continue
        if "_autoframe" in _vf.parts:
            continue
        try:
            _r2 = subprocess.run(
                [ffprobe, "-v", "quiet",
                 "-show_entries", "format_tags=creation_time",
                 "-of", "csv=p=0", str(_vf)],
                capture_output=True, text=True, timeout=5,
            )
            _ts = _r2.stdout.strip()
            if not _ts:
                continue
            from datetime import datetime
            _dt = datetime.fromisoformat(_ts.replace("Z", "+00:00"))
            _epoch = _dt.timestamp()
            _cam = stem_to_camera.get(_vf.stem, "")
            _epoch += _cam_offsets.get(_cam, 0.0)
            stem_to_time[_vf.stem] = _epoch
        except Exception:
            pass
    if len(stem_to_time) >= 2:
        _t_min   = min(stem_to_time.values())
        _t_max   = max(stem_to_time.values())
        _t_range = _t_max - _t_min
        if _t_range > 0:
            stem_to_time = {k: (v - _t_min) / _t_range
                            for k, v in stem_to_time.items()}
            print(f"  Chronological arc: {len(stem_to_time)} sources  "
                  f"span={_t_range/3600:.1f}h")
        else:
            stem_to_time = {}
    else:
        stem_to_time = {}

    # 3. Motion analysis — dynamic top_percent so schedule can be filled without reuse
    total_available = len(scene_scores)
    needed = len(schedule)
    dynamic_pct = min(1.0, max(top_percent, (needed / total_available) * 1.2)) if total_available > 0 else top_percent
    if dynamic_pct > top_percent:
        print(f"  Motion pass: expanding to {dynamic_pct*100:.0f}% ({needed} slots / {total_available} clips)")
    clips = analyse_clips(autocut_dir, scene_scores, dynamic_pct, ffprobe,
                          stem_to_camera=stem_to_camera or None,
                          stem_to_time=stem_to_time or None)
    if not clips:
        raise RuntimeError("No clips available for motion analysis")

    if len(clips) < len(schedule):
        print(f"  ⚠ Only {len(clips)} unique clips for {len(schedule)} slots — schedule trimmed (no reuse)")
        schedule = schedule[:len(clips)]

    # 4. Match clips to schedule
    _chron_weight = 0.20 if stem_to_time else 0.0
    edit = match_clips(schedule, clips, chron_weight=_chron_weight,
                       cam_pattern=_cam_pattern, cam_order=_cam_order)
    if not edit:
        raise RuntimeError("Clip matching produced no edit")

    # 5. Render
    if output is None:
        output = auto_dir / "highlight_music_driven.mp4"

    render(edit, music_path, music_ss, output, ffmpeg=ffmpeg, nvenc=nvenc,
           resolution=_resolution, framerate=_framerate)
    return output


def _pick_music_from_dir(music_dir: Path) -> Path | None:
    """Pick a random MP3/M4A from a directory (mirrors make_shorts logic)."""
    exts = {".mp3", ".m4a", ".ogg", ".flac", ".wav"}
    tracks = [p for p in sorted(music_dir.rglob("*")) if p.suffix.lower() in exts]
    if not tracks:
        return None
    import random
    return random.choice(tracks)


if __name__ == "__main__":
    import argparse, configparser, sys as _sys

    ap = argparse.ArgumentParser(description="Music-driven highlight assembler")
    ap.add_argument("work_dir")
    ap.add_argument("--music",       default="", help="Path to music file")
    ap.add_argument("--music-dir",   default="", help="Directory to auto-pick music from")
    ap.add_argument("--output",      default="")
    ap.add_argument("--top-percent", type=float, default=0.40,
                    help="Fraction of top CLIP clips to motion-analyse (default 0.4)")
    args = ap.parse_args()

    # Read ffmpeg/ffprobe from global config.ini
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).parent.parent / "config.ini")
    _ffmpeg  = cfg.get("paths", "ffmpeg",  fallback="ffmpeg")
    _ffprobe = cfg.get("paths", "ffprobe", fallback="ffprobe")

    # Resolve music file
    if args.music:
        _music = Path(args.music)
    elif args.music_dir:
        _music = _pick_music_from_dir(Path(args.music_dir))
        if not _music:
            _sys.exit(f"ERROR: no music files found in {args.music_dir}")
    else:
        # Fall back to global config [music] dir
        _mdir = cfg.get("music", "dir", fallback="")
        _music = _pick_music_from_dir(Path(_mdir)) if _mdir else None
        if not _music:
            _sys.exit("ERROR: no music file — use --music or --music-dir")

    # Detect NVENC
    _nvenc = False
    try:
        r = subprocess.run([_ffmpeg, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=5)
        _nvenc = "h264_nvenc" in r.stdout
    except Exception:
        pass

    out = assemble(
        Path(args.work_dir),
        _music,
        Path(args.output) if args.output else None,
        top_percent=args.top_percent,
        ffmpeg=_ffmpeg,
        ffprobe=_ffprobe,
        nvenc=_nvenc,
    )
    print(f"\nDone → {out}")
