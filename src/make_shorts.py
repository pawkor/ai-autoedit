#!/usr/bin/env python3
"""
make_shorts.py — generate a 9:16 YouTube Shorts clip from existing scored scenes.

Features:
  - Top-scored scenes, center-cropped to 9:16 (1080×1920)
  - Dynamic xfade transitions between shots (zoomin, radial, fadewhite…)
  - Animated text overlays (PIL-rendered, rotated, fly-in from random direction)
  - Auto-picked highest-energy music from library, with fade-out

Usage:
    python3 make_shorts.py <work_dir>
    python3 make_shorts.py <work_dir> --duration 30 --shot 1.5
    python3 make_shorts.py <work_dir> --music /data/music/track.mp3 --no-text
"""

import argparse
import csv
import io
import json
import math
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import warnings
# librosa uses stacklevel>1 so warnings are attributed to the caller's module,
# not to "librosa" — match by message pattern instead of module name.
# PySoundFile warning fires for formats libsndfile doesn't support (e.g. .m4a/AAC);
# librosa falls back to audioread transparently — safe to suppress.
warnings.filterwarnings("ignore", message=".*PySoundFile failed.*")
warnings.filterwarnings("ignore", message=".*audioread.*")
warnings.filterwarnings("ignore", category=FutureWarning, module="librosa")

from PIL import Image, ImageDraw, ImageFont

# ── Fixed intro words (first 2 shots) ─────────────────────────────────────────
INTRO_WORDS = ["#EPIC", "#ADVENTURE"]

# ── Hashtag word pool (shots 3+) — short, punchy, high-discovery tags ──────────
# Chosen for motorcycle/adventure content on YouTube Shorts:
#   - broad reach: #moto #motorcycle #riding
#   - niche/community: #ktm #enduro #adventurebike #motovlog
#   - emotion/style: #freedom #speed #twisties #curves
#   - visual context: #mountains #passes #roadtrip #explore
# Shown without '#' prefix (cleaner on screen); '#' is in the video description.
WORDS = [
    "#MOTO",       "#KTM",        "#RIDING",     "#SPEED",
    "#FREEDOM",    "#TWISTIES",   "#CURVES",     "#MOUNTAINS",
    "#EXPLORE",    "#ROADTRIP",   "#MOTOVLOG",   "#PASSES",
    "#BIKERS",     "#TWOHEELS",   "#PURE MOTO",  "#WIDE OPEN",
    "#NO LIMITS",  "#FULL SEND",  "#ON THE ROAD","#OPEN ROAD",
]

# ── xfade transitions — dynamic / explosive feel ───────────────────────────────
TRANSITIONS = [
    "zoomin", "fadewhite", "radial",
    "circleopen", "circleclose",
    "squeezeh", "squeezev",
    "pixelize", "fadeblack",
    "wipeleft", "wiperight",
]

FONT_PATH  = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
XFADE_DUR  = 0.20   # seconds
FONT_SIZE  = 110    # px — rendered into 1080-wide space
BORDER_PX  = 8      # text border/stroke width


# ── Text rendering with PIL ────────────────────────────────────────────────────

def render_text_png(word: str, angle_deg: float, width: int, height: int) -> Path:
    """
    Render word as a transparent PNG with black stroke + white fill, rotated by angle_deg.
    Returns path to a temp PNG (caller responsible for cleanup via tempdir).
    """
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except Exception:
        font = ImageFont.load_default()

    # Measure text on a throw-away canvas
    dummy = Image.new("RGBA", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy)
    bb = dummy_draw.textbbox((0, 0), word, font=font)
    tw = bb[2] - bb[0] + BORDER_PX * 4
    th = bb[3] - bb[1] + BORDER_PX * 4

    # Draw text on its own canvas (with border)
    canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw   = ImageDraw.Draw(canvas)
    ox, oy = BORDER_PX * 2, BORDER_PX * 2

    # Stroke: draw text offset in 8 directions
    for dx in range(-BORDER_PX, BORDER_PX + 1):
        for dy in range(-BORDER_PX, BORDER_PX + 1):
            if dx == 0 and dy == 0:
                continue
            dist = math.hypot(dx, dy)
            if dist <= BORDER_PX:
                draw.text((ox + dx, oy + dy), word, font=font, fill=(0, 0, 0, 255))
    # Fill
    draw.text((ox, oy), word, font=font, fill=(255, 255, 255, 255))

    # Rotate (expand canvas to avoid clipping)
    rotated = canvas.rotate(-angle_deg, expand=True, resample=Image.BICUBIC)

    # Compose onto final frame-size canvas (transparent background)
    frame = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    rx, ry = rotated.size
    paste_x = (width  - rx) // 2
    paste_y = (height - ry) // 2
    frame.paste(rotated, (paste_x, paste_y), rotated)

    return frame   # return PIL image; caller saves to disk


def _fly_in_expr(direction: str, img_w: int, img_h: int,
                 frame_w: int, frame_h: int,
                 final_x: int, final_y: int, fly_dur: float) -> tuple[str, str]:
    """
    Return ffmpeg overlay (x, y) expressions that animate the text image
    flying in from `direction` over `fly_dur` seconds, then holding position.
    """
    # start positions (off-screen)
    starts = {
        "left":   (-img_w,          final_y),
        "right":  (frame_w,         final_y),
        "top":    (final_x,         -img_h),
        "bottom": (final_x,         frame_h),
    }
    sx, sy = starts[direction]

    # Linear easing: pos = start + (final-start)*(t/dur)  for t < dur, else final
    # If fly_dur=0: text is at final position from frame 0 (no animation).
    def expr_1d(s: int, f: int, dur: float) -> str:
        if dur <= 0 or s == f:
            return str(f)
        return f"if(lt(t,{dur}),{s}+({f}-({s}))*t/{dur},{f})"

    return expr_1d(sx, final_x, fly_dur), expr_1d(sy, final_y, fly_dur)


def make_clip(src: Path, ss: float, shot_dur: float,
              word: str, angle: float, direction: str,
              width: int, height: int,
              tmp: Path, idx: int, use_text: bool,
              xfade_dur: float = XFADE_DUR) -> Path | None:
    """
    Crop + scale source clip, overlay animated text image.
    Two-pass: (1) crop/scale, (2) overlay text.
    """
    crop_out = tmp / f"crop_{idx:04d}.mp4"
    out      = tmp / f"clip_{idx:04d}.mp4"

    # ── Pass 1: crop + scale (fast, high quality) ──────────────────────────
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{ss:.3f}", "-i", str(src), "-t", str(shot_dur),
        "-vf", f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale={width}:{height}:flags=lanczos",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an", str(crop_out),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"  [{idx+1}] crop error")
        print(r.stderr.decode(errors="replace")[-400:])
        return None

    if not use_text:
        return crop_out

    # ── Render text PNG ────────────────────────────────────────────────────
    text_img = render_text_png(word, angle, width, height)
    png_path = tmp / f"text_{idx:04d}.png"
    text_img.save(str(png_path))
    iw, ih = text_img.size

    # ── Fly-in position ─────────────────────────────────────────────────────
    # Final position: centered horizontally, vertical based on idx (top/bottom)
    final_x = (width  - iw) // 2
    final_y = int(height * (0.10 if idx % 2 == 0 else 0.68))

    fly_dur  = 0.0    # text appears on frame 1 (no fly-in delay)
    text_end = shot_dur - xfade_dur - 0.05  # disappear before xfade

    ox, oy = _fly_in_expr(direction, iw, ih, width, height, final_x, final_y, fly_dur)

    # overlay visible from 0 to text_end
    vf = (
        f"[0:v][1:v]overlay="
        f"x='if(between(t,0,{text_end:.2f}),{ox},NAN)':"
        f"y='if(between(t,0,{text_end:.2f}),{oy},NAN)'"
        f":format=auto,format=yuv420p[v]"
    )

    # ── Pass 2: overlay ─────────────────────────────────────────────────────
    cmd = [
        "ffmpeg", "-y",
        "-i", str(crop_out),
        "-i", str(png_path),
        "-filter_complex", vf,
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an", str(out),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"  [{idx+1}] overlay error")
        print(r.stderr.decode(errors="replace")[-400:])
        return crop_out   # fallback: use clip without text

    return out


def build_xfade_graph(n_clips: int, shot_dur: float, transitions: list[str],
                      xfade_dur: float = XFADE_DUR) -> tuple[str, str]:
    if n_clips == 1:
        return "[0:v]format=yuv420p[vout]", "[vout]"
    parts = []
    prev  = "[0:v]"
    for i, tr in enumerate(transitions):
        offset = (i + 1) * shot_dur - (i + 1) * xfade_dur
        label  = f"[v{i+1:02d}]" if i < len(transitions) - 1 else "[vraw]"
        parts.append(
            f"{prev}[{i+1}:v]xfade=transition={tr}:"
            f"duration={xfade_dur}:offset={offset:.4f}{label}"
        )
        prev = label
    parts.append("[vraw]format=yuv420p[vout]")
    return ";\n".join(parts), "[vout]"


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        stderr=subprocess.DEVNULL,
    )
    for s in json.loads(out).get("streams", []):
        if s.get("codec_type") == "video":
            try:
                return float(s["duration"])
            except (KeyError, ValueError):
                pass
    return 0.0


def find_best_offset(music_path: Path, target_dur: float) -> float:
    """
    Find the most dynamically dense segment of `target_dur` seconds using onset density.

    Algorithm:
      1. Load audio (mono, native sr).
      2. Compute onset strength envelope via librosa — each frame represents how
         strongly a new note/beat onset occurs at that moment.
      3. Slide a window of `target_dur` seconds across the envelope and sum onset
         strength within each window.  The window with the highest cumulative onset
         density is the most musically "packed" / energetic segment.
      4. Return the start time of that window in seconds.

    Onset strength is preferred over raw RMS energy because it is sensitive to
    rhythmic density and transients (drops, builds, fast percussion) rather than
    just loudness — exactly what makes a Short feel energetic.
    """
    import librosa
    import numpy as np

    print(f"  Analysing onset density: {music_path.name} …", end="", flush=True)

    y, sr = librosa.load(str(music_path), sr=None, mono=True)
    track_dur = len(y) / sr

    if track_dur <= target_dur:
        print(" (track shorter than target — using start)")
        return 0.0

    hop = 512
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)

    fps           = sr / hop
    window_frames = int(target_dur * fps)

    # Cumulative-sum trick for O(n) sliding window
    cumsum        = np.concatenate(([0.0], np.cumsum(onset_env)))
    window_sums   = cumsum[window_frames:] - cumsum[:-window_frames]

    best_frame = int(np.argmax(window_sums))
    best_time  = best_frame / fps

    # Safety margin: don't start so late that we'd run out of audio before fade-out
    max_start = track_dur - target_dur - 2.0
    best_time = max(0.0, min(best_time, max_start))

    best_score = float(window_sums[best_frame])
    print(f" offset={best_time:.1f}s  density={best_score:.1f}")
    return best_time


def _used_tracks(music_dirs: list[Path]) -> set[str]:
    """Load the set of recently used track paths from shorts_used.json files."""
    used: set[str] = set()
    for root in music_dirs:
        p = root / "shorts_used.json"
        if p.exists():
            try:
                used.update(json.loads(p.read_text()))
            except Exception:
                pass
    return used


def _mark_track_used(track_path: Path, music_dirs: list[Path]):
    """Append track to shorts_used.json in the first music dir."""
    if not music_dirs:
        return
    p = music_dirs[0] / "shorts_used.json"
    try:
        used: list[str] = json.loads(p.read_text()) if p.exists() else []
    except Exception:
        used = []
    path_str = str(track_path)
    if path_str not in used:
        used.append(path_str)
    p.write_text(json.dumps(used, indent=2))


def pick_music(music_dirs: list[Path]) -> Path | None:
    """
    Pick the highest-energy unplayed track from the music library.
    Tracks used in previous Shorts runs are recorded in shorts_used.json.
    If all tracks have been used, the history is reset and selection starts fresh.
    """
    # Collect all available indexed tracks
    all_tracks: list[tuple[float, Path]] = []   # (energy, path)
    for root in music_dirs:
        for idx_path in root.rglob("index.json"):
            try:
                data   = json.loads(idx_path.read_text())
                tracks = data if isinstance(data, list) else list(data.values())
                for t in tracks:
                    f = Path(t.get("file", ""))
                    if f.exists():
                        all_tracks.append((float(t.get("energy", 0)), f))
            except Exception:
                continue

    if not all_tracks:
        return None

    all_tracks.sort(reverse=True)   # highest energy first

    used = _used_tracks(music_dirs)

    # Filter out recently used tracks
    fresh = [(e, f) for e, f in all_tracks if str(f) not in used]

    if not fresh:
        # All tracks have been used — reset history and start over
        print("  (all tracks used — resetting history)")
        for root in music_dirs:
            p = root / "shorts_used.json"
            if p.exists():
                p.write_text("[]")
        fresh = all_tracks

    _energy, chosen = fresh[0]
    _mark_track_used(chosen, music_dirs)
    return chosen


def read_project_title(work_dir: Path) -> list[str]:
    """
    Read [job] title from work_dir/config.ini and split into intro words.

    "2025 Bałkany / The Balkans" → ["2025", "Bałkany", "The Balkans"]
    Splits on '/' first (phrase boundary), then each phrase is kept whole
    unless it's a single token — giving one word/phrase per shot.
    """
    try:
        import configparser
        cp = configparser.ConfigParser()
        cp.read(str(work_dir / "config.ini"))
        raw = cp.get("job", "title", fallback="").replace("\\n", "\n").strip()
        if not raw:
            return []
        # Use only the first line (skip subtitles on line 2+)
        first_line = raw.splitlines()[0].strip()
        # Rule: text before first '/' is split word-by-word (year, place name…)
        #       text after '/' stays as one phrase (translation, subtitle)
        # e.g. "2025 Bałkany / The Balkans" → ["2025", "BAŁKANY", "THE BALKANS"]
        parts = first_line.split("/", 1)
        words: list[str] = []
        # Part before '/': split by spaces
        for token in parts[0].split():
            if token:
                words.append(token.upper())
        # Part after '/' (if present): one phrase
        if len(parts) > 1:
            phrase = parts[1].strip()
            if phrase:
                words.append(phrase.upper())
        return words
    except Exception:
        return []


def _output_name(work_dir: Path) -> str:
    parts = [p for p in work_dir.parts if p]
    year = loc = day = ""
    for i, p in enumerate(parts):
        if re.match(r"^\d{4}$", p):
            year = p
            if i + 1 < len(parts) and re.match(r"^\d{2}-", parts[i + 1]):
                loc = parts[i + 1]
            if i + 2 < len(parts):
                day = parts[i + 2]
            break
    tokens = [t for t in [year, loc, day] if t]
    return "-".join(tokens) if tokens else work_dir.name


def _next_version(work_dir: Path, base: str) -> str:
    nums = [
        int(m.group(1))
        for p in work_dir.glob(f"{base}-short_v*.mp4")
        if (m := re.search(r"-short_v(\d+)\.mp4$", p.name))
    ]
    return f"v{max(nums, default=0) + 1:02d}"


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir")
    ap.add_argument("--duration",   type=float, default=30.0)
    ap.add_argument("--shot",       type=float, default=1.5,  help="Seconds per shot (default 1.5)")
    ap.add_argument("--top",        type=int,   default=0,    help="Force top N scenes (0=auto)")
    ap.add_argument("--music",      default="",               help="Path to music file (auto-pick if omitted)")
    ap.add_argument("--music-dir",  default="",               help="Root dir for music index search")
    ap.add_argument("--transition", default="",               help="Force one xfade transition for all")
    ap.add_argument("--xfade-dur",  type=float, default=XFADE_DUR)
    ap.add_argument("--width",      type=int,   default=1080)
    ap.add_argument("--height",     type=int,   default=1920)
    ap.add_argument("--text",       action="store_true", help="Overlay animated text words per shot")
    ap.add_argument("--seed",       type=int,   default=None)
    args = ap.parse_args()

    xfade_dur = args.xfade_dur

    if args.seed is not None:
        random.seed(args.seed)

    work_dir    = Path(args.work_dir).resolve()
    auto_dir    = work_dir / "_autoframe"
    scores_csv  = auto_dir / "scene_scores.csv"
    autocut_dir = auto_dir / "autocut"

    if not scores_csv.exists():
        sys.exit(f"ERROR: {scores_csv} not found")
    if not autocut_dir.is_dir():
        sys.exit(f"ERROR: {autocut_dir} not found")

    # ── Scenes ────────────────────────────────────────────────────────────────
    raw: list[tuple[float, str]] = []
    with open(scores_csv, newline="") as f:
        for row in csv.DictReader(f):
            try:
                raw.append((float(row["score"]), row["scene"]))
            except (KeyError, ValueError):
                continue
    if not raw:
        sys.exit("ERROR: scene_scores.csv is empty")

    # Per-camera score normalisation (mirrors server.py job_frames logic).
    # Without this, one camera may dominate top-N if its raw scores are higher.
    cam_csv = auto_dir / "camera_sources.csv"
    if cam_csv.exists():
        # Build source → camera map
        cam_map: dict[str, str] = {}
        with open(cam_csv, newline="") as f:
            for row in csv.DictReader(f):
                cam_map[row["source"]] = row["camera"]

        # Group scenes by camera
        by_cam: dict[str, list[int]] = {}
        for i, (_, scene) in enumerate(raw):
            src = re.sub(r"-scene-\d+$", "", scene)
            cam = cam_map.get(src, "default")
            by_cam.setdefault(cam, []).append(i)

        if len(by_cam) > 1:
            scores = [s for s, _ in raw]
            for cam, idxs in by_cam.items():
                lo = min(scores[i] for i in idxs)
                hi = max(scores[i] for i in idxs)
                for i in idxs:
                    scores[i] = (scores[i] - lo) / (hi - lo) if hi > lo else 1.0
            raw = [(scores[i], raw[i][1]) for i in range(len(raw))]
            cams_str = ", ".join(f"{c}:{len(v)}" for c, v in by_cam.items())
            print(f"Multicam normalisation: {cams_str}")

    scenes = sorted(raw, reverse=True)

    # Account for xfade overlap: N*shot - (N-1)*xfade = duration
    # → N = (duration - xfade) / (shot - xfade). Add +4 as buffer for skipped scenes.
    _eff = max(args.shot - xfade_dur, 0.01)
    n_shots    = args.top if args.top > 0 else max(1, math.ceil((args.duration - xfade_dur) / _eff) + 4)
    candidates = scenes[:n_shots]
    print(f"Scenes: {len(scenes)} available  |  picking top {len(candidates)}")
    print(f"Score range: {candidates[-1][0]:.3f} – {candidates[0][0]:.3f}")
    print(f"Shot: {args.shot}s  |  xfade: {xfade_dur}s  |  target: ~{len(candidates)*args.shot:.0f}s\n")

    # ── Music ─────────────────────────────────────────────────────────────────
    music_file: Path | None = Path(args.music) if args.music else None
    if not music_file:
        search_roots = []
        if args.music_dir:
            search_roots.append(Path(args.music_dir))
        try:
            import configparser
            cp = configparser.ConfigParser()
            cp.read(str(Path(__file__).parent.parent / "config.ini"))
            d = cp.get("music", "dir", fallback="")
            if d:
                shorts_dir = Path(d) / "shorts"
                search_roots.append(shorts_dir if shorts_dir.is_dir() else Path(d))
        except Exception:
            pass
        if search_roots:
            music_file = pick_music(search_roots)
    if music_file:
        music_ss = find_best_offset(music_file, args.duration)
        print(f"Music: {music_file.name}  ss={music_ss:.1f}s\n")
    else:
        music_ss = 0.0
        print("Music: none\n")

    # ── Word / angle / direction pools ────────────────────────────────────────
    # First 2 shots: fixed "EPIC" / "ADVENTURE"; rest: hashtag pool
    intro_words = INTRO_WORDS if args.text else []
    if intro_words:
        print(f"Intro words: {' · '.join(intro_words)}")

    rand_pool = WORDS.copy()
    random.shuffle(rand_pool)
    while len(rand_pool) < len(candidates):
        extra = WORDS.copy()
        random.shuffle(extra)
        rand_pool.extend(extra)

    # Final word list: intro first, then random pool for the rest
    word_pool = intro_words + rand_pool
    # Trim to number of candidates (intro may be longer than candidates in edge cases)
    while len(word_pool) < len(candidates):
        word_pool.extend(rand_pool)

    directions = ["left", "right", "top", "bottom"]
    angles     = list(range(-15, 16, 3))   # -15°…+15° in steps of 3°

    tr_pool = TRANSITIONS.copy()
    random.shuffle(tr_pool)

    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(_tmp)

        # ── Per-shot clips ────────────────────────────────────────────────────
        clips = []
        for i, (score, scene) in enumerate(candidates):
            src = autocut_dir / f"{scene}.mp4"
            if not src.exists():
                print(f"  [{i+1:2d}] SKIP {scene}")
                continue
            dur       = probe_duration(src)
            if dur < args.shot:
                print(f"  [{i+1:2d}] SKIP {scene} (scene {dur:.2f}s < shot {args.shot}s)")
                continue
            # Skip first 20% of scene (camera still settling after cut),
            # skip last 10% (approaching next cut). Random offset within window.
            # With --seed this is reproducible; vary seed to get different frames.
            win_start = dur * 0.20
            win_end   = dur * 0.90 - args.shot
            if win_end > win_start:
                ss = random.uniform(win_start, win_end)
            else:
                ss = min(dur * 0.20, dur - args.shot)  # narrow window: clamp to ensure full shot fits
            word      = word_pool[i]
            angle     = random.choice(angles)
            direction = random.choice(directions)

            clip = make_clip(src, ss, args.shot, word, angle, direction,
                             args.width, args.height, tmp, i, args.text,
                             xfade_dur)
            if clip:
                clips.append((clip, probe_duration(clip)))
                print(f"  [{i+1:2d}/{len(candidates)}] score={score:.3f}  ss={ss:.1f}s  "
                      f"word={word}  angle={angle:+d}°  from={direction}")

        if not clips:
            sys.exit("ERROR: no clips generated")

        # Unpack clips — [(path, actual_duration), ...]
        clip_paths = [c for c, _ in clips]

        # ── Transitions ───────────────────────────────────────────────────────
        n_tr = len(clip_paths) - 1
        if args.transition:
            transitions = [args.transition] * n_tr
        else:
            # Intro shots get impactful transitions; rest are random
            n_intro_tr = max(0, len(intro_words) - 1)
            intro_transitions = (["zoomin", "fadewhite"] * (n_intro_tr // 2 + 1))[:n_intro_tr]
            rest_transitions  = [tr_pool[i % len(tr_pool)] for i in range(n_tr - n_intro_tr)]
            transitions = intro_transitions + rest_transitions
        if n_tr:
            print(f"\nTransitions: {', '.join(transitions)}")

        # ── Pass 1: video-only (xfade chain, no audio) ───────────────────────
        base      = _output_name(work_dir)
        version   = _next_version(work_dir, base)
        out_path  = work_dir / f"{base}-short_{version}.mp4"

        fc, vout = build_xfade_graph(len(clip_paths), args.shot, transitions, xfade_dur)

        inputs = []
        for c in clip_paths:
            inputs += ["-i", str(c)]

        print("\n  Encoding video...", flush=True)
        video_only = tmp / "video_only.mp4"
        cmd_v = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", fc,
            "-map", f"{vout}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "tv", "-preset", "fast", "-crf", "22",
            "-an", str(video_only),
        ]
        r = subprocess.run(cmd_v, capture_output=True)
        if r.returncode != 0:
            print("ERROR in video encode:")
            print(r.stderr.decode(errors="replace")[-800:])
            sys.exit(1)

        # Probe the ACTUAL video duration — xfade rounding can differ from the
        # theoretical sum(clip_durs) - n_tr*xfade_dur, causing audio overshoot.
        actual_dur = probe_duration(video_only)
        fade_st    = max(0, actual_dur - 2.0)

        # ── Pass 2: mux audio trimmed to exact video duration ─────────────────
        print("  Muxing audio...", flush=True)
        if music_file:
            cmd_a = [
                "ffmpeg", "-y",
                "-i", str(video_only),
                "-ss", f"{music_ss:.3f}", "-i", str(music_file),
                "-filter_complex",
                f"[1:a]atrim=0:{actual_dur:.4f},"
                f"apad=whole_dur={actual_dur:.4f},"
                f"afade=t=out:st={fade_st:.2f}:d=2[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-t", f"{actual_dur:.4f}",
                "-movflags", "+faststart",
                str(out_path),
            ]
            r = subprocess.run(cmd_a, capture_output=True)
            if r.returncode != 0:
                print("ERROR in audio mux:")
                print(r.stderr.decode(errors="replace")[-800:])
                sys.exit(1)
        else:
            video_only.rename(out_path)
            actual_dur = probe_duration(out_path)

    music_info = f" + {music_file.name[:40]}" if music_file else ""
    text_info  = " + rotated text fly-in" if args.text else ""
    print(f"\n✓ {out_path.name}")
    print(f"  {len(clip_paths)} shots × {args.shot}s  xfade {xfade_dur}s{text_info}{music_info}")
    print(f"  {args.width}×{args.height}  |  {actual_dur:.1f}s")


if __name__ == "__main__":
    main()
