#!/usr/bin/env python3
"""
proxy_reframe.py — replace LRV-based trimmed clips with VID_ high-res versions.

After select_scenes.py selects scenes from low-res LRV proxies, this script
finds the corresponding VID_ source files, extracts the exact same time ranges,
and reframes them from the high-res dual fisheye source.

Activated only when [reframe] vid_input_format is set in config.ini.

LRV filename mapping:  LRV_TIMESTAMP_11_NNN → VID_TIMESTAMP_10_NNN

Usage:
    python3 /path/to/repo/proxy_reframe.py
        <selected_scenes.txt> <csv_dir> <cam_b_dir> <output_dir>
        --yaw YAW --pitch PITCH --roll ROLL --h-fov HFOV --v-fov VFOV
        --input-format FORMAT --ih-fov IH_FOV
        --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe
        --codec CODEC --quality "QUALITY_FLAGS"
"""

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path


def get_duration(ffprobe, path):
    r = subprocess.run(
        [ffprobe, '-v', 'quiet', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', path],
        capture_output=True, text=True
    )
    try:
        return float(r.stdout.strip())
    except Exception:
        return None


def get_scene_times(csv_path, scene_num):
    """Return (start_sec, end_sec) for scene_num from scenedetect-format CSV."""
    try:
        with open(csv_path) as f:
            reader = csv.reader(f)
            next(reader)  # header row 1 ("Scene List")
            next(reader)  # header row 2 (column names)
            for row in reader:
                if not row:
                    continue
                try:
                    if int(row[0]) == scene_num:
                        return float(row[3]), float(row[6])
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        print(f"  ! CSV error {csv_path}: {e}", file=sys.stderr)
    return None, None


def lrv_to_vid_path(lrv_stem, cam_b_dir):
    """Map LRV_TIMESTAMP_11_NNN stem to VID_TIMESTAMP_10_NNN.insv path."""
    vid_stem = lrv_stem.replace('LRV_', 'VID_', 1).replace('_11_', '_10_', 1)
    vid_path = Path(cam_b_dir) / f"{vid_stem}.insv"
    if vid_path.exists():
        return str(vid_path)
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('selected_scenes')
    p.add_argument('csv_dir')
    p.add_argument('cam_b_dir')
    p.add_argument('output_dir')
    p.add_argument('--yaw',          type=float, default=0)
    p.add_argument('--pitch',        type=float, default=0)
    p.add_argument('--roll',         type=float, default=0)
    p.add_argument('--h-fov',        type=float, default=100)
    p.add_argument('--v-fov',        type=float, default=75)
    p.add_argument('--input-format', default='dfisheye')
    p.add_argument('--ih-fov',       type=float, default=190)
    p.add_argument('--ffmpeg',       default='ffmpeg')
    p.add_argument('--ffprobe',      default='ffprobe')
    p.add_argument('--codec',        default='libx264')
    p.add_argument('--quality',      default='-crf 18 -preset fast')
    args = p.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    lines = Path(args.selected_scenes).read_text().splitlines()
    new_lines = []
    replaced = 0
    skipped = 0

    for line in lines:
        if "file '" not in line or '/trimmed/LRV_' not in line:
            new_lines.append(line)
            continue

        clip_path = line.strip()[6:-1]  # strip: file '...'
        clip_name = Path(clip_path).stem

        parts = clip_name.rsplit('-scene-', 1)
        if len(parts) != 2:
            new_lines.append(line)
            continue

        lrv_stem, scene_num_str = parts[0], parts[1]
        try:
            scene_num = int(scene_num_str)
        except ValueError:
            new_lines.append(line)
            continue

        vid_path = lrv_to_vid_path(lrv_stem, args.cam_b_dir)
        if not vid_path:
            print(f"  ! VID_ not found for {lrv_stem}, keeping LRV proxy", file=sys.stderr)
            skipped += 1
            new_lines.append(line)
            continue

        out_path = Path(args.output_dir) / f"{clip_name}.mp4"

        if out_path.exists():
            print(f"  ✓ {clip_name} (cached)")
            new_lines.append(f"file '{out_path}'")
            replaced += 1
            continue

        trim_dur = get_duration(args.ffprobe, clip_path)
        if not trim_dur:
            new_lines.append(line)
            continue

        csv_path = Path(args.csv_dir) / f"{lrv_stem}-Scenes.csv"
        if not csv_path.exists():
            print(f"  ! CSV not found: {csv_path.name}, keeping LRV proxy", file=sys.stderr)
            skipped += 1
            new_lines.append(line)
            continue

        scene_start, scene_end = get_scene_times(str(csv_path), scene_num)
        if scene_start is None:
            new_lines.append(line)
            continue

        scene_dur = scene_end - scene_start
        trim_offset = max(0.0, (scene_dur - trim_dur) / 2)
        vid_start = scene_start + trim_offset

        vf = (
            f"v360={args.input_format}:rectilinear"
            f":ih_fov={args.ih_fov}:iv_fov={args.ih_fov}"
            f":yaw={args.yaw}:pitch={args.pitch}:roll={args.roll}"
            f":h_fov={args.h_fov}:v_fov={args.v_fov}"
        )

        print(f"  → {clip_name}  {trim_dur:.1f}s @ {vid_start:.1f}s")

        quality_flags = args.quality.split()
        cmd = [
            args.ffmpeg,
            '-ss', f'{vid_start:.3f}',
            '-t',  f'{trim_dur:.3f}',
            '-i',  vid_path,
            '-vf', vf,
            '-c:v', args.codec, *quality_flags,
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k',
            str(out_path), '-y', '-loglevel', 'error',
        ]

        result = subprocess.run(cmd)
        if result.returncode == 0:
            new_lines.append(f"file '{out_path}'")
            replaced += 1
        else:
            print(f"  ! Reframe failed for {clip_name}, keeping LRV proxy", file=sys.stderr)
            skipped += 1
            new_lines.append(line)

    Path(args.selected_scenes).write_text('\n'.join(new_lines) + '\n')
    print(f"  Proxy: {replaced} replaced, {skipped} kept LRV")


if __name__ == '__main__':
    main()
