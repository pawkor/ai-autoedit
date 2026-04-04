#!/usr/bin/env python3
"""
Reconstruct PySceneDetect *-Scenes.csv files from existing autocut clips.

Use case: CSVs were accidentally deleted but autocut clips survive.
Durations are read from _autoframe/duration_cache.json (built during DRY_RUN).
Falls back to ffprobe for clips missing from the cache.

Usage:
    python3 rebuild_csvs.py <work_dir>

The script writes to <work_dir>/_autoframe/csv/ and is safe to re-run —
it skips sources whose CSV already exists unless --force is given.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


CSV_HEADER = (
    "Scene Number,Start Frame,Start Timecode,Start Time (seconds),"
    "End Frame,End Timecode,End Time (seconds),"
    "Length (frames),Length (timecode),Length (seconds)"
)
ASSUMED_FPS = 30.0  # only used for frame-number columns; pipeline uses seconds


def sec_to_tc(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description="Rebuild scene CSVs from autocut clips")
    ap.add_argument("work_dir")
    ap.add_argument("--force", action="store_true", help="Overwrite existing CSVs")
    args = ap.parse_args()

    work_dir = Path(args.work_dir).resolve()
    auto_dir = work_dir / "_autoframe"
    autocut_dir = auto_dir / "autocut"
    csv_dir = auto_dir / "csv"
    cache_path = auto_dir / "duration_cache.json"

    if not autocut_dir.exists():
        print("ERROR: no autocut directory found", file=sys.stderr)
        sys.exit(1)

    # Load duration cache
    cache: dict[str, float] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        print(f"Loaded {len(cache)} entries from duration_cache.json")
    else:
        print("Warning: duration_cache.json not found — will ffprobe every clip (slow)")

    csv_dir.mkdir(parents=True, exist_ok=True)

    # Group autocut clips by source stem
    scene_re = re.compile(r"^(.+)-scene-(\d+)\.mp4$")
    groups: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for mp4 in autocut_dir.glob("*.mp4"):
        m = scene_re.match(mp4.name)
        if m:
            groups[m.group(1)].append((int(m.group(2)), mp4))

    if not groups:
        print("ERROR: no autocut clips found", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(groups)} source videos, {sum(len(v) for v in groups.values())} clips total")

    written = skipped = probed = 0
    for source_stem, scenes in sorted(groups.items()):
        out_csv = csv_dir / f"{source_stem}-Scenes.csv"
        if out_csv.exists() and not args.force:
            print(f"  ✓ {source_stem} — CSV exists, skipping (use --force to overwrite)")
            skipped += 1
            continue

        scenes.sort(key=lambda x: x[0])

        rows = []
        t = 0.0
        for scene_num, mp4 in scenes:
            # Prefer cache (no disk I/O)
            key = mp4.name
            dur = cache.get(key)
            if dur is None:
                dur = probe_duration(mp4)
                probed += 1
            if dur <= 0:
                print(f"  ⚠ {mp4.name}: zero duration, skipping scene")
                t += 0  # t unchanged; next scene starts at same position
                continue

            start = t
            end = t + dur
            t = end

            sf = max(1, round(start * ASSUMED_FPS))
            ef = max(sf + 1, round(end * ASSUMED_FPS))
            lf = ef - sf

            rows.append(
                f"{scene_num},{sf},{sec_to_tc(start)},{start:.3f},"
                f"{ef},{sec_to_tc(end)},{end:.3f},"
                f"{lf},{sec_to_tc(dur)},{dur:.3f}"
            )

        if not rows:
            print(f"  ⚠ {source_stem}: no valid scenes, skipping")
            continue

        out_csv.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n")
        print(f"  ✓ {source_stem}: {len(rows)} scenes → {out_csv.name}")
        written += 1

    print(f"\nDone: {written} CSVs written, {skipped} skipped, {probed} clips ffprobed")


if __name__ == "__main__":
    main()
