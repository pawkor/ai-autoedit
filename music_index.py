#!/usr/bin/env python3
# music_index.py — analyze BPM, energy, genre for MP3/M4A library
# Usage: python3 music_index.py /path/to/music [--output index.json] [--force]
#
# Genre sources (in priority order):
#   1. Embedded file tags read via ffprobe (ID3/iTunes metadata)
#   2. Filename pattern "｜ Genre ｜" (NCS convention)
#   3. Last.fm API lookup (only if LAST_FM_API_KEY env var is set)
# Output JSON: list of {file, title, artist, genre, bpm, energy, energy_norm, duration}

import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from multiprocessing import Pool

import subprocess
import numpy as np
import librosa
from tqdm import tqdm

LAST_FM_API_KEY = os.environ.get("LAST_FM_API_KEY", "")
LAST_FM_URL = "https://ws.audioscrobbler.com/2.0/"

_SKIP_TAGS = {"seen live", "favorite", "favourites", "love", "owned", "wishlist",
              "albums i own", "beautiful", "awesome", "amazing", "great"}


# ── Metadata from file tags ───────────────────────────────────────────────────

def read_file_tags(path: str) -> dict:
    """
    Read embedded tags via ffprobe.
    Returns dict with keys: genre, artist, title (all lowercase strings or "").
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True
        )
        data = json.loads(result.stdout)
        tags = {k.lower(): v for k, v in data.get("format", {}).get("tags", {}).items()}
        return {
            "genre":  tags.get("genre",  "").strip().lower(),
            "artist": tags.get("artist", tags.get("album_artist", tags.get("performer", ""))).strip(),
            "title":  tags.get("title",  "").strip(),
        }
    except Exception:
        return {"genre": "", "artist": "", "title": ""}


# ── Filename-based fallbacks ──────────────────────────────────────────────────

def extract_genre_from_filename(filename: str) -> str:
    """NCS convention: Artist - Title ｜ Genre ｜ NCS - ..."""
    m = re.search(r'｜\s*([^｜]+?)\s*｜', filename)
    if m:
        return m.group(1).strip().lower()
    return ""


def parse_artist_title_from_filename(filename: str) -> tuple[str, str]:
    """
    Fallback artist/title from filename when tags are missing.
    Handles:
      - "Artist - Title.mp3"
      - "Artist-Album-Title.m4a"
      - "Artist - Title ｜ Genre ｜ NCS.mp3"
    """
    stem = Path(filename).stem
    stem = re.sub(r'\s*｜.*', '', stem).strip()
    m = re.match(r'^(.+?)\s+-\s+(.+)$', stem)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    parts = stem.split('-')
    if len(parts) >= 3:
        return parts[0].replace('_', ' ').strip(), parts[-1].replace('_', ' ').strip()
    if len(parts) == 2:
        return parts[0].replace('_', ' ').strip(), parts[1].replace('_', ' ').strip()
    return "", ""


# ── Last.fm lookup (optional) ─────────────────────────────────────────────────

def lastfm_genre(artist: str, title: str, retries: int = 2) -> str:
    if not LAST_FM_API_KEY or not artist or not title:
        return ""
    params = urllib.parse.urlencode({
        "method": "track.getInfo",
        "api_key": LAST_FM_API_KEY,
        "artist": artist,
        "track": title,
        "format": "json",
        "autocorrect": 1,
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{LAST_FM_URL}?{params}", timeout=5) as r:
                data = json.loads(r.read())
            for tag in data.get("track", {}).get("toptags", {}).get("tag", []):
                name = tag.get("name", "").lower().strip()
                if name and name not in _SKIP_TAGS and len(name) > 1:
                    return name
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.5)
    return ""


def lastfm_artist_genre(artist: str, retries: int = 2) -> str:
    if not LAST_FM_API_KEY or not artist:
        return ""
    params = urllib.parse.urlencode({
        "method": "artist.getTopTags",
        "api_key": LAST_FM_API_KEY,
        "artist": artist,
        "format": "json",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{LAST_FM_URL}?{params}", timeout=5) as r:
                data = json.loads(r.read())
            for tag in data.get("toptags", {}).get("tag", []):
                name = tag.get("name", "").lower().strip()
                if name and name not in _SKIP_TAGS and len(name) > 1:
                    return name
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.5)
    return ""


# ── Audio analysis ────────────────────────────────────────────────────────────

def load_audio_ffmpeg(path: str, sr: int = 22050) -> np.ndarray:
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
        energy = float(np.mean(librosa.feature.rms(y=y)[0]))
        duration = len(y) / sr

        # 1. Embedded tags
        tags = read_file_tags(mp3_path)
        genre  = tags["genre"]
        artist = tags["artist"]
        title  = tags["title"]

        # 2. Filename fallbacks for missing fields
        if not genre:
            genre = extract_genre_from_filename(Path(mp3_path).name)
        if not artist or not title:
            fa, ft = parse_artist_title_from_filename(Path(mp3_path).name)
            if not artist: artist = fa
            if not title:  title  = ft

        return {
            "file":        mp3_path,
            "title":       title or Path(mp3_path).stem,
            "artist":      artist,
            "genre":       genre,   # may still be empty → Last.fm enrichment later
            "bpm":         round(bpm, 1),
            "energy":      round(energy, 6),
            "energy_norm": 0.0,
            "duration":    round(duration, 1),
        }
    except Exception as e:
        print(f"  Error {Path(mp3_path).name}: {e}", file=sys.stderr)
        return None


# ── Last.fm genre enrichment (optional pass) ──────────────────────────────────

def enrich_genres(results: list[dict], force: bool = False) -> int:
    """Fill missing genres via Last.fm. No-op if LAST_FM_API_KEY not set."""
    if not LAST_FM_API_KEY:
        return 0
    to_enrich = [r for r in results if (not r.get("genre") or force) and r.get("artist")]
    if not to_enrich:
        return 0
    print(f"  Last.fm: enriching genres for {len(to_enrich)} tracks...")
    enriched = 0
    for r in tqdm(to_enrich, desc="  Last.fm"):
        genre = lastfm_genre(r["artist"], r["title"] or Path(r["file"]).stem)
        if not genre:
            genre = lastfm_artist_genre(r["artist"])
        if genre:
            r["genre"] = genre
            enriched += 1
        time.sleep(0.25)
    return enriched


# ── Index save ────────────────────────────────────────────────────────────────

def recalc_energy_norm(results: list[dict]):
    energies = [r["energy"] for r in results]
    if not energies:
        return
    e_min, e_max = min(energies), max(energies)
    for r in results:
        r["energy_norm"] = round((r["energy"] - e_min) / (e_max - e_min + 1e-9), 4)


def save(results: list[dict], output_path: Path):
    recalc_energy_norm(results)
    results.sort(key=lambda x: x["bpm"])
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("music_dir")
    parser.add_argument("--output", default=None)
    parser.add_argument("--force", action="store_true", help="Re-analyze all tracks")
    parser.add_argument("--force-genres", action="store_true", help="Re-enrich all genres via Last.fm")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 4))
    args = parser.parse_args()

    music_dir = Path(args.music_dir)
    output_path = Path(args.output) if args.output else music_dir / "index.json"

    mp3_files = sorted(f for ext in ("*.mp3", "*.m4a") for f in music_dir.glob(ext))
    if not mp3_files:
        print(f"No MP3/M4A files found in {music_dir}")
        sys.exit(1)

    existing: dict[str, dict] = {}
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

    if new_files:
        print(f"  Analyzing {len(new_files)} new tracks ({args.workers} workers)...")
        with Pool(args.workers) as pool:
            for entry in tqdm(pool.imap_unordered(analyze_track, new_files), total=len(new_files)):
                if entry:
                    results.append(entry)
                    new_count += 1
                if new_count % 10 == 0:
                    save(results, output_path)

    enriched = enrich_genres(results, force=args.force_genres)
    if enriched:
        print(f"  Genres enriched via Last.fm: {enriched} tracks")

    save(results, output_path)

    genres = sorted({r["genre"] for r in results if r.get("genre")})
    print(f"\nIndexed: {len(results)} tracks ({new_count} new, {enriched} Last.fm lookups)")
    if results:
        print(f"BPM range: {min(r['bpm'] for r in results):.0f} – {max(r['bpm'] for r in results):.0f}")
    if genres:
        print(f"Genres: {', '.join(genres)}")
    print(f"Saved: {output_path}")


if __name__ == "__main__":
    main()
