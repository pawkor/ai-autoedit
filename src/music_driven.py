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

    hop = 512
    # Onset strength as input to beat_track → more accurate beat timestamps
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=hop)
    tempo = float(np.squeeze(tempo))  # librosa ≥0.10 returns 0-dim array
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop).tolist()

    # PLP (Predominant Local Pulse) — rhythmic intensity, not just loudness.
    # Captures verse/chorus/bridge boundaries more accurately than RMS.
    pulse = librosa.beat.plp(onset_envelope=onset_env, sr=sr, hop_length=hop)
    pulse_times = librosa.times_like(pulse, sr=sr, hop_length=hop)

    beat_energy = np.interp(beat_times, pulse_times, pulse)
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
            "camera":      best.get("camera", "unknown"),
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
    # Log selected scenes for exclusion debugging
    scene_list = [e["scene"] for e in edit]
    print(f"  Scenes: {scene_list[:8]}{'…' if len(scene_list) > 8 else ''}")
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
        ["-c:v", "h264_nvenc", "-rc", "constqp", "-qp", "18", "-preset", "p4",
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

        _fps_int = int(framerate) if framerate else 60

        def _trim_one(args):
            i, entry = args
            out = tmp / f"s{i:04d}.mp4"
            # Snap duration to frame boundary to prevent accumulation of rounding drift
            dur = round(entry["duration"] * _fps_int) / _fps_int
            cmd = [
                ffmpeg, "-y",
                "-ss", str(entry["clip_ss"]),
                "-t",  str(dur),
                "-i",  entry["clip_path"],
                *enc_v, *vf_args,
                "-an",
                str(out)
            ]
            r = subprocess.run(cmd, capture_output=True)
            return (i, out) if (r.returncode == 0 and out.exists()) else (i, None)

        # Limit NVENC parallel encodes — GPU encoder has a session cap (typically 3-5)
        trim_workers = 3 if nvenc else _WORKERS
        results = {}
        total_clips = len(edit)
        done_count = 0
        with ThreadPoolExecutor(max_workers=trim_workers) as pool:
            for i, out in pool.map(_trim_one, list(enumerate(edit))):
                results[i] = out
                done_count += 1
                print(f"  [{done_count}/{total_clips}] clip (md)", flush=True)

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
        # Include 'duration' directive so concat demuxer enforces exact segment length.
        # Without it, PTS from encoded files may be off by ±1 frame → drift accumulates.
        clist_lines = []
        for i, p in enumerate(trimmed):
            snapped = round(edit[i]["duration"] * _fps_int) / _fps_int
            clist_lines.append(f"file '{p}'\nduration {snapped:.6f}")
        clist.write_text("\n".join(clist_lines))
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

        # Overlay music with fade-out, mixed with original clip audio
        fade_st = max(0.0, video_dur - 3.0)
        cmd = [
            ffmpeg, "-y",
            "-i", str(vid),
            "-ss", str(music_ss), "-t", str(video_dur + 1.0), "-i", str(music_path),
            "-filter_complex",
            f"[1:a]afade=t=out:st={fade_st:.2f}:d=3.0[aout]",
            "-map", "0:v", "-map", "[aout]",
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
    dry_run:        bool           = False,
) -> Path:
    import configparser as _cp_mod
    _cp = _cp_mod.ConfigParser()
    _cp.read([
        str(Path(__file__).parent.parent / "config.ini"),
        str(work_dir / "config.ini"),
        str(Path(__file__).parent.parent / "webapp" / "config.ini"),
    ])
    # Auto-detect resolution from first clip in autocut/ — avoids unnecessary upscaling
    # when source footage is 1080p. Config override still works when set explicitly.
    _resolution_cfg = _cp.get("video", "resolution", fallback="")
    if _resolution_cfg:
        _resolution = _resolution_cfg
    else:
        _autocut_dir_probe = work_dir / "_autoframe" / "autocut"
        _resolution = "1920:1080"  # safe default
        _probe_clip = next(iter(sorted(_autocut_dir_probe.glob("*.mp4"))), None) if _autocut_dir_probe.exists() else None
        if _probe_clip:
            try:
                import subprocess as _sp2
                _pr = _sp2.run([ffprobe, "-v", "quiet", "-show_entries", "stream=width,height",
                                "-of", "csv=p=0", str(_probe_clip)],
                               capture_output=True, text=True, timeout=5)
                _dims = [l for l in _pr.stdout.strip().splitlines() if l.strip()]
                if _dims:
                    _w, _h = _dims[0].split(",")[:2]
                    _resolution = f"{_w.strip()}:{_h.strip()}"
            except Exception:
                pass
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

    # Load manual overrides from gallery UI
    # States: "include" = force-include, "ban" = hard-exclude (never, not even fallback),
    #         "exclude" treated as "ban" for backwards compatibility
    _overrides_path = auto_dir / "manual_overrides.json"
    _manual_banned: set[str] = set()   # never use, hard exclude
    _manual_included: set[str] = set() # force include regardless of threshold
    if _overrides_path.exists():
        try:
            import json as _json
            _ov = _json.loads(_overrides_path.read_text())
            _manual_banned   = {k for k, v in _ov.items() if v in ("ban", "exclude")}
            _manual_included = {k for k, v in _ov.items() if v == "include"}
            if _manual_banned:
                print(f"  Banned: {len(_manual_banned)} scene(s) — hard excluded from pool")
        except Exception:
            pass

    # Precompute source→camera and source→absolute epoch (used for exclusion propagation
    # and back-cam filtering below). Done once here, reused in stem_to_time later.
    _interval_s  = float(_cp.get("clip_scan", "interval_sec", fallback="3.0"))
    _clip_dur_s  = float(_cp.get("clip_scan", "clip_dur_sec", fallback="10.0"))
    _src_cam_off: dict[str, float] = {}
    if _cp.has_section("cam_offsets"):
        for _ko, _vo in _cp.items("cam_offsets"):
            try: _src_cam_off[_ko] = float(_vo)
            except ValueError: pass
    _src_cam_map: dict[str, str] = {}
    _cam_src_path = auto_dir / "camera_sources.csv"
    if _cam_src_path.exists():
        with open(_cam_src_path) as _csf:
            for _csr in csv.DictReader(_csf):
                if "source" in _csr and "camera" in _csr:
                    _src_cam_map[_csr["source"]] = _csr["camera"]
    _src_epoch: dict[str, float] = {}
    _vext3 = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts"}
    for _svf in sorted(work_dir.rglob("*")):
        if _svf.suffix.lower() not in _vext3: continue
        if "_autoframe" in _svf.parts: continue
        try:
            _r3 = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries", "format_tags=creation_time",
                 "-of", "csv=p=0", str(_svf)],
                capture_output=True, text=True, timeout=5)
            _ts3 = _r3.stdout.strip()
            if not _ts3: continue
            from datetime import datetime as _dtcls
            _ep3 = _dtcls.fromisoformat(_ts3.replace("Z", "+00:00")).timestamp()
            _cam3 = _src_cam_map.get(_svf.stem, "")
            _src_epoch[_svf.stem] = _ep3 + _src_cam_off.get(_cam3, 0.0)
        except Exception:
            pass

    # Load per-clip offsets from CSV (offset_sec = actual start within source file).
    # Produced by clip_scan.py; absent in older CSVs → fallback to clip_N * interval (buggy).
    _clip_offset: dict[str, float] = {}
    with open(scores_csv) as _foff:
        for _roff in csv.DictReader(_foff):
            _os = _roff.get("offset_sec", "")
            if _os:
                try: _clip_offset[_roff["scene"]] = float(_os)
                except ValueError: pass

    import re as _re3
    def _clip_range(scene_key: str):
        src = _clip_source(scene_key)
        epoch = _src_epoch.get(src)
        if epoch is None: return None
        if scene_key in _clip_offset:
            t0 = epoch + _clip_offset[scene_key]
        else:
            # Fallback for CSVs without offset_sec (pre-fix analyze)
            m = _re3.search(r'-clip-(\d+)$', scene_key)
            if not m: return None
            t0 = epoch + int(m.group(1)) * _interval_s
        return (t0, t0 + _clip_dur_s)

    # Propagate bans to other cameras: banned cam-A clip → ban all cam-B clips
    # overlapping the same absolute time window.
    if _manual_banned and _src_epoch:
        _ban_ranges = [r for s in _manual_banned if (r := _clip_range(s))]
        if _ban_ranges:
            _sync_banned: set[str] = set()
            with open(scores_csv) as _f4:
                for _r4 in csv.DictReader(_f4):
                    _sc4 = _r4.get("scene", "")
                    if _sc4 in _manual_banned: continue
                    _rng4 = _clip_range(_sc4)
                    if not _rng4: continue
                    for (ban_s, ban_e) in _ban_ranges:
                        if _rng4[0] < ban_e and _rng4[1] > ban_s:
                            _sync_banned.add(_sc4)
                            break
            if _sync_banned:
                print(f"  Sync-banned: {len(_sync_banned)} scene(s) from other cameras "
                      f"in banned time windows")
                _manual_banned.update(_sync_banned)

    # Load CLIP scores; hard-exclude banned scenes and negative-dominant scenes
    _all_scores: dict[str, float] = {}
    _neg_excluded = 0
    with open(scores_csv) as f:
        for row in csv.DictReader(f):
            try:
                scene = row["scene"]
                if scene in _manual_banned:
                    continue
                final = float(row["score"])
                if final < 0:
                    _neg_excluded += 1
                    continue
                _all_scores[scene] = final
            except (KeyError, ValueError):
                pass
    if _neg_excluded:
        print(f"  Neg-score excluded: {_neg_excluded} scene(s)")
    if not _all_scores:
        raise ValueError("scene_scores.csv is empty")

    # Apply gallery threshold: preferred pool = selected scenes, fallback = rest sorted desc.
    # Manual includes bypass threshold. Fallback used only when preferred pool too small.
    _threshold = float(_cp.get("scene_selection", "threshold", fallback="0"))
    _preferred = {k: v for k, v in _all_scores.items() if v >= _threshold or k in _manual_included}
    _fallback  = sorted(
        ((k, v) for k, v in _all_scores.items() if k not in _preferred),
        key=lambda x: x[1], reverse=True
    )

    # Back-cam scenes are allowed freely — sync-ban already propagates bans from
    # main-cam to back-cam for the same time window. Camera pattern in match_clips()
    # handles switching between cameras to build dynamics.

    print(f"\n[music-driven] {len(_all_scores)} clips (threshold≥{_threshold:.3f}: {len(_preferred)})  "
          f"music={music_path.name}  scores={scores_csv.name}  res={_resolution}@{_framerate}fps")

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
    # BPM-adaptive: ensure minimum clip durations regardless of tempo.
    # At high BPM (e.g. 117) default 3 beats = 1.5s → slideshow.
    # Targets: fast≥2.5s, mid≥4.0s, slow≥6.0s.
    import math as _math
    _bpm = music_info.get("tempo", 120.0)
    _beat_sec = 60.0 / _bpm
    _min_fast = float(_cp.get("music_driven", "min_clip_fast_sec", fallback="2.5"))
    _min_mid  = float(_cp.get("music_driven", "min_clip_mid_sec",  fallback="4.0"))
    _min_slow = float(_cp.get("music_driven", "min_clip_slow_sec", fallback="6.0"))
    # Round up to nearest multiple of 4 (bar in 4/4) so cuts land on downbeats.
    def _bar_ceil(n: int, bar: int = 4) -> int:
        return _math.ceil(n / bar) * bar
    _beats_fast = _bar_ceil(max(_beats_fast, _math.ceil(_min_fast / _beat_sec)))
    _beats_mid  = _bar_ceil(max(_beats_mid,  _math.ceil(_min_mid  / _beat_sec)))
    _beats_slow = _bar_ceil(max(_beats_slow, _math.ceil(_min_slow / _beat_sec)))
    print(f"  BPM={_bpm:.0f}  beat={_beat_sec:.2f}s  "
          f"beats: fast={_beats_fast}({_beats_fast*_beat_sec:.1f}s) "
          f"mid={_beats_mid}({_beats_mid*_beat_sec:.1f}s) "
          f"slow={_beats_slow}({_beats_slow*_beat_sec:.1f}s)")
    schedule = build_schedule(beat_times, beat_energy, _beats_fast, _beats_mid, _beats_slow)
    if not schedule:
        raise RuntimeError("Could not build cut schedule from music")

    # Align music playback to the first detected beat.
    # beat_times[0] is rarely exactly 0 — there's a short pre-beat silence.
    # Without this offset every cut lands beat_times[0] seconds early.
    music_ss = schedule[0]["start"]

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

    # 3. Build final clip pool: preferred (above threshold + manual includes) first,
    # fallback to auto scenes below threshold to fill music duration.
    # Banned scenes never appear in either pool.
    needed = len(schedule)
    scene_scores = dict(_preferred)
    if len(scene_scores) < needed:
        _take = needed - len(scene_scores)
        _added = dict(_fallback[:_take])
        scene_scores.update(_added)
        print(f"  Pool: {len(_preferred)} selected + {len(_added)} fallback "
              f"(needed {needed} slots)")
    else:
        print(f"  Pool: {len(scene_scores)} selected clips  ({len(_fallback)} below threshold)")

    # Camera-pattern balance: if cam_pattern active, ensure each camera has enough
    # clips in the pool to cover its pattern share. Add fallback for deficient cameras.
    _resolved_pat_early = _parse_cam_pattern(_cam_pattern, _cam_order) if _cam_pattern and _cam_order else None
    if _resolved_pat_early and stem_to_camera:
        _cam_needed: dict[str, int] = {}
        for _pi in range(needed):
            _pc = _resolved_pat_early[_pi % len(_resolved_pat_early)]
            _cam_needed[_pc] = _cam_needed.get(_pc, 0) + 1
        _cam_have: dict[str, int] = {}
        for _sc in scene_scores:
            _pc2 = stem_to_camera.get(_clip_source(_sc), "unknown")
            _cam_have[_pc2] = _cam_have.get(_pc2, 0) + 1
        _pat_added = 0
        for _pc, _cnt in _cam_needed.items():
            _deficit = _cnt - _cam_have.get(_pc, 0)
            if _deficit > 0:
                _fb_cam = [(k, v) for k, v in _fallback if stem_to_camera.get(_clip_source(k), "") == _pc and k not in scene_scores]
                _fb_add = dict(_fb_cam[:_deficit])
                scene_scores.update(_fb_add)
                _pat_added += len(_fb_add)
        if _pat_added:
            print(f"  Pattern balance: +{_pat_added} fallback clips to cover camera pattern")

    # Motion analysis — skip entirely for dry-run, use duration_cache.json instead
    if dry_run:
        import json as _jd
        _dur_cache: dict[str, float] = {}
        _dur_cache_path = auto_dir / "duration_cache.json"
        if _dur_cache_path.exists():
            try:
                _raw = _jd.loads(_dur_cache_path.read_text())
                _dur_cache = {k.removesuffix(".mp4"): float(v) for k, v in _raw.items()}
            except Exception:
                pass
        clips = []
        for _scene, _score in sorted(scene_scores.items(), key=lambda x: x[1], reverse=True):
            _clip_path = autocut_dir / f"{_scene}.mp4"
            if not _clip_path.exists():
                continue
            _dur = _dur_cache.get(_scene, 0.0)
            if _dur < 0.5:
                continue
            _src = _clip_source(_scene)
            clips.append({
                "scene":          _scene,
                "score":          _score,
                "path":           _clip_path,
                "duration":       _dur,
                "motion_peak":    _dur * 0.3,
                "motion_level":   0.0,
                "motion_norm":    0.0,
                "camera":         (stem_to_camera or {}).get(_src, "unknown"),
                "clip_time_norm": (stem_to_time or {}).get(_src),
            })
        print(f"  Dry-run: {len(clips)} clips from duration cache (motion skipped)")
    else:
        clips = analyse_clips(autocut_dir, scene_scores, 1.0, ffprobe,
                              stem_to_camera=stem_to_camera or None,
                              stem_to_time=stem_to_time or None)
    if not clips:
        raise RuntimeError("No clips available for motion analysis")

    # Filter out static clips: configurable via [music_driven] min_motion_score (0.0 = off)
    _min_motion = float(_cp.get("music_driven", "min_motion_score", fallback="0.1"))
    if not dry_run and _min_motion > 0 and len(clips) > len(schedule):
        _before = len(clips)
        _filtered = [c for c in clips if c.get("motion_norm", 0) >= _min_motion]
        # Only apply filter if enough clips remain to fill schedule
        if len(_filtered) >= len(schedule):
            clips = _filtered
            print(f"  Motion filter: removed {_before - len(clips)} static clips "
                  f"(motion_norm < {_min_motion}, {len(clips)} remain)")
        else:
            print(f"  Motion filter: skipped (would leave only {len(_filtered)} for "
                  f"{len(schedule)} slots — pool too small)")

    if len(clips) < len(schedule):
        print(f"  ⚠ Only {len(clips)} unique clips for {len(schedule)} slots — schedule trimmed (no reuse)")
        schedule = schedule[:len(clips)]

    # 4. Match clips to schedule
    _chron_weight = 0.20 if stem_to_time else 0.0
    edit = match_clips(schedule, clips, chron_weight=_chron_weight,
                       cam_pattern=_cam_pattern, cam_order=_cam_order)
    if not edit:
        raise RuntimeError("Clip matching produced no edit")

    # 5a. Dry-run: write sequence JSON and exit without encoding
    if dry_run:
        import json as _json
        seq = []
        for e in edit:
            scene = e["scene"]
            frame_path = None
            for suffix in ("_f0.jpg", "_f1.jpg", ".jpg"):
                fp = auto_dir / "frames" / (scene + suffix)
                if fp.exists():
                    frame_path = str(fp)
                    break
            seq.append({
                "scene":      scene,
                "duration":   round(e["duration"], 2),
                "energy":     round(e["energy"], 3),
                "clip_score": e.get("clip_score", 0),
                "clip_ss":    round(e.get("clip_ss", 0), 3),
                "clip_path":  e.get("clip_path", ""),
                "music_start": round(e["music_start"], 2),
                "frame_path": frame_path,
            })
        out_json = auto_dir / "preview_sequence.json"
        out_json.write_text(_json.dumps({"sequence": seq, "music": str(music_path)}, indent=2))
        print(f"Dry-run complete → {len(seq)} slots → {out_json}")
        return out_json

    # 5b. Render
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
    ap.add_argument("--dry-run", action="store_true",
                    help="Run scene selection only, write preview_sequence.json, skip encoding")
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
        dry_run=args.dry_run,
    )
    print(f"\nDone → {out}")
