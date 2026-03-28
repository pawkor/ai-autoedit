#!/usr/bin/env python3
"""
gpu_detect.py — GPU-accelerated scene detection and splitting via decord + PyTorch.

Replaces scenedetect for the detect + split steps. Outputs a scenedetect-compatible
CSV (for caching compatibility) and scene clips to autocut/.

Usage:
    python3 gpu_detect.py <video> <csv_dir> <autocut_dir> [options]

Options:
    --threshold N       Mean pixel difference threshold 0-255 (default: 30)
    --min-scene-len N   Minimum scene length in seconds (default: 8)
"""

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

import torch

try:
    import decord
    from decord import VideoReader
    HAVE_DECORD = True
except ImportError:
    HAVE_DECORD = False


def fmt_tc(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def detect_scenes(video_path: str, threshold: float, min_scene_len_sec: float) -> tuple:
    """
    Returns (scenes, fps) where scenes is a list of
    (scene_num, start_frame, end_frame, start_sec, end_sec).
    Frames are decoded on GPU if available, CPU otherwise.
    Comparison uses 64×64 thumbnails for speed.
    """
    if torch.cuda.is_available():
        ctx = decord.gpu(0)
        print(f"  decord: GPU ({torch.cuda.get_device_name(0)})", flush=True)
    else:
        ctx = decord.cpu(0)
        print("  decord: CPU (no CUDA)", flush=True)

    try:
        vr = VideoReader(video_path, ctx=ctx, num_threads=1)
    except Exception:
        ctx = decord.cpu(0)
        print("  decord: GPU context failed, falling back to CPU", flush=True)
        vr = VideoReader(video_path, ctx=ctx, num_threads=1)
    fps = vr.get_avg_fps()
    total_frames = len(vr)
    min_frames = max(1, int(min_scene_len_sec * fps))

    CHUNK = 128   # frames per batch
    THUMB = 64    # resize to 64×64 for diff computation

    scene_starts = [0]
    last_cut = 0
    prev = None

    from tqdm import tqdm
    chunks = range(0, total_frames, CHUNK)
    for chunk_start in tqdm(chunks, desc="  detect", unit="chunk", leave=False):
        chunk_end = min(chunk_start + CHUNK, total_frames)
        indices = list(range(chunk_start, chunk_end))

        # shape: (N, H, W, 3), uint8
        batch = vr.get_batch(indices)

        if not isinstance(batch, torch.Tensor):
            batch = torch.from_numpy(batch.asnumpy())

        batch = batch.to(torch.float32) / 255.0  # (N, H, W, 3)

        # Downsample to THUMB×THUMB
        h, w = batch.shape[1], batch.shape[2]
        sh = max(1, h // THUMB)
        sw = max(1, w // THUMB)
        small = batch[:, ::sh, ::sw, :]  # (N, ~64, ~64, 3)

        for i, frame_idx in enumerate(indices):
            curr = small[i]
            if prev is None:
                prev = curr
                continue

            diff = (curr - prev).abs().mean().item() * 255.0

            if diff > threshold and (frame_idx - last_cut) >= min_frames:
                scene_starts.append(frame_idx)
                last_cut = frame_idx

            prev = curr

    # Build scene list
    scenes = []
    for i, start in enumerate(scene_starts):
        end = (scene_starts[i + 1] - 1) if i + 1 < len(scene_starts) else (total_frames - 1)
        scenes.append((
            i + 1,
            start,
            end,
            start / fps,
            (end + 1) / fps,
        ))

    return scenes, fps


def write_csv(scenes, output_csv: str):
    with open(output_csv, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Scene List', '', '', '', '', '', '', '', '', ''])
        w.writerow([
            'Scene Number', 'Start Frame', 'Start Timecode', 'Start Time (seconds)',
            'End Frame', 'End Timecode', 'End Time (seconds)',
            'Length (frames)', 'Length (timecode)', 'Length (seconds)',
        ])
        for scene_num, start_f, end_f, start_sec, end_sec in scenes:
            length_f = end_f - start_f + 1
            length_sec = end_sec - start_sec
            w.writerow([
                scene_num,
                start_f + 1,
                fmt_tc(start_sec),
                f"{start_sec:.3f}",
                end_f + 1,
                fmt_tc(end_sec),
                f"{end_sec:.3f}",
                length_f,
                fmt_tc(length_sec),
                f"{length_sec:.3f}",
            ])


def split_video(video_path: str, scenes, autocut_dir: str, base_name: str):
    for scene_num, start_f, end_f, start_sec, end_sec in scenes:
        out = os.path.join(autocut_dir, f"{base_name}-scene-{scene_num:03d}.mp4")
        if os.path.exists(out):
            continue
        duration = end_sec - start_sec
        subprocess.run([
            "ffmpeg",
            "-ss", f"{start_sec:.3f}",
            "-i", video_path,
            "-t", f"{duration:.3f}",
            "-c", "copy",
            out, "-y", "-loglevel", "quiet",
        ])


def main():
    if not HAVE_DECORD:
        print("ERROR: decord not installed. Run: pip install decord", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("csv_dir")
    parser.add_argument("autocut_dir")
    parser.add_argument("--threshold",     type=float, default=30.0)
    parser.add_argument("--min-scene-len", type=float, default=8.0)
    args = parser.parse_args()

    video_path = args.video
    base_name  = Path(video_path).stem
    output_csv = os.path.join(args.csv_dir, f"{base_name}-Scenes.csv")

    os.makedirs(args.autocut_dir, exist_ok=True)

    # Cache check
    if os.path.exists(output_csv):
        with open(output_csv) as f:
            expected = sum(1 for _ in f) - 2
        existing = len([
            x for x in os.listdir(args.autocut_dir)
            if x.startswith(f"{base_name}-scene-") and x.endswith(".mp4")
        ])
        if existing >= expected > 0:
            print(f"  ✓ {base_name} ({existing} scenes, cached)")
            return

    device = "GPU" if torch.cuda.is_available() else "CPU"
    print(f"  → {base_name} (detect {device})", flush=True)

    scenes, fps = detect_scenes(video_path, args.threshold, args.min_scene_len)
    print(f"    {len(scenes)} scenes  (fps={fps:.2f})", flush=True)

    write_csv(scenes, output_csv)
    split_video(video_path, scenes, args.autocut_dir, base_name)


if __name__ == "__main__":
    main()
