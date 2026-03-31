#!/usr/bin/env python3
"""
pipeline.py — Python orchestrator replacing autoframe.sh.
Called from webapp/server.py as an async generator.

Usage (server.py):
    async for line in pipeline.run(params, work_dir):
        emit(line)
"""

import asyncio
import configparser
import json
import os
import pandas as pd
import random
import re
import sys
import time
from pathlib import Path
from typing import AsyncIterator, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR    = SCRIPT_DIR.parent


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_cfg(work_dir: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read([str(APP_DIR / "config.ini"), str(work_dir / "config.ini")])
    return cp

def _s(cp, sec, key, fb=""):     return cp.get(sec, key, fallback=fb)
def _f(cp, sec, key, fb=0.0):    return cp.getfloat(sec, key, fallback=fb)
def _i(cp, sec, key, fb=0):      return cp.getint(sec, key, fallback=fb)


# ── Subprocess helpers ────────────────────────────────────────────────────────

async def _probe_duration(path: Path, ffprobe: str) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "quiet", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.strip())
    except Exception:
        return None


async def _run(cmd: list, cwd=None, env=None) -> tuple[int, str]:
    """Run command, return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", errors="replace")


# ── Versioning helper ─────────────────────────────────────────────────────────

def _next_version(path: Path) -> Path:
    """Return path with _vN suffix, e.g. highlight_final_music_v2.mp4."""
    parent, ext = path.parent, path.suffix
    base = re.sub(r'_v\d+$', '', path.stem)
    nums = [
        int(m.group(1))
        for f in parent.glob(f"{base}_v*{ext}")
        if (m := re.match(rf'^{re.escape(base)}_v(\d+)$', f.stem))
    ]
    return parent / f"{base}_v{max(nums, default=0) + 1}{ext}"


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run(params: dict, work_dir: Path,
              analyze_only: bool = False,
              selected_track: Optional[str] = None) -> AsyncIterator[str]:
    """
    Async generator yielding log lines.
    Raises RuntimeError on unrecoverable errors.
    """
    t_start = time.time()
    cp      = _load_cfg(work_dir)

    # ── Parameters ────────────────────────────────────────────────────────────
    threshold   = float(params.get("threshold")  or _f(cp, "scene_selection", "threshold",        0.148))
    max_scene   = float(params.get("max_scene")  or _f(cp, "scene_selection", "max_scene_sec",    10))
    per_file    = float(params.get("per_file")   or _f(cp, "scene_selection", "max_per_file_sec", 45))
    no_intro    = bool(params.get("no_intro",  False))
    no_music    = bool(params.get("no_music",  False))
    cam_a       = str(params.get("cam_a")  or "")
    cam_b       = str(params.get("cam_b")  or "")
    music_genre  = str(params.get("music_genre")  or "")
    music_artist = str(params.get("music_artist") or "")
    music_files_filter = params.get("music_files") or []

    # Music dir: explicit param > config.ini > default
    _md = str(params.get("music_dir") or "")
    music_dir = Path(os.path.expanduser(_md)) if _md else \
                Path(os.path.expanduser(_s(cp, "music", "dir", "~/music")))

    # Title
    title = str(params.get("title") or "")
    if not title:
        parts = work_dir.parts
        year  = next((p for p in parts if re.match(r"^\d{4}$", p)), "")
        trip  = ""
        if year and year in parts:
            idx = parts.index(year)
            if idx + 1 < len(parts):
                raw  = parts[idx + 1]
                trip = re.sub(r"^\d+[-.]?", "", raw).replace("-", " ").strip()
        title = f"{year}\n{trip}" if trip else year

    # Paths
    ffmpeg      = os.path.expanduser(_s(cp, "paths", "ffmpeg",  "ffmpeg"))
    ffprobe     = os.path.expanduser(_s(cp, "paths", "ffprobe", "ffprobe"))
    font        = os.path.expanduser(_s(cp, "intro_outro", "font",
                                        "~/fonts/Caveat-Bold.ttf"))
    work_subdir = _s(cp, "paths", "work_subdir", "_autoframe")

    # Video settings
    resolution    = _s(cp, "video", "resolution",   "3840:2160")
    framerate     = _s(cp, "video", "framerate",    "60")
    audio_bitrate = _s(cp, "video", "audio_bitrate","192k")
    nvenc_cq      = _s(cp, "video", "nvenc_cq",     "18")
    nvenc_preset  = _s(cp, "video", "nvenc_preset",  "p5")
    x264_crf      = _s(cp, "video", "x264_crf",     "15")
    x264_preset   = _s(cp, "video", "x264_preset",  "fast")

    # Scene detection
    sd_threshold = _s(cp, "scene_detection", "threshold",    "20")
    sd_min_scene = _s(cp, "scene_detection", "min_scene_len","8s")

    # Intro/outro
    intro_dur    = _s(cp, "intro_outro", "duration",         "3")
    fade_dur     = float(_s(cp, "intro_outro", "fade_duration", "1"))
    outro_text   = _s(cp, "intro_outro", "outro_text",       "Editing powered by AI")
    fsize_title  = _s(cp, "intro_outro", "font_size_title",  "120")
    fsize_sub    = _s(cp, "intro_outro", "font_size_subtitle","96")
    fsize_outro  = _s(cp, "intro_outro", "font_size_outro",  "60")

    # Music
    music_fade = float(_s(cp, "music", "fade_out_duration", "3"))
    music_vol  = _s(cp, "music", "music_volume",   "0.7")
    orig_vol   = _s(cp, "music", "original_volume","0.3")

    # ── Setup dirs ────────────────────────────────────────────────────────────
    auto_dir = work_dir / work_subdir
    for d in ["autocut", "frames", "csv", "trimmed"]:
        (auto_dir / d).mkdir(parents=True, exist_ok=True)

    # ── Detect encoder ────────────────────────────────────────────────────────
    enc_proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-encoders",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    enc_out, _ = await enc_proc.communicate()
    if b"h264_nvenc" in enc_out:
        vid_codec   = "h264_nvenc"
        vid_quality = ["-rc", "vbr", "-cq", nvenc_cq, "-b:v", "0", "-preset", nvenc_preset]
        hwaccel     = ["-hwaccel", "cuda"]
    else:
        vid_codec   = "libx264"
        vid_quality = ["-crf", x264_crf, "-preset", x264_preset]
        hwaccel     = []

    # ── Header ────────────────────────────────────────────────────────────────
    title_line1 = title.split("\n")[0]
    yield f"Threshold: {threshold}  Max scene: {max_scene}s  Per file: {per_file}s  Title: {title_line1}"
    yield ""

    # ── [1/6] Find source files ───────────────────────────────────────────────
    def _is_source(f: Path) -> bool:
        n = f.name.lower()
        return n.endswith(".mp4") and not n.startswith("highlight") and not n.endswith(".lrv")

    if cam_a and cam_b:
        source_files = sorted(
            [f for f in (work_dir / cam_a).glob("*.mp4") if _is_source(f)] +
            [f for f in (work_dir / cam_b).glob("*.mp4") if _is_source(f)]
        )
    else:
        source_files = sorted(f for f in work_dir.glob("*.mp4") if _is_source(f))

    yield f"[1/6] Found {len(source_files)} source files"

    if not source_files:
        raise RuntimeError(f"No MP4 files found in {work_dir}")

    if cam_a and cam_b:
        cam_csv = auto_dir / "camera_sources.csv"
        with open(cam_csv, "w") as fh:
            fh.write("source,camera\n")
            for sf in source_files:
                cam = cam_a if f"/{cam_a}/" in str(sf) else cam_b
                fh.write(f"{sf.stem},{cam}\n")
        yield f"  Dual-cam: {cam_a} / {cam_b}"

    # ── [2/6] Scene detection (sequential with progress) ─────────────────────
    yield ""
    total_detect = len(source_files)
    yield f"[2/6] Scene detection ({total_detect} files)..."

    for i, sf in enumerate(source_files, 1):
        csv = auto_dir / "csv" / f"{sf.stem}-Scenes.csv"
        if csv.exists():
            count = max(0, sum(1 for _ in open(csv)) - 2)
            yield f"  [{i}/{total_detect}] ✓ {sf.name} ({count} scenes, cached)"
            continue

        yield f"  [{i}/{total_detect}] ▶ {sf.name}..."
        proc = await asyncio.create_subprocess_exec(
            "scenedetect", "-f", "3", "-i", str(sf),
            "detect-content", "--threshold", sd_threshold,
            "--min-scene-len", sd_min_scene,
            "list-scenes", "-o", str(auto_dir / "csv"),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        buf = b""
        while True:
            chunk = await proc.stderr.read(256)
            if not chunk:
                break
            buf += chunk
            parts = buf.replace(b"\r", b"\n").split(b"\n")
            buf = parts[-1]
            for part in parts[:-1]:
                line = part.decode("utf-8", errors="replace").strip()
                if line:
                    yield f"\r    {line}"
        await proc.wait()
        count = max(0, sum(1 for _ in open(csv)) - 2) if csv.exists() else 0
        status = "✓" if csv.exists() else "✗"
        yield f"  [{i}/{total_detect}] {status} {sf.name}: {count} scenes"

    # ── [3/6] Split scenes ───────────────────────────────────────────────────
    yield ""
    total_split = len(source_files)
    yield f"[3/6] Splitting scenes... (0/{total_split})"

    for split_i, sf in enumerate(source_files, 1):
        csv = auto_dir / "csv" / f"{sf.stem}-Scenes.csv"
        if not csv.exists():
            yield f"  [{split_i}/{total_split}] ✗ {sf.name}: no CSV, skipping"
            continue
        try:
            expected = max(0, sum(1 for _ in open(csv)) - 2)
        except Exception:
            expected = 0
        existing = len(list((auto_dir / "autocut").glob(f"{sf.stem}-scene-*.mp4")))
        if existing >= expected > 0:
            yield f"  [{split_i}/{total_split}] ✓ {sf.name} ({existing} scenes, cached)"
            continue
        yield f"  [{split_i}/{total_split}] {sf.name} — splitting {expected} scenes..."
        proc = await asyncio.create_subprocess_exec(
            "scenedetect", "-i", str(sf),
            "load-scenes", "-i", str(csv),
            "split-video", "-o", str(auto_dir / "autocut"),
            "--filename", f"{sf.stem}-scene-$SCENE_NUMBER",
            "--copy",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        done = len(list((auto_dir / "autocut").glob(f"{sf.stem}-scene-*.mp4")))
        yield f"  [{split_i}/{total_split}] ✓ {sf.name} ({done} scenes)"

    scene_files = sorted((auto_dir / "autocut").glob("*.mp4"))
    yield f"  Total: {len(scene_files)} scenes"
    if not scene_files:
        raise RuntimeError("No scenes produced. Check source files and scenedetect output.")

    # ── [3b] Validate autocut clips, re-encode corrupt ones ──────────────────
    yield ""
    yield "[3b] Validating scene clips..."

    _val_cache = auto_dir / "validation_ok.txt"
    _val_cached = False
    try:
        if _val_cache.exists() and int(_val_cache.read_text().strip()) == len(scene_files):
            _val_cached = True
    except Exception:
        pass

    async def _clip_ok(mp4: Path) -> bool:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0", str(mp4),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode == 0 and b"video" in stdout

    async def _reencode_from_source(mp4: Path) -> str:
        m = re.match(r'^(.+)-scene-(\d+)$', mp4.stem)
        if not m:
            mp4.unlink(missing_ok=True)
            return f"  ⚠ Cannot parse name, removed: {mp4.name}"
        source_stem, scene_num = m.group(1), int(m.group(2))
        scene_csv = auto_dir / "csv" / f"{source_stem}-Scenes.csv"
        if not scene_csv.exists():
            mp4.unlink(missing_ok=True)
            return f"  ⚠ No scene CSV for {source_stem}, removed: {mp4.name}"
        source_file = None
        for sf in source_files:
            if sf.stem == source_stem:
                source_file = sf
                break
        if not source_file:
            mp4.unlink(missing_ok=True)
            return f"  ⚠ Source not found for {mp4.name}, removed"
        try:
            import csv as _csv
            with open(scene_csv, newline="") as f:
                rows = list(_csv.DictReader(f))
            row = rows[scene_num - 1]
            start_sec = float(row["Start Time (seconds)"])
            end_sec   = float(row["End Time (seconds)"])
        except Exception as e:
            mp4.unlink(missing_ok=True)
            return f"  ⚠ Cannot read timestamps for {mp4.name}: {e}, removed"
        tmp = mp4.with_suffix(".reencode.mp4")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y",
            "-ss", f"{start_sec:.3f}", "-to", f"{end_sec:.3f}",
            "-i", str(source_file),
            "-c:v", vid_codec, *vid_quality,
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-vf", f"scale={resolution}",
            "-r", framerate,
            str(tmp), "-loglevel", "quiet",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if tmp.exists() and tmp.stat().st_size > 100_000:
            mp4.unlink(missing_ok=True)
            tmp.rename(mp4)
            return f"  ✓ Re-encoded {mp4.name} ({start_sec:.1f}s–{end_sec:.1f}s)"
        tmp.unlink(missing_ok=True)
        mp4.unlink(missing_ok=True)
        return f"  ✗ Re-encode failed for {mp4.name}, removed"

    if _val_cached:
        yield f"  ✓ Cached — {len(scene_files)} clips already validated, skipping"
    else:
        corrupt_clips = []
        total_clips = len(scene_files)
        report_every = max(1, total_clips // 10)
        for i, sf in enumerate(scene_files, 1):
            if not await _clip_ok(sf):
                corrupt_clips.append(sf)
            if i % report_every == 0 or i == total_clips:
                yield f"  [{i}/{total_clips}] checked, {len(corrupt_clips)} corrupt so far"
        if not corrupt_clips:
            yield f"  ✓ All {total_clips} clips OK"
            _val_cache.write_text(str(total_clips))
        else:
            yield f"  {len(corrupt_clips)} corrupt clip(s) — re-encoding..."
            for sf in corrupt_clips:
                yield await _reencode_from_source(sf)
            scene_files = sorted((auto_dir / "autocut").glob("*.mp4"))

    # ── [4/6] Frame extraction (parallel, batched) ───────────────────────────
    yield ""
    yield "[4/6] Extracting key frames..."

    async def _extract_frame(sf: Path) -> None:
        out_jpg = auto_dir / "frames" / f"{sf.stem}.jpg"
        if out_jpg.exists() or sf.stat().st_size < 5_000_000:
            return
        dur = await _probe_duration(sf, ffprobe)
        if not dur:
            return
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, *hwaccel, "-ss", f"{dur / 2:.3f}", "-i", str(sf),
            "-vframes", "1", "-q:v", "2", "-update", "1",
            str(out_jpg), "-y", "-loglevel", "quiet",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    batch_size = os.cpu_count() or 4
    for i in range(0, len(scene_files), batch_size):
        await asyncio.gather(*[_extract_frame(sf) for sf in scene_files[i:i + batch_size]])

    frame_count = len(list((auto_dir / "frames").glob("*.jpg")))
    yield f"  Frames: {frame_count}"
    if frame_count == 0:
        raise RuntimeError("No frames extracted. All scenes may be < 5MB or unreadable.")

    # ── [5/6] CLIP scoring ────────────────────────────────────────────────────
    yield ""
    yield "[5/6] CLIP scoring..."

    scores_csv = auto_dir / "scene_scores.csv"
    _safe_env = {k: v for k, v in os.environ.items()
                 if k not in ("ANTHROPIC_API_KEY", "LAST_FM_API_KEY")}
    if scores_csv.exists():
        try:
            _check_df = pd.read_csv(scores_csv)
            _nan_count = int(_check_df["score"].isna().sum())
            if _nan_count:
                scores_csv.unlink()
                yield f"  {_nan_count} scene(s) with missing scores — rescoring..."
            else:
                yield f"  Cached (delete {scores_csv.name} to rescore)"
        except Exception:
            scores_csv.unlink()
            yield "  Corrupt scores CSV — rescoring..."
    if not scores_csv.exists():
        clip_env = {
            **_safe_env,
            "FRAMES_DIR": str(auto_dir / "frames") + "/",
            "OUTPUT_CSV":  str(scores_csv),
        }
        clip_proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT_DIR / "clip_score.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_dir),
            env=clip_env,
        )
        async for raw in clip_proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                yield f"  {line}"
        await clip_proc.wait()
        if not scores_csv.exists():
            raise RuntimeError("CLIP scoring failed — no scene_scores.csv produced.")
        try:
            _new_df = pd.read_csv(scores_csv)
            if _new_df["score"].isna().all():
                raise RuntimeError(
                    "CLIP scoring produced all-NaN scores. "
                    "Check [clip_prompts] positive/negative in config.ini — both must be non-empty."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

    # ── Auto-threshold from top-10 (first analysis only, no user threshold set)
    if analyze_only and not params.get("threshold"):
        try:
            _scores_df = pd.read_csv(scores_csv).dropna(subset=["score"])
            _top10_min = float(_scores_df.nlargest(10, "score")["score"].min())
            _auto_threshold = round(_top10_min, 4)
            if threshold != _auto_threshold:
                yield f"  Auto-threshold: {_auto_threshold} (min of top 10; was {threshold})"
                threshold = _auto_threshold
        except Exception:
            pass

    # ── Write analyze_result.json ─────────────────────────────────────────────
    try:
        _ar_df = pd.read_csv(scores_csv).dropna(subset=["score"])
        _est_scenes = int((_ar_df["score"] >= threshold).sum())
        _avg_dur = 0.0
        _dur_count = 0
        for _csv_path in (auto_dir / "csv").glob("*-Scenes.csv"):
            try:
                _sdf = pd.read_csv(_csv_path, skiprows=1)
                for _, _row in _sdf.iterrows():
                    _d = float(_row.get("Length (seconds)", 0) or 0)
                    if _d > 0:
                        _avg_dur += _d
                        _dur_count += 1
            except Exception:
                pass
        _avg_dur = (_avg_dur / _dur_count) if _dur_count else max_scene * 0.6
        _est_dur = round(_est_scenes * min(_avg_dur, max_scene), 1)
        _ar = {
            "scene_count":          int(len(_ar_df)),
            "auto_threshold":       threshold,
            "estimated_scenes":     _est_scenes,
            "estimated_duration_sec": _est_dur,
        }
        (auto_dir / "analyze_result.json").write_text(json.dumps(_ar, indent=2))
    except Exception as _ar_err:
        yield f"  [warn] analyze_result.json not written: {_ar_err}"

    if analyze_only:
        yield ""
        yield f"✓ Analysis complete — {len(scene_files)} scenes, threshold {threshold}"
        return

    # ── [6/6] Scene selection ─────────────────────────────────────────────────
    yield ""
    yield "[6/6] Selecting scenes and building highlight..."

    sel_env = {
        **_safe_env,
        "SCENES_DIR":  str(auto_dir / "autocut") + "/",
        "TRIMMED_DIR": str(auto_dir / "trimmed") + "/",
        "OUTPUT_CSV":  str(scores_csv),
        "OUTPUT_LIST": str(auto_dir / "selected_scenes.txt"),
        "CAM_SOURCES":      str(auto_dir / "camera_sources.csv"),
        "AUDIO_CAM":        cam_a,
        "MANUAL_OVERRIDES": str(auto_dir / "manual_overrides.json"),
    }
    sel_proc = await asyncio.create_subprocess_exec(
        sys.executable, str(SCRIPT_DIR / "select_scenes.py"),
        str(threshold), str(max_scene), str(per_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(work_dir),
        env=sel_env,
    )
    async for raw in sel_proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            yield f"  {line}"
    await sel_proc.wait()

    selected_txt = auto_dir / "selected_scenes.txt"
    if not selected_txt.exists() or selected_txt.stat().st_size == 0:
        raise RuntimeError(
            f"No scenes selected. Try lowering threshold (current: {threshold})."
        )

    # ── Encode highlight.mp4 ──────────────────────────────────────────────────
    highlight = auto_dir / "highlight.mp4"

    concat_dur = 0.0
    with open(selected_txt) as fh:
        for line in fh:
            m = re.match(r"file '(.+)'", line.strip())
            if m:
                d = await _probe_duration(Path(m.group(1)), ffprobe)
                if d:
                    concat_dur += d

    yield f"  Encoding highlight ({concat_dur:.1f}s)..."

    enc_cmd = [
        ffmpeg, "-f", "concat", "-safe", "0",
        "-i", str(selected_txt),
        "-vf", (f"scale={resolution}:flags=lanczos:force_original_aspect_ratio=decrease,"
                f"pad={resolution}:(ow-iw)/2:(oh-ih)/2:color=black"),
        "-c:v", vid_codec, *vid_quality,
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-pix_fmt", "yuv420p", "-r", framerate, "-vsync", "cfr",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-loglevel", "error",
        str(highlight), "-y",
    ]
    enc_proc2 = await asyncio.create_subprocess_exec(
        *enc_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir),
    )
    total_s = concat_dur or 1.0
    async for raw in enc_proc2.stdout:
        k, _, v = raw.decode("utf-8", errors="replace").strip().partition("=")
        if k == "out_time_ms":
            try:
                cur = int(v) / 1_000_000
                pct = min(int(cur * 100 / total_s), 100)
                filled = pct // 2
                bar = "█" * filled + "░" * (50 - filled)
                yield f"\r  [{bar}] {pct:3d}%  {cur:.1f}/{total_s:.1f}s"
            except Exception:
                pass
    enc_stderr = await enc_proc2.stderr.read()
    await enc_proc2.wait()
    yield ""

    if not highlight.exists():
        err = enc_stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Encoding failed: {err[:300]}")

    hl_dur = await _probe_duration(highlight, ffprobe) or 0.0

    # Warn if output is significantly shorter than expected (truncated encode)
    if concat_dur > 5 and hl_dur < concat_dur * 0.95:
        gap = concat_dur - hl_dur
        err_txt = enc_stderr.decode("utf-8", errors="replace").strip()
        yield f"  ⚠ highlight.mp4 is {gap:.0f}s shorter than expected ({hl_dur:.0f}s / {concat_dur:.0f}s)"
        if err_txt:
            for ln in err_txt.splitlines()[-5:]:
                yield f"    ffmpeg: {ln}"

    # ── Intro / Outro ─────────────────────────────────────────────────────────
    final: Path | None = None
    if not no_intro:
        yield ""
        yield "Adding intro/outro..."

        df_scores = pd.read_csv(scores_csv)
        best_frame = auto_dir / "frames" / f"{df_scores.iloc[0]['scene']}.jpg"

        # Get video dimensions
        dim_proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0",
            str(highlight),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        dim_out, _ = await dim_proc.communicate()
        dim_parts = dim_out.decode().strip().split(",")
        width  = dim_parts[0].strip() if len(dim_parts) >= 2 else resolution.split(":")[0]
        height = dim_parts[1].strip() if len(dim_parts) >= 2 else resolution.split(":")[1]

        title_parts = title.split("\n")
        line1 = title_parts[0] if title_parts else ""
        line2 = " ".join(title_parts[1:]) if len(title_parts) > 1 else ""
        fade_out_st = float(intro_dur) - fade_dur

        intro_mp4 = auto_dir / "intro.mp4"
        outro_mp4 = auto_dir / "outro.mp4"
        hl_faded  = auto_dir / "highlight_faded.mp4"
        final     = auto_dir / "highlight_final.mp4"

        # Intro card
        vf_intro = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"drawtext=text='{line1}':fontfile={font}:fontsize={fsize_title}:"
            f"fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-80:"
            f"shadowcolor=black:shadowx=4:shadowy=4,"
            f"drawtext=text='{line2}':fontfile={font}:fontsize={fsize_sub}:"
            f"fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2+60:"
            f"shadowcolor=black:shadowx=4:shadowy=4,"
            f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={fade_out_st:.3f}:d={fade_dur}"
        )
        await _run([
            ffmpeg, "-loop", "1", "-i", str(best_frame),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", intro_dur, "-vf", vf_intro,
            "-map", "0:v", "-map", "1:a",
            "-c:v", vid_codec, *vid_quality, "-pix_fmt", "yuv420p", "-r", framerate,
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(intro_mp4), "-y", "-loglevel", "quiet",
        ])

        # Faded highlight
        fade_out_hl = hl_dur - fade_dur
        await _run([
            ffmpeg, "-i", str(highlight),
            "-vf", f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={fade_out_hl:.3f}:d={fade_dur}",
            "-c:v", vid_codec, *vid_quality, "-pix_fmt", "yuv420p", "-c:a", "copy",
            str(hl_faded), "-y", "-loglevel", "quiet",
        ])

        # Outro card
        vf_outro = (
            f"drawtext=text='{outro_text}':fontfile={font}:fontsize={fsize_outro}:"
            f"fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:"
            f"shadowcolor=gray:shadowx=2:shadowy=2,"
            f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={fade_out_st:.3f}:d={fade_dur}"
        )
        await _run([
            ffmpeg, "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:d={intro_dur}:r={framerate}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", vf_outro, "-map", "0:v", "-map", "1:a",
            "-c:v", vid_codec, *vid_quality, "-pix_fmt", "yuv420p", "-r", framerate,
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", intro_dur,
            str(outro_mp4), "-y", "-loglevel", "quiet",
        ])

        # Final concat
        concat_list = auto_dir / "final_concat.txt"
        concat_list.write_text(
            f"file '{intro_mp4}'\nfile '{hl_faded}'\nfile '{outro_mp4}'\n"
        )
        await _run([
            ffmpeg, "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", "-movflags", "+faststart",
            str(final), "-y", "-loglevel", "quiet",
        ])

        final_dur = await _probe_duration(final, ffprobe) or 0
        yield f"  highlight_final.mp4: {int(final_dur // 60)}m{int(final_dur % 60)}s"

    # ── Music mix ─────────────────────────────────────────────────────────────
    if not no_music and (music_dir.is_dir() or selected_track):
        yield ""
        yield "Adding music..."

        # Pinned track: skip all index/filter logic
        if selected_track:
            _st_path = Path(selected_track)
            if _st_path.exists():
                video_to_mix = final or highlight
                vid_dur      = await _probe_duration(video_to_mix, ffprobe) or 0
                yield f"  Track (pinned): {_st_path.stem}"
                output_music = _next_version(work_dir / "highlight_final_music.mp4")
                fade_start = vid_dur - music_fade
                await _run([
                    ffmpeg,
                    "-i", str(video_to_mix),
                    "-i", str(_st_path),
                    "-filter_complex",
                    f"[0:a]volume={orig_vol}[orig];"
                    f"[1:a]atrim=0:{vid_dur:.3f},"
                    f"afade=t=out:st={fade_start:.3f}:d={music_fade},"
                    f"volume={music_vol}[music];"
                    "[orig][music]amix=inputs=2:duration=first[aout]",
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(output_music), "-y", "-loglevel", "quiet",
                ])
                yield f"  → {output_music.name}"
            else:
                yield f"  ⚠ Pinned track not found: {selected_track}, skipping music"
        else:
            mp3_files = list(music_dir.glob("*.mp3")) + list(music_dir.glob("*.m4a"))
            if not mp3_files:
                yield f"  No MP3 files in {music_dir}, skipping music"
            else:
                music_index = music_dir / "index.json"
                indexed_count = 0
                if music_index.exists():
                    try:
                        indexed_count = len(json.loads(music_index.read_text()))
                    except Exception:
                        pass

                if not music_index.exists() or len(mp3_files) > indexed_count:
                    yield f"  Building music index ({len(mp3_files)} tracks)..."
                    _, idx_out = await _run([
                        sys.executable, str(SCRIPT_DIR / "music_index.py"),
                        str(music_dir), "--output", str(music_index),
                    ])
                    for ln in idx_out.splitlines():
                        if ln.strip():
                            yield f"  {ln}"
                else:
                    yield f"  Music index: {indexed_count} tracks (cached)"

                all_tracks: list[dict] = []
                if music_index.exists():
                    try:
                        all_tracks = json.loads(music_index.read_text())
                    except Exception:
                        pass

                if not all_tracks:
                    yield "  No tracks in index, skipping music"
                else:
                    avg_score     = float(pd.read_csv(scores_csv)["score"].mean())
                    energy_target = min(0.9, max(0.2, (avg_score - 0.14) * 10))

                    video_to_mix = final or highlight
                    vid_dur      = await _probe_duration(video_to_mix, ffprobe) or 0

                    tracks = list(all_tracks)
                    if music_genre:
                        f = [t for t in tracks if music_genre.lower() in t.get("genre", "").lower()]
                        if f:
                            tracks = f
                    if music_artist:
                        artists = [a.strip() for a in music_artist.split(",") if a.strip()]
                        f = [t for t in tracks if any(a in t.get("title", "").lower() for a in artists)]
                        if f:
                            tracks = f
                    if music_files_filter:
                        fset = set(str(p) for p in music_files_filter)
                        f = [t for t in tracks if t.get("file", "") in fset]
                        if f:
                            tracks = f

                    long_enough = [t for t in tracks if t["duration"] >= vid_dur]
                    if long_enough:
                        long_enough.sort(key=lambda t: (
                            t["duration"] - vid_dur,
                            abs(t.get("energy_norm", 0.5) - energy_target)
                        ))
                        best_track = random.choice(long_enough[:5])
                    elif tracks:
                        tracks.sort(key=lambda t: (
                            vid_dur - t["duration"],
                            abs(t.get("energy_norm", 0.5) - energy_target)
                        ))
                        best_track = random.choice(tracks[:5])
                    else:
                        best_track = None

                    if best_track is None:
                        yield "  Could not select track, skipping music"
                    else:
                        yield f"  Track: {Path(best_track['file']).stem}"
                        output_music = _next_version(work_dir / "highlight_final_music.mp4")
                        fade_start = vid_dur - music_fade
                        await _run([
                            ffmpeg,
                            "-i", str(video_to_mix),
                            "-i", best_track["file"],
                            "-filter_complex",
                            f"[0:a]volume={orig_vol}[orig];"
                            f"[1:a]atrim=0:{vid_dur:.3f},"
                            f"afade=t=out:st={fade_start:.3f}:d={music_fade},"
                            f"volume={music_vol}[music];"
                            "[orig][music]amix=inputs=2:duration=first[aout]",
                            "-map", "0:v", "-map", "[aout]",
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart",
                            str(output_music), "-y", "-loglevel", "quiet",
                        ])
                        yield f"  → {output_music.name}"

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed    = time.time() - t_start
    scene_count = sum(1 for _ in open(selected_txt))
    hl_min, hl_sec = int(hl_dur // 60), int(hl_dur % 60)
    el_min, el_sec = int(elapsed // 60), int(elapsed % 60)

    yield ""
    yield "✓ DONE"
