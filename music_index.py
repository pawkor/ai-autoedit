#!/usr/bin/env python3
# music_index.py — analyze BPM and energy for MP3 library
# Usage: python3 /path/to/repo/music_index.py /path/to/music [--output index.json]
#
# Output JSON: list of {file, title, genre, bpm, energy, duration}
# Genre extracted from filename pattern: "｜ Genre ｜" (NCS convention)
# energy: RMS loudness normalized 0-1 (higher = more intense)

import os
import re
import sys
import json
import argparse
from pathlib import Path
from multiprocessing import Pool

import subprocess
import numpy as np
import librosa
from tqdm import tqdm


def extract_genre(filename: str) -> str:
    m = re.search(r'｜\s*([^｜]+?)\s*｜', filename)
    if m:
        return m.group(1).strip().lower()
    return "unknown"


def load_audio_ffmpeg(path: str, sr: int = 22050) -> np.ndarray:
    """Decode audio via ffmpeg to raw PCM, avoids soundfile/audioread MP3 issues."""
    cmd = [
        "ffmpeg", "-i", path,
        "-f", "f32le", "-ac", "1", "-ar", str(sr),
        "-loglevel", "quiet", "pipe:1"
    ]
    raw = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def analyze_track(mp3_path: str) -> dict | None:
    try:
        sr = 22050
        y = load_audio_ffmpeg(mp3_path, sr)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo) if np.isscalar(tempo) else float(tempo[0])

        rms = librosa.feature.rms(y=y)[0]
        energy = float(np.mean(rms))

        duration = len(y) / sr
        genre = extract_genre(Path(mp3_path).name)

        return {
            "file":     mp3_path,
            "title":    Path(mp3_path).stem,
            "genre":    genre,
            "bpm":      round(bpm, 1),
            "energy":   round(energy, 6),
            "duration": round(duration, 1),
        }
    except Exception as e:
        print(f"  Error {Path(mp3_path).name}: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("music_dir", help="Directory with MP3 files")
    parser.add_argument("--output", default=None, help="Output JSON path (default: music_dir/index.json)")
    parser.add_argument("--force", action="store_true", help="Re-analyze already indexed tracks")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 4),
                        help="Parallel worker processes (default: min(cpu_count, 4))")
    args = parser.parse_args()

    music_dir = Path(args.music_dir)
    output_path = Path(args.output) if args.output else music_dir / "index.json"

    mp3_files = sorted(f for ext in ("*.mp3", "*.m4a") for f in music_dir.glob(ext))
    if not mp3_files:
        print(f"No MP3 files found in {music_dir}")
        sys.exit(1)

    # Load existing index
    existing = {}
    if output_path.exists() and not args.force:
        with open(output_path) as f:
            for entry in json.load(f):
                existing[entry["file"]] = entry

    results = [e for e in existing.values() if Path(e["file"]).exists()]
    removed = len(existing) - len(results)
    if removed:
        print(f"  Removed {removed} deleted tracks from index")

    new_files = [str(mp3) for mp3 in mp3_files if str(mp3) not in existing]
    new_count = 0

    def save(data):
        data.sort(key=lambda x: x["bpm"])
        energies = [r["energy"] for r in data]
        if energies:
            e_min, e_max = min(energies), max(energies)
            for r in data:
                r["energy_norm"] = round((r["energy"] - e_min) / (e_max - e_min + 1e-9), 4)
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    if new_files:
        print(f"  Analyzing {len(new_files)} new tracks ({args.workers} workers)...")
        with Pool(args.workers) as pool:
            for entry in tqdm(pool.imap_unordered(analyze_track, new_files), total=len(new_files)):
                if entry:
                    results.append(entry)
                    new_count += 1
                if new_count % 10 == 0:
                    save(results)

    if new_count > 0:
        save(results)

    print(f"\nIndexed: {len(results)} tracks ({new_count} new)")
    if results:
        print(f"BPM range: {min(r['bpm'] for r in results):.0f} – {max(r['bpm'] for r in results):.0f}")
    print(f"Saved: {output_path}")


if __name__ == "__main__":
    main()
