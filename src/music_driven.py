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

def build_schedule(beat_times: list[float], beat_energy: list[float]) -> list[dict]:
    """
    Group consecutive beats into shot slots.
    High energy  (>0.65) → 2 beats/shot  (~fast cuts, chorus)
    Medium energy         → 3 beats/shot
    Low energy   (<0.35) → 4 beats/shot  (~scenic, verse)
    """
    schedule: list[dict] = []
    n = len(beat_times)
    i = 0
    while i < n - 1:
        energy = beat_energy[i]
        n_beats = 2 if energy > 0.65 else (4 if energy < 0.35 else 3)
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

    fast = sum(1 for s in schedule if s["n_beats"] <= 2)
    slow = sum(1 for s in schedule if s["n_beats"] >= 4)
    print(f"  Schedule: {len(schedule)} slots  fast={fast}  slow={slow}  "
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
                  top_percent: float, ffprobe: str) -> list[dict]:
    """
    Compute motion profiles for the top_percent% of CLIP-scored clips.
    Returns list sorted by CLIP score descending.
    """
    sorted_scenes = sorted(scene_scores.items(), key=lambda x: x[1], reverse=True)
    cutoff     = max(1, int(len(sorted_scenes) * top_percent))
    candidates = sorted_scenes[:cutoff]
    print(f"  Motion pass: top {top_percent*100:.0f}% → {len(candidates)}/{len(sorted_scenes)} clips")

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
        return {
            "scene":        scene,
            "score":        score,
            "path":         clip_path,
            "duration":     clip_dur,
            "motion_peak":  peak_t,
            "motion_level": motion_lvl,
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


def match_clips(schedule: list[dict], clips: list[dict]) -> list[dict]:
    """
    Assign best clip to each slot.
    Scoring per candidate:  CLIP_score * 0.6  +  energy_match * 0.4
    Align motion peak to beat hit (anchor_offset = 30% into slot).
    Source diversity: avoid repeating the same source file within a rolling window.
    """
    import collections
    used: set[str] = set()
    edit: list[dict] = []

    num_sources = len({_clip_source(c["scene"]) for c in clips})
    # Rolling window: how many consecutive slots before a source can repeat.
    # At least 2 sources worth of slots, or minimum 4.
    _window = max(4, num_sources * 2)
    recent_sources: collections.deque = collections.deque(maxlen=_window)

    for slot in schedule:
        dur    = slot["duration"]
        energy = slot["energy"]

        def _pool(relax_dur: bool, allow_reuse: bool) -> list[dict]:
            min_dur = dur if relax_dur else dur + 0.2
            return [
                c for c in clips
                if c["duration"] >= min_dur
                and (allow_reuse or c["scene"] not in used)
                and _clip_source(c["scene"]) not in recent_sources
            ]

        pool = _pool(False, False)
        if not pool:
            pool = _pool(True, False)          # relax duration
        if not pool:
            # Relax source diversity constraint but still avoid scene reuse
            pool = [c for c in clips if c["duration"] >= dur and c["scene"] not in used]
        if not pool:
            pool = [c for c in clips if c["duration"] >= dur]  # last resort reuse
        if not pool:
            continue

        def rank(c: dict) -> float:
            motion_match = 1.0 - abs(energy - c.get("motion_norm", 0.5))
            return c["score"] * 0.6 + motion_match * 0.4

        best   = max(pool, key=rank)
        used.add(best["scene"])
        recent_sources.append(_clip_source(best["scene"]))

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
    print(f"  Matched: {len(edit)} slots  {covered:.1f}s  unique={unique}")
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
    _resolution = _cp.get("video", "resolution", fallback="3840:2160")
    _framerate  = _cp.get("video", "framerate",  fallback="60")

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

    # Start from the most energetically dense section
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from make_shorts import find_best_offset
        music_ss = find_best_offset(music_path, music_info["duration"] * 0.8)
    except Exception:
        music_ss = 0.0

    # Shift beat_times to start from music_ss
    start_idx = next((i for i, t in enumerate(beat_times) if t >= music_ss), 0)
    beat_times  = [t - music_ss for t in beat_times[start_idx:]]
    beat_energy = beat_energy[start_idx:]

    # 2. Build cut schedule
    schedule = build_schedule(beat_times, beat_energy)
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

    # 3. Motion analysis on top CLIP clips
    clips = analyse_clips(autocut_dir, scene_scores, top_percent, ffprobe)
    if not clips:
        raise RuntimeError("No clips available for motion analysis")

    # Cap schedule to available clip count so each scene appears at most once
    if len(schedule) > len(clips):
        print(f"  Trimming schedule {len(schedule)} → {len(clips)} slots (clip count cap)")
        schedule = schedule[:len(clips)]

    # 4. Match clips to schedule
    edit = match_clips(schedule, clips)
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
