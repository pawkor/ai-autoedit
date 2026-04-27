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
import fcntl
import json
import os
import pandas as pd
import random
import re
import sys
import time
from pathlib import Path
from typing import AsyncIterator, Optional

_GPU_LOCK_FILE = Path("/tmp/ai-autoedit-gpu.lock")

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

async def _probe_fps(path: Path, ffprobe: str) -> float | None:
    """Return avg_frame_rate as float, falling back to r_frame_rate."""
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
        "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    for token in out.decode().split():
        try:
            parts = token.strip().split("/")
            val = float(parts[0]) / float(parts[1]) if len(parts) == 2 else float(parts[0])
            if 1.0 < val < 300.0:
                return round(val, 3)
        except Exception:
            continue
    return None


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


async def _probe_video_duration(path: Path, ffprobe: str) -> float | None:
    """Return video stream duration (not container duration).
    Falls back to container duration if video stream has no duration tag."""
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=duration",
        "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.strip())
    except Exception:
        return await _probe_duration(path, ffprobe)


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


# ── Output naming ─────────────────────────────────────────────────────────────

def _output_name(work_dir: Path) -> str:
    """Derive a human-friendly base name from work_dir.
    Strips BROWSE_ROOT prefix, then replaces '/' with '-'.
    E.g. /data/2025/04-Grecja/04.21 → '2025-04-Grecja-04.21'
    """
    browse_root = Path(os.environ.get("BROWSE_ROOT", str(Path.home())))
    try:
        rel = work_dir.resolve().relative_to(browse_root.resolve())
    except ValueError:
        rel = Path(work_dir.name)
    name = str(rel).replace("/", "-").strip("-")
    return name or work_dir.name


# ── Versioning helper ─────────────────────────────────────────────────────────

def _next_version(path: Path) -> Path:
    """Return path with -vNN suffix, e.g. highlight-v01.mp4."""
    parent, ext = path.parent, path.suffix
    base = re.sub(r'-v\d+$', '', path.stem)
    nums = [
        int(m.group(1))
        for f in parent.glob(f"{base}-v*{ext}")
        if (m := re.match(rf'^{re.escape(base)}-v(\d+)$', f.stem))
    ]
    return parent / f"{base}-v{max(nums, default=0) + 1:02d}{ext}"


# ── Post-processing (intro/outro + rename + preview) ─────────────────────────

async def apply_postprocess(
    work_dir:  Path,
    highlight: Path,
    params:    dict,
) -> AsyncIterator[str]:
    """
    Apply intro/outro, mix music (from music_info.json), rename to output name, generate preview.
    Yields log lines. Called after music_driven.py completes.
    highlight: path to the rendered video (video-only, no music yet).
    """
    cp       = _load_cfg(work_dir)
    auto_dir = work_dir / _s(cp, "paths", "work_subdir", "_autoframe")

    ffmpeg  = os.path.expanduser(_s(cp, "paths", "ffmpeg",  "ffmpeg"))
    ffprobe = os.path.expanduser(_s(cp, "paths", "ffprobe", "ffprobe"))
    font    = os.path.expanduser(_s(cp, "intro_outro", "font",
                                    "~/fonts/Caveat-Bold.ttf"))

    resolution   = _s(cp, "video", "resolution",  "3840:2160")
    framerate    = _s(cp, "video", "framerate",   "60")
    nvenc_cq     = _s(cp, "video", "nvenc_cq",    "18")
    nvenc_preset = _s(cp, "video", "nvenc_preset", "p5")
    x264_crf     = _s(cp, "video", "x264_crf",    "15")
    x264_preset  = _s(cp, "video", "x264_preset", "fast")

    intro_dur   = _s(cp, "intro_outro", "duration",          "3")
    fade_dur    = float(_s(cp, "intro_outro", "fade_duration", "1"))
    outro_text  = _s(cp, "intro_outro", "outro_text",        "Editing powered by AI")
    fsize_title = _s(cp, "intro_outro", "font_size_title",   "120")
    fsize_sub   = _s(cp, "intro_outro", "font_size_subtitle","96")
    fsize_outro = _s(cp, "intro_outro", "font_size_outro",   "60")

    # Detect NVENC
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

    no_intro = bool(params.get("no_intro", False))
    title    = str(params.get("title") or _output_name(work_dir))
    hl_dur   = await _probe_duration(highlight, ffprobe) or 0.0
    final: Path | None = None

    if not no_intro:
        yield ""
        yield "Adding intro/outro..."

        # Best frame from scores CSV
        _frames_dir = auto_dir / "frames"
        best_frame  = None
        for _csv in [auto_dir / "scene_scores.csv", auto_dir / "scene_scores_allcam.csv"]:
            if _csv.exists():
                try:
                    _df = pd.read_csv(_csv)
                    _best = _df.iloc[0]["scene"]
                    best_frame = next(
                        (p for p in [
                            _frames_dir / f"{_best}_f1.jpg",
                            _frames_dir / f"{_best}_f0.jpg",
                            _frames_dir / f"{_best}.jpg",
                        ] if p.exists()),
                        None
                    )
                    if best_frame:
                        break
                except Exception:
                    pass
        if best_frame is None:
            _all = sorted(_frames_dir.glob("*.jpg"))
            best_frame = _all[0] if _all else None

        if best_frame is None:
            yield "  ⚠ No frame for intro card — skipping intro/outro"
        else:
            # Try to extract a full-res frame from the original clip (autocut/) instead
            # of using the low-res 640px gallery thumbnail.
            _best_scene = None
            for _csv in [auto_dir / "scene_scores.csv", auto_dir / "scene_scores_allcam.csv"]:
                if _csv.exists():
                    try:
                        _best_scene = pd.read_csv(_csv).iloc[0]["scene"]
                        break
                    except Exception:
                        pass
            if _best_scene:
                _clip_file = auto_dir / "autocut" / f"{_best_scene}.mp4"
                if _clip_file.exists():
                    _hq_frame = auto_dir / "intro_hq_frame.jpg"
                    _probe = await asyncio.create_subprocess_exec(
                        ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(_clip_file),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    )
                    _pdur, _ = await _probe.communicate()
                    _cdur = float(_pdur.decode().strip() or "0")
                    if _cdur > 0:
                        _fproc = await asyncio.create_subprocess_exec(
                            ffmpeg, "-ss", f"{_cdur * 0.5:.3f}", "-i", str(_clip_file),
                            "-vframes", "1", "-q:v", "2", str(_hq_frame), "-y", "-loglevel", "quiet",
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        )
                        await _fproc.wait()
                        if _hq_frame.exists():
                            best_frame = _hq_frame
                            yield "  intro: using full-res frame from source clip"

            # Use configured resolution (music-driven clips are normalised to it)
            w, h = resolution.split(":")

            title_parts = title.split("\n")
            line1 = title_parts[0] if title_parts else ""
            line2 = " ".join(title_parts[1:]) if len(title_parts) > 1 else ""
            fade_out_st = float(intro_dur) - fade_dur

            intro_mp4 = auto_dir / "intro.mp4"
            outro_mp4 = auto_dir / "outro.mp4"
            hl_faded  = auto_dir / "highlight_faded.mp4"
            final     = auto_dir / "highlight_final.mp4"

            yield "  intro/outro [1/3] intro card..."
            vf_intro = (
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
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
                "-avoid_negative_ts", "make_zero",
                "-c:v", vid_codec, *vid_quality, "-pix_fmt", "yuv420p", "-r", framerate,
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-map_metadata", "-1",
                str(intro_mp4), "-y", "-loglevel", "quiet",
            ])

            yield "  intro/outro [2/3] fading highlight..."
            fade_out_hl = hl_dur - fade_dur
            await _run([
                ffmpeg, *hwaccel, "-i", str(highlight),
                "-vf", f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={fade_out_hl:.3f}:d={fade_dur}",
                "-avoid_negative_ts", "make_zero",
                "-c:v", vid_codec, *vid_quality, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-b:a", "192k", "-map_metadata", "-1",
                str(hl_faded), "-y", "-loglevel", "quiet",
            ])

            yield "  intro/outro [3/3] outro + merge..."
            vf_outro = (
                f"drawtext=text='{outro_text}':fontfile={font}:fontsize={fsize_outro}:"
                f"fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:"
                f"shadowcolor=gray:shadowx=2:shadowy=2,"
                f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={fade_out_st:.3f}:d={fade_dur}"
            )
            await _run([
                ffmpeg, "-f", "lavfi",
                "-i", f"color=c=black:s={w}x{h}:d={intro_dur}:r={framerate}",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-vf", vf_outro, "-map", "0:v", "-map", "1:a",
                "-avoid_negative_ts", "make_zero",
                "-c:v", vid_codec, *vid_quality, "-pix_fmt", "yuv420p", "-r", framerate,
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", intro_dur,
                "-map_metadata", "-1",
                str(outro_mp4), "-y", "-loglevel", "quiet",
            ])

            concat_list = auto_dir / "final_concat.txt"
            concat_list.write_text(
                f"file '{intro_mp4}'\nfile '{hl_faded}'\nfile '{outro_mp4}'\n"
            )
            await _run([
                ffmpeg, "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c", "copy", "-movflags", "+faststart",
                str(final), "-y", "-loglevel", "quiet",
            ])
            highlight.unlink(missing_ok=True)
            hl_faded.unlink(missing_ok=True)
            intro_mp4.unlink(missing_ok=True)
            outro_mp4.unlink(missing_ok=True)
            concat_list.unlink(missing_ok=True)

            final_dur = await _probe_duration(final, ffprobe) or 0
            yield f"  Final: {int(final_dur//60)}:{int(final_dur%60):02d} ({final_dur:.1f}s)"

    # Rename to output name (work_dir/YYYY-MM-Place-DD-vNN.mp4)
    src = final or highlight
    if src and src.exists():
        _music_info_path = auto_dir / "music_info.json"
        if _music_info_path.exists():
            try:
                _mi       = json.loads(_music_info_path.read_text())
                _mpath    = _mi.get("music_path", "")
                _mss      = float(_mi.get("music_ss", 0))
                _mvol     = float(_mi.get("music_vol", 0.7))
                
                # Load actual volume settings from config
                _orig_vol = _f(cp, "music", "original_volume", 0.25)
                # Ensure values are within sanity range
                _mvol     = max(0.0, min(2.0, _mvol))
                _orig_vol = max(0.0, min(2.0, _orig_vol))

                if _mpath and Path(_mpath).exists():
                    yield ""
                    yield f"Mixing music (music={_mvol:.2f}, cam={_orig_vol:.2f})..."
                    _vdur      = await _probe_duration(src, ffprobe) or 0.0
                    _outro_dur = float(intro_dur)
                    _fade_dur  = _f(cp, "music", "fade_out_duration", _outro_dur)
                    _fst       = max(0.0, _vdur - _outro_dur)
                    _mout      = src.with_name(src.stem + "_withmusic.mp4")

                    # amix filter: inputs are [0:a] (camera) and [1:a] (music)
                    # We apply volume to each before mixing.
                    _af_mix = (
                        f"[1:a]volume={_mvol},afade=t=out:st={_fst:.2f}:d={_fade_dur:.1f}[aout]"
                    )

                    _mix_ret, _mix_err = await _run([
                        ffmpeg, "-y",
                        "-i", str(src),
                        "-ss", str(_mss), "-t", str(_vdur + 0.5),
                        "-i", _mpath,
                        "-filter_complex", _af_mix,
                        "-map", "0:v", "-map", "[aout]",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                        "-t", str(_vdur),
                        str(_mout),
                    ])
                    if _mout.exists():
                        src.unlink(missing_ok=True)
                        src = _mout
                        _music_info_path.unlink(missing_ok=True)
                        yield f"  Music mixed: {_vdur:.1f}s, fade at {_fst:.1f}s"
                    else:
                        yield f"  ⚠ Music mix failed — continuing without music"
            except Exception as _me:
                yield f"  ⚠ Music mix error: {_me}"

        out_name = _output_name(work_dir)
        out_path = _next_version(work_dir / f"{out_name}.mp4")
        src.rename(out_path)
        yield f"  → {out_path.name}"

        # Detect output resolution to decide on upscale + preview
        _out_h = 0
        try:
            _ph = await asyncio.create_subprocess_exec(
                ffprobe, "-v", "quiet", "-show_entries", "stream=height",
                "-of", "csv=p=0", str(out_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            _ph_out, _ = await _ph.communicate()
            _out_h = int(next((l for l in _ph_out.decode().splitlines() if l.strip()), "0"))
        except Exception:
            pass

        # 4K upscale for YouTube: only when source < 4K and enabled in config
        _upscale_4k = _s(cp, "video", "upscale_4k", "true").lower() in ("1", "true", "yes")
        if _upscale_4k and 0 < _out_h < 2160:
            _yt_out = out_path.with_name(out_path.stem + "_yt4k.mp4")
            yield ""
            yield "Upscaling to 4K for YouTube..."
            # Use scale_cuda when NVENC available (stays on GPU), fall back to CPU scale
            if vid_codec == "h264_nvenc":
                _up_cmd = [
                    ffmpeg, "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                    "-i", str(out_path),
                    "-vf", "scale_cuda=3840:2160:interp_algo=lanczos",
                    "-c:v", "h264_nvenc", "-rc", "vbr", "-cq", nvenc_cq,
                    "-b:v", "0", "-preset", nvenc_preset,
                    "-c:a", "copy", "-movflags", "+faststart",
                    str(_yt_out), "-y",
                ]
            else:
                _up_cmd = [
                    ffmpeg, "-i", str(out_path),
                    "-vf", "scale=3840:2160:flags=lanczos",
                    "-c:v", "libx264", "-crf", x264_crf, "-preset", x264_preset,
                    "-c:a", "copy", "-movflags", "+faststart",
                    str(_yt_out), "-y",
                ]
            _ret, _ = await _run(_up_cmd)
            if _yt_out.exists():
                yield f"  ✓ {_yt_out.name}"
            else:
                yield f"  ⚠ 4K upscale failed (code {_ret})"

        # Preview: only needed for 4K output (1080p is already web-playable)
        if _out_h >= 2160:
            yield ""
            yield "Generating preview..."
            _prev_out = out_path.with_name(out_path.stem + "_preview.mp4")
            if vid_codec == "h264_nvenc":
                _prev_cmd = [
                    ffmpeg, *hwaccel, "-i", str(out_path),
                    "-vf", "scale=-2:1080",
                    "-c:v", "h264_nvenc", "-b:v", "15M", "-maxrate", "20M", "-bufsize", "30M",
                    "-preset", "p4", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(_prev_out), "-y",
                ]
            else:
                _prev_cmd = [
                    ffmpeg, "-i", str(out_path),
                    "-vf", "scale=-2:1080",
                    "-c:v", "libx264", "-crf", "20", "-preset", "fast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(_prev_out), "-y",
                ]
            _ret, _ = await _run(_prev_cmd)
            if _prev_out.exists():
                yield f"  ✓ {_prev_out.name}"
            else:
                yield f"  ⚠ Preview failed (code {_ret})"
    else:
        yield "  ⚠ No output file to rename"

    yield ""
    yield "✓ DONE"


# ── Main pipeline ─────────────────────────────────────────────────────────────

import csv as _csv_mod

def _back_cam_sources(cam_src_csv: Path, main_cam: str) -> set[str]:
    """Return set of source stems that belong to non-main cameras."""
    if not main_cam or not cam_src_csv.exists():
        return set()
    with open(cam_src_csv) as _f:
        return {r["source"] for r in _csv_mod.DictReader(_f)
                if r.get("camera") != main_cam}


def _proxy_path(sf: Path, work_dir: Path, auto_dir: Path, cameras: list) -> Path:
    """Return proxy file path for source file sf."""
    proxy_dir = auto_dir / "proxy"
    for cam in cameras:
        try:
            rel = sf.relative_to(work_dir / cam)
            return proxy_dir / cam / rel.with_suffix(".mp4")
        except ValueError:
            continue
    return proxy_dir / sf.with_suffix(".mp4").name


async def estimate(params: dict, work_dir: Path) -> dict:
    """
    Run select_scenes.py with DRY_RUN=1 and return
    { scenes, duration_sec, main_scenes, cam_ratio, threshold }.
    Updates analyze_result.json in the auto_dir.
    """
    cp          = _load_cfg(work_dir)
    threshold   = float(params.get("threshold") or _f(cp, "scene_selection", "threshold",        0.148))
    max_scene   = float(params.get("max_scene") or _f(cp, "scene_selection", "max_scene_sec",    10))
    per_file    = float(params.get("per_file")  or _f(cp, "scene_selection", "max_per_file_sec", 45))
    min_gap     = float(params.get("min_gap_sec") or _f(cp, "scene_selection", "min_gap_sec",    0))
    _raw_cams   = params.get("cameras") or []
    if isinstance(_raw_cams, str):
        _raw_cams = [c.strip() for c in _raw_cams.split(",") if c.strip()]
    if not _raw_cams:
        _ca = str(params.get("cam_a") or "")
        _cb = str(params.get("cam_b") or "")
        _raw_cams = [c for c in [_ca, _cb] if c]
    cam_a       = _raw_cams[0] if _raw_cams else ""
    work_subdir = _s(cp, "paths", "work_subdir", "_autoframe")
    auto_dir    = work_dir / work_subdir
    allcam_csv  = auto_dir / "scene_scores_allcam.csv"
    scores_csv  = allcam_csv if allcam_csv.exists() else auto_dir / "scene_scores.csv"

    if not scores_csv.exists():
        return {}

    safe_env = {k: v for k, v in os.environ.items()
                if k not in ("ANTHROPIC_API_KEY", "LAST_FM_API_KEY")}
    _cam_offsets = params.get("cam_offsets") or {}
    if isinstance(_cam_offsets, str):
        try:
            import json as _json_tmp; _cam_offsets = _json_tmp.loads(_cam_offsets)
        except Exception:
            _cam_offsets = {}
    dry_env = {
        **safe_env,
        "SCENES_DIR":        str(auto_dir / "autocut") + "/",
        "TRIMMED_DIR":       str(auto_dir / "trimmed") + "/",
        "OUTPUT_CSV":        str(scores_csv),
        "OUTPUT_LIST":       str(auto_dir / "selected_scenes.txt"),
        "CAM_SOURCES":       str(auto_dir / "camera_sources.csv"),
        "CSV_DIR":           str(auto_dir / "csv"),
        "AUDIO_CAM":         cam_a,
        "MANUAL_OVERRIDES":  str(auto_dir / "manual_overrides.json"),
        "EMBEDDINGS_FILE":   str(auto_dir / "scene_embeddings.npz"),
        "DUPLICATES_FILE":   str(auto_dir / "scene_duplicates.json"),
        "DRY_RUN":           "1",
        **({"MIN_GAP_SEC": str(min_gap)} if min_gap > 0 else {}),
        **({"CAM_OFFSETS": json.dumps(_cam_offsets)} if _cam_offsets else {}),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(SCRIPT_DIR / "select_scenes.py"),
        str(threshold), str(max_scene), str(per_file),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(work_dir), env=dry_env,
    )
    out, _ = await proc.communicate()
    lines = out.decode("utf-8", errors="replace").splitlines()

    if proc.returncode != 0:
        import logging as _log
        _log.warning("estimate() select_scenes.py exited %d:\n%s", proc.returncode, "\n".join(lines[-20:]))

    scenes, dur, main = None, None, None
    for ln in lines:
        m = re.search(r'^Selected:\s*(\d+)', ln)
        if m: scenes = int(m.group(1))
        m = re.search(r'^Total:.*\(([\d.]+)s\)', ln) or re.search(r'^Total:\s*([\d.]+)s', ln)
        if m: dur = float(m.group(1))
        m = re.search(r'Main cam \([^)]+\):\s*(\d+)\s*scenes', ln)
        if m: main = int(m.group(1))

    cam_ratio = round(scenes / main, 4) if (scenes and main and main > 0) else 1.0

    if not dur:
        import logging as _log
        _log.warning("estimate() returned 0 duration (threshold=%.4f). Output:\n%s",
                     threshold, "\n".join(lines[-30:]))

    result = {
        "scenes":       scenes or 0,
        "duration_sec": round(dur, 1) if dur is not None else 0,
        "main_scenes":  main if main is not None else (scenes or 0),
        "cam_ratio":    cam_ratio,
        "threshold":    threshold,
    }

    # Update analyze_result.json
    ar_path = auto_dir / "analyze_result.json"
    try:
        ar = json.loads(ar_path.read_text()) if ar_path.exists() else {}
        ar.update({
            "auto_threshold":         threshold,
            "estimated_scenes":       result["scenes"],
            "estimated_duration_sec": result["duration_sec"],
            "estimated_main_scenes":  result["main_scenes"],
            "cam_ratio":              cam_ratio,
        })
        ar_path.write_text(json.dumps(ar, indent=2))
    except Exception:
        pass

    return result


async def find_threshold_iter(params: dict, work_dir: Path, target_sec: float):
    """
    Async generator — yields progress dicts for each binary-search iteration,
    then a final dict with done=True.
    Each progress dict: {iteration, total, threshold, duration_sec, done=False}
    Final dict: {done=True, threshold, duration_sec, scenes, main_scenes, cam_ratio} or {done=True, error=...}
    """
    MAX_ITER = 12
    lo, hi = 0.0, 1.0
    best: dict | None = None
    best_diff = float("inf")

    for i in range(MAX_ITER):
        mid = round((lo + hi) / 2, 4)
        r = await estimate({**params, "threshold": mid}, work_dir)
        if not r:
            yield {"done": True, "error": "No scores CSV — run analysis first"}
            return
        dur = r.get("duration_sec", 0)
        if dur > 0:
            diff = abs(dur - target_sec)
            if diff < best_diff:
                best_diff = diff
                best = r
        yield {"iteration": i + 1, "total": MAX_ITER, "threshold": mid,
               "duration_sec": dur, "done": False}
        if dur > target_sec:
            lo = mid   # too long → raise threshold
        else:
            hi = mid   # too short → lower threshold

    if best:
        yield {**best, "done": True}
    else:
        yield {"done": True, "error": "No result"}


async def create_proxy(params: dict, work_dir: Path):
    """
    Async generator — creates 480p/20fps CFR proxy files for scene detection.
    Yields progress dicts: {done, total, current_file, finished, error?}
    Final dict: {done: True, finished, total}
    """
    cp          = _load_cfg(work_dir)
    ffmpeg      = os.path.expanduser(_s(cp, "paths", "ffmpeg", "ffmpeg"))
    work_subdir = _s(cp, "paths", "work_subdir", "_autoframe")
    auto_dir    = work_dir / work_subdir

    _raw_cams = params.get("cameras") or []
    if isinstance(_raw_cams, str):
        _raw_cams = [c.strip() for c in _raw_cams.split(",") if c.strip()]
    if not _raw_cams:
        _ca = str(params.get("cam_a") or "")
        _cb = str(params.get("cam_b") or "")
        _raw_cams = [c for c in [_ca, _cb] if c]
    cameras = _raw_cams

    def _is_source(f: Path) -> bool:
        n = f.name.lower()
        return n.endswith(".mp4") and not n.startswith("highlight") and not n.endswith(".lrv")

    # Build per-camera file lists
    if cameras:
        cam_files = {cam: sorted(f for f in (work_dir / cam).iterdir() if _is_source(f))
                     for cam in cameras}
    else:
        cam_files = {"": sorted(f for f in work_dir.iterdir() if _is_source(f))}

    source_files = [f for fs in cam_files.values() for f in fs]
    file_cam     = {f: cam for cam, fs in cam_files.items() for f in fs}

    total        = len(source_files)
    finished     = 0
    failed_files: list[str] = []
    cam_finished = {cam: 0 for cam in cam_files}
    cam_totals   = {cam: len(fs) for cam, fs in cam_files.items()}

    def _cams_st():
        return {cam: {"total": cam_totals[cam], "finished": cam_finished[cam]}
                for cam in cam_files}

    # Detect NVENC availability — prefer GPU for proxy encoding
    _use_nvenc = False
    _nvenc_check = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-encoders",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    _enc_out, _ = await _nvenc_check.communicate()
    if b"h264_nvenc" in _enc_out:
        _use_nvenc = True

    # Limit concurrent encodes: GPU=4 (NVENC sessions), CPU=half cores (cap 8)
    _max_parallel = min(12, max(1, (os.cpu_count() or 2) // 2))
    _enc_sem = asyncio.Semaphore(_max_parallel)

    queue: asyncio.Queue = asyncio.Queue()
    current_files: dict = {cam: "" for cam in cam_files}

    async def _encode_one(cam: str, sf: Path):
        nonlocal finished
        proxy     = _proxy_path(sf, work_dir, auto_dir, cameras)
        proxy_tmp = proxy.with_suffix(".mp4.tmp")

        if proxy.exists():
            finished += 1
            cam_finished[cam] += 1
            await queue.put({"cam": cam, "file": sf.name})
            return

        if proxy_tmp.exists():
            await queue.put({"cam": cam, "file": sf.name, "skipped": True})
            return

        proxy.parent.mkdir(parents=True, exist_ok=True)
        await queue.put({"cam": cam, "file": sf.name})

        async with _enc_sem:
            # Proxy is 480p — CPU libx264 ultrafast is faster than NVENC here
            # (NVENC bottlenecked by CPU decode/scale, PCI-E transfer overhead not worth it)
            enc_args = [
                "-y", "-i", str(sf),
                "-vf", "scale=-2:480,fps=20",
                "-c:v", "libx264", "-crf", "30", "-preset", "ultrafast",
                "-fps_mode", "cfr", "-an", "-f", "mp4",
            ]
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, *enc_args, str(proxy_tmp),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await proc.communicate()

        if proc.returncode == 0 and proxy_tmp.exists():
            proxy_tmp.rename(proxy)
            finished += 1
            cam_finished[cam] += 1
        else:
            proxy_tmp.unlink(missing_ok=True)
            err_msg = stderr_bytes.decode("utf-8", errors="replace").strip().splitlines()
            err_tail = " | ".join(err_msg[-3:]) if err_msg else f"rc={proc.returncode}"
            failed_files.append(sf.name)
            await queue.put({"cam": cam, "file": sf.name,
                             "error": f"ffmpeg failed for {sf.name}: {err_tail}"})

    async def _run_cam(cam: str, files: list):
        tasks = [asyncio.create_task(_encode_one(cam, sf)) for sf in files]
        await asyncio.gather(*tasks)
        await queue.put({"cam": cam, "done": True})

    cam_tasks = [asyncio.create_task(_run_cam(cam, files))
                 for cam, files in cam_files.items()]
    n_done = 0
    try:
        while n_done < len(cam_tasks):
            msg = await queue.get()
            if msg.get("done"):
                n_done += 1
                continue
            current_files[msg["cam"]] = msg.get("file", "")
            upd: dict = {
                "done": False, "total": total, "finished": finished,
                "current_cam": msg["cam"], "current_file": msg.get("file", ""),
                "current_files": dict(current_files), "cams": _cams_st(),
            }
            if "error" in msg:
                upd["error"] = msg["error"]
            yield upd
    except (asyncio.CancelledError, GeneratorExit):
        for t in cam_tasks:
            t.cancel()
        await asyncio.gather(*cam_tasks, return_exceptions=True)
        raise

    yield {"done": True, "total": total, "finished": finished, "cams": _cams_st(),
           **({"failed_files": failed_files} if failed_files else {})}


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
    min_gap_r   = float(params.get("min_gap_sec") or _f(cp, "scene_selection", "min_gap_sec",     0))
    no_intro    = bool(params.get("no_intro",  False))
    no_music    = bool(params.get("no_music",  False))
    clip_first  = bool(params.get("clip_first", True))
    # cameras: ordered list, first = audio cam; falls back to legacy cam_a/cam_b
    _raw_cams = params.get("cameras") or []
    if isinstance(_raw_cams, str):
        _raw_cams = [c.strip() for c in _raw_cams.split(",") if c.strip()]
    if not _raw_cams:
        _ca = str(params.get("cam_a") or "")
        _cb = str(params.get("cam_b") or "")
        _raw_cams = [c for c in [_ca, _cb] if c]
    cameras = _raw_cams
    cam_a   = cameras[0] if cameras else ""  # first cam = audio source
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
    sd_threshold = str(params.get("sd_threshold") or _s(cp, "scene_detection", "threshold",    "20"))
    _sdm_raw     = str(params.get("sd_min_scene")  or _s(cp, "scene_detection", "min_scene_len","8s"))
    sd_min_scene = _sdm_raw if _sdm_raw.endswith("s") else _sdm_raw + "s"

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
        yield f"[DBG] encoder: h264_nvenc  hwaccel: cuda  cq: {nvenc_cq}  preset: {nvenc_preset}"
    else:
        vid_codec   = "libx264"
        vid_quality = ["-crf", x264_crf, "-preset", x264_preset]
        hwaccel     = []
        yield f"[DBG] encoder: libx264  hwaccel: none  crf: {x264_crf}  preset: {x264_preset}"

    # ── Header ────────────────────────────────────────────────────────────────
    title_line1 = title.split("\n")[0]
    yield f"Threshold: {threshold}  Max scene: {max_scene}s  Per file: {per_file}s  Title: {title_line1}"
    yield ""

    # ── [1/6] Find source files ───────────────────────────────────────────────
    def _is_source(f: Path) -> bool:
        n = f.name.lower()
        return n.endswith(".mp4") and not n.startswith("highlight") and not n.endswith(".lrv")

    if cameras:
        source_files = sorted(
            f for cam in cameras
            for f in (work_dir / cam).iterdir()
            if _is_source(f)
        )
    else:
        source_files = sorted(f for f in work_dir.iterdir() if _is_source(f))

    yield f"[1/6] Found {len(source_files)} source files"

    if not source_files:
        raise RuntimeError(f"No MP4 files found in {work_dir}")

    if cameras:
        cam_csv = auto_dir / "camera_sources.csv"
        with open(cam_csv, "w") as fh:
            fh.write("source,camera\n")
            for sf in source_files:
                matched = next((cam for cam in cameras if f"/{cam}/" in str(sf)), cameras[-1])
                fh.write(f"{sf.stem},{matched}\n")
        yield f"  {len(cameras)}-cam: {' / '.join(cameras)}"

    # ── [2/6]–[5/6] Scene extraction + CLIP scoring ──────────────────────────
    if clip_first:
        # ── CLIP-first mode: dense scan → peaks → clip extraction ─────────────
        yield ""
        yield "[2/6] CLIP-first scan (interval={:.0f}s, clip={:.0f}s, gap={:.0f}s)...".format(
            float(params.get("clip_scan_interval") or 3),
            float(params.get("clip_scan_clip_dur") or 8),
            float(params.get("clip_scan_min_gap")  or 30),
        )
        # Clear stale clips and frames from previous runs (both scenedetect and old clip-first)
        _stale_clips  = (list((auto_dir / "autocut").glob("*-scene-*.mp4")) +
                         list((auto_dir / "autocut").glob("*-clip-*.mp4")))
        _stale_frames = list((auto_dir / "frames").glob("*.jpg"))
        if _stale_clips:
            for _sf in _stale_clips:
                _sf.unlink()
            yield f"  Cleared {len(_stale_clips)} old clip(s)"
        if _stale_frames:
            for _sf in _stale_frames:
                _sf.unlink()
            yield f"  Cleared {len(_stale_frames)} old frame(s)"
        _safe_env_cs = {k: v for k, v in os.environ.items()
                        if k not in ("ANTHROPIC_API_KEY", "LAST_FM_API_KEY")}
        _is_dual_cs = bool(cameras and len(cameras) > 1)
        scan_env = {
            **_safe_env_cs,
            "WORK_DIR":              str(work_dir),
            "AUTO_DIR":              str(auto_dir),
            "CAMERAS":               ",".join(cameras),
            "FFMPEG":                ffmpeg,
            "FFPROBE":               ffprobe,
            "OUTPUT_CSV":            str(auto_dir / "scene_scores.csv"),
            "OUTPUT_CSV_ALLCAM":     str(auto_dir / "scene_scores_allcam.csv"),
            "CAM_SOURCES":           str(auto_dir / "camera_sources.csv"),
            "AUDIO_CAM":             cam_a,
            "CLIP_SCAN_INTERVAL_SEC":str(params.get("clip_scan_interval") or 3),
            "CLIP_SCAN_CLIP_DUR_SEC":str(params.get("clip_scan_clip_dur")  or 8),
            "CLIP_SCAN_MIN_GAP_SEC": str(params.get("clip_scan_min_gap")   or 30),
            **({"CLIP_BATCH_SIZE":  str(params["batch_size"])}  if params.get("batch_size")  else {}),
            **({"CLIP_NUM_WORKERS": str(params["clip_workers"])} if params.get("clip_workers") else {}),
        }
        scan_proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT_DIR / "clip_scan.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_dir),
            env=scan_env,
        )
        async for raw in scan_proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                yield f"  {line}"
        await scan_proc.wait()
        scores_csv = auto_dir / "scene_scores.csv"
        if not scores_csv.exists():
            raise RuntimeError("CLIP scan failed — no scene_scores.csv produced.")
        scene_files = sorted((auto_dir / "autocut").glob("*.mp4"))
        yield f"  [3/6]–[5/6] skipped (CLIP-first mode)"
        yield f"  Clips: {len(scene_files)}  Scores: {scores_csv.name}"
        _cam_src_csv = auto_dir / "camera_sources.csv"
        import hashlib as _hashlib
        _cur_hash = _hashlib.sha256(
            (params.get("positive", "") + "\n---\n" + params.get("negative", "")).encode()
        ).hexdigest()
        (auto_dir / "scores_prompts.hash").write_text(_cur_hash)

    # ── [2/6] Scene detection (parallel) ─────────────────────────────────────
    if clip_first:
        yield ""
        yield "[2/6]–[5/6] skipped (CLIP-first mode)"
    if not clip_first:
        yield ""
        total_detect = len(source_files)
        yield f"[2/6] Scene detection ({total_detect} files)..."

    # Invalidate CSV cache when detection params change.
    # Normalise threshold to "22" not "22.0" so int/float variants compare equal.
    def _norm_detect_sig(s: str) -> str:
        try:
            t, m = s.split("|", 1)
            v = float(t)
            return f"{int(v) if v == int(v) else v}|{m}"
        except Exception:
            return s
    _detect_params_sig = f"{sd_threshold}|{sd_min_scene}"
    _detect_params_file = auto_dir / "csv" / ".detect_params"
    _csv_dir = auto_dir / "csv"
    _csv_dir.mkdir(parents=True, exist_ok=True)
    _stored_sig = _detect_params_file.read_text().strip() if _detect_params_file.exists() else None
    if not clip_first and _norm_detect_sig(_stored_sig or "") != _norm_detect_sig(_detect_params_sig):
        stale_csv     = list(_csv_dir.glob("*-Scenes.csv"))
        stale_clips   = list((auto_dir / "autocut").glob("*.mp4"))
        stale_frames  = list((auto_dir / "frames").glob("*.jpg"))
        stale_trimmed = list((auto_dir / "trimmed").glob("*.mp4"))
        for f in stale_csv + stale_clips + stale_frames + stale_trimmed:
            f.unlink()
        # Scores reference old clip filenames → must be regenerated too
        for stale_score in [auto_dir / "scene_scores.csv", auto_dir / "scene_scores_allcam.csv",
                             auto_dir / "scores_prompts.hash",
                             auto_dir / "duration_cache.json", auto_dir / "validation_ok.txt",
                             auto_dir / "scene_embeddings.npz", auto_dir / "scene_duplicates.json"]:
            stale_score.unlink(missing_ok=True)
        msg_parts = []
        if stale_csv:     msg_parts.append(f"{len(stale_csv)} CSV(s)")
        if stale_clips:   msg_parts.append(f"{len(stale_clips)} clip(s)")
        if stale_frames:   msg_parts.append(f"{len(stale_frames)} frame(s)")
        if stale_trimmed: msg_parts.append(f"{len(stale_trimmed)} trimmed(s)")
        if msg_parts:
            yield f"  ⚠ Detect params changed — cleared {', '.join(msg_parts)}"

    def _count_csv_scenes(p: Path) -> int:
        """Count scene data rows (lines starting with a digit — excludes all headers)."""
        try:
            with open(p) as fh:
                return sum(1 for ln in fh if ln.lstrip()[:1].isdigit())
        except Exception:
            return 0

    to_detect = []
    if not clip_first:
        for sf in source_files:
            csv = auto_dir / "csv" / f"{sf.stem}-Scenes.csv"
            if csv.exists():
                count = _count_csv_scenes(csv)
                yield f"  ✓ {sf.name} ({count} scenes, cached)"
            else:
                to_detect.append(sf)
    # clip_first: to_detect stays empty — clip_scan already extracted clips
    if to_detect:
        # ── Auto-calibrate detect threshold on one representative file ────────
        _sample = max(to_detect, key=lambda f: f.stat().st_size)
        yield f"  Calibrating threshold on {_sample.name}..."
        _cal_threshold = int(float(sd_threshold))
        import tempfile as _tempfile, csv as _csv_cal
        for _attempt in range(3):
            _proxy_s   = _proxy_path(_sample, work_dir, auto_dir, cameras)
            _detect_s  = str(_proxy_s) if _proxy_s.exists() else str(_sample)
            with _tempfile.TemporaryDirectory() as _tmpdir:
                _cp = await asyncio.create_subprocess_exec(
                    "scenedetect", "-i", _detect_s,
                    "detect-content", "--threshold", str(_cal_threshold),
                    "--min-scene-len", sd_min_scene,
                    "list-scenes", "-o", _tmpdir,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await _cp.wait()
                _cal_csvs = list(Path(_tmpdir).glob("*-Scenes.csv"))
                _cal_durs = []
                if _cal_csvs:
                    with open(_cal_csvs[0]) as _fh:
                        _lines = _fh.readlines()
                    for _row in _csv_cal.DictReader(_lines[1:]):
                        try:
                            _cal_durs.append(float(_row["Length (seconds)"]))
                        except (KeyError, ValueError):
                            pass
            if not _cal_durs:
                break
            _micro_pct = sum(1 for d in _cal_durs if d < 5) / len(_cal_durs) * 100
            if _micro_pct > 20:
                _new = min(_cal_threshold + 8, 60)
                yield f"  {_micro_pct:.0f}% micro-scenes → threshold {_cal_threshold} → {_new}"
                _cal_threshold = _new
            else:
                break
        _cal_str = str(_cal_threshold)
        if _cal_str != sd_threshold:
            yield f"  ✓ Calibrated: {sd_threshold} → {_cal_str}  (median={sorted(_cal_durs)[len(_cal_durs)//2]:.1f}s)"
            sd_threshold = _cal_str
            _detect_params_sig = f"{sd_threshold}|{sd_min_scene}"
        else:
            yield f"  ✓ Threshold {sd_threshold} OK  (median={sorted(_cal_durs)[len(_cal_durs)//2]:.1f}s, {len(_cal_durs)} scenes)"

        workers = min(len(to_detect), int(params.get("max_detect_workers") or os.cpu_count() or 4))
        yield f"  Running {len(to_detect)} files in parallel (workers={workers})..."
        sem = asyncio.Semaphore(workers)
        completed = asyncio.Queue()

        async def _detect_one(sf):
            async with sem:
                proxy      = _proxy_path(sf, work_dir, auto_dir, cameras)
                proxy_tmp  = proxy.with_suffix(".mp4.tmp")
                # Wait for in-progress proxy (poll every 2s, up to 30 min)
                for _ in range(900):
                    if proxy.exists():
                        break
                    if not proxy_tmp.exists():
                        break  # proxy not started — use original
                    await asyncio.sleep(2)
                detect_src = str(proxy) if proxy.exists() else str(sf)
                proc = await asyncio.create_subprocess_exec(
                    "scenedetect", "-i", detect_src,
                    "detect-content", "--threshold", sd_threshold,
                    "--min-scene-len", sd_min_scene,
                    "list-scenes", "-o", str(auto_dir / "csv"),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            # scenedetect names CSV after the input stem; if we used proxy,
            # rename to the original stem so the rest of pipeline finds it.
            if proxy.exists():
                proxy_csv = auto_dir / "csv" / f"{proxy.stem}-Scenes.csv"
                orig_csv  = auto_dir / "csv" / f"{sf.stem}-Scenes.csv"
                if proxy_csv.exists() and not orig_csv.exists():
                    proxy_csv.rename(orig_csv)
            csv = auto_dir / "csv" / f"{sf.stem}-Scenes.csv"
            count = max(0, sum(1 for _ in open(csv)) - 2) if csv.exists() else 0
            status = "✓" if csv.exists() else "✗"
            await completed.put(f"  {status} {sf.name}: {count} scenes")

        tasks = [asyncio.create_task(_detect_one(sf)) for sf in to_detect]
        for _ in range(len(to_detect)):
            yield await completed.get()
        await asyncio.gather(*tasks)

    if not clip_first:
        _detect_params_file.write_text(_detect_params_sig)

    # ── Scene detection stats + threshold hint ────────────────────────────────
    _all_csv = list((auto_dir / "csv").glob("*-Scenes.csv"))
    if _all_csv and not clip_first:
        import csv as _csv_stats
        _durations = []
        for _csv_path in _all_csv:
            try:
                with open(_csv_path) as _fh:
                    _lines = _fh.readlines()
                # First line is "Timecode List:,..."; real headers are on line 2
                for _row in _csv_stats.DictReader(_lines[1:]):
                    try:
                        _durations.append(float(_row["Length (seconds)"]))
                    except (KeyError, ValueError):
                        pass
            except Exception:
                pass
        if _durations:
            _durations.sort()
            _n = len(_durations)
            _median = _durations[_n // 2]
            _micro = sum(1 for d in _durations if d < 5)
            _micro_pct = round(_micro / _n * 100)
            yield f"  Scene stats: {_n} total  median={_median:.1f}s  min={_durations[0]:.1f}s  max={_durations[-1]:.1f}s"
            if _micro_pct > 20:
                _suggested = min(int(sd_threshold) + 8, 60)
                yield f"  ⚠ {_micro_pct}% scenes < 5s (vibration noise) — try raising Detect threshold to ~{_suggested}"
            elif _n < 50 and len(source_files) > 1:
                _suggested = max(int(sd_threshold) - 8, 5)
                yield f"  ⚠ Very few scenes ({_n}) — try lowering Detect threshold to ~{_suggested}"
            else:
                yield f"  ✓ Scene distribution looks healthy"

    # ── [3/6] Split scenes (parallel) ────────────────────────────────────────
    yield ""
    total_split = len(source_files)
    split_workers = min(total_split, int(params.get("max_detect_workers") or os.cpu_count() or 8))
    if not clip_first:
        yield f"[3/6] Splitting scenes... (workers={split_workers})"

    split_sem       = asyncio.Semaphore(split_workers)
    split_queue: asyncio.Queue = asyncio.Queue()
    split_finished  = 0

    async def _split_one(sf: Path):
        nonlocal split_finished
        csv_f    = auto_dir / "csv" / f"{sf.stem}-Scenes.csv"
        expected = _count_csv_scenes(csv_f) if csv_f.exists() else 0
        existing = len(list((auto_dir / "autocut").glob(f"{sf.stem}-scene-*.mp4")))
        if not csv_f.exists():
            split_finished += 1
            await split_queue.put({"file": sf.name, "done": False, "msg": f"✗ {sf.name}: no CSV, skipping"})
            return
        if existing >= expected > 0:
            split_finished += 1
            await split_queue.put({"file": sf.name, "done": False, "msg": f"✓ {sf.name} ({existing} scenes, cached)"})
            return
        async with split_sem:
            proc = await asyncio.create_subprocess_exec(
                "scenedetect", "-i", str(sf),
                "load-scenes", "-i", str(csv_f),
                "split-video", "-o", str(auto_dir / "autocut"),
                "--filename", f"{sf.stem}-scene-$SCENE_NUMBER",
                "--copy",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        done = len(list((auto_dir / "autocut").glob(f"{sf.stem}-scene-*.mp4")))
        split_finished += 1
        await split_queue.put({"file": sf.name, "done": False, "msg": f"✓ {sf.name} ({done} scenes)"})

    if not clip_first:
        split_tasks = [asyncio.create_task(_split_one(sf)) for sf in source_files]
        for _ in range(total_split):
            msg = await split_queue.get()
            yield f"  [{split_finished}/{total_split}] {msg['msg']}"
        await asyncio.gather(*split_tasks)

    if not clip_first:
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
            # Also remove corresponding frame files so next CLIP-first run regenerates them
            for _fsuf in ("_f0.jpg", "_f1.jpg", "_f2.jpg", ".jpg"):
                (auto_dir / "frames" / (mp4.stem + _fsuf)).unlink(missing_ok=True)
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
            ffmpeg, "-y", *hwaccel,
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
    # Ensure _safe_env is defined for post-scoring code regardless of mode
    _safe_env = {k: v for k, v in os.environ.items()
                 if k not in ("ANTHROPIC_API_KEY", "LAST_FM_API_KEY")}
    yield ""
    yield "[4/6] Extracting key frames..."
    if clip_first:
        yield "  Skipped (CLIP-first mode — peak frames extracted by clip_scan)"

    # Filter to main-cam scenes only (back cam is not scored, no point extracting frames)
    _cam_src_csv  = auto_dir / "camera_sources.csv"
    _back_srcs_fe = _back_cam_sources(_cam_src_csv, cam_a)
    if _back_srcs_fe:
        scene_files_main = [sf for sf in scene_files
                            if re.sub(r'-scene-\d+$', '', sf.stem) not in _back_srcs_fe]
        if len(scene_files_main) < len(scene_files):
            yield f"  Skipping {len(scene_files) - len(scene_files_main)} back-cam scenes"
    else:
        scene_files_main = scene_files

    # Remove stale frames from previous runs that no longer have a matching scene clip
    _valid_stems = {sf.stem for sf in scene_files_main}
    _stale = [p for p in (auto_dir / "frames").glob("*.jpg")
              if re.sub(r'_f\d+$', '', p.stem) not in _valid_stems]
    if _stale:
        for p in _stale:
            p.unlink(missing_ok=True)
        yield f"  Removed {len(_stale)} stale frame(s) from previous runs"

    # Upgrade old single-frame format (scene.jpg) to multi-frame (_f0/_f1/_f2)
    _old_format = [p for p in (auto_dir / "frames").glob("*.jpg")
                   if not re.search(r'_f\d+$', p.stem)]
    if _old_format:
        for p in _old_format:
            p.unlink(missing_ok=True)
        yield f"  Upgrading {len(_old_format)} frames → multi-frame format (re-extracting)"

    async def _extract_frame(sf: Path) -> None:
        if sf.stat().st_size < 5_000_000:
            return
        dur = await _probe_duration(sf, ffprobe)
        if not dur:
            return
        for fi, frac in enumerate([0.25, 0.50, 0.75]):
            out_jpg = auto_dir / "frames" / f"{sf.stem}_f{fi}.jpg"
            if out_jpg.exists():
                continue
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, *hwaccel, "-ss", f"{dur * frac:.3f}", "-i", str(sf),
                "-vframes", "1", "-vf", "scale=640:-2,crop=iw:ih*0.65:0:0", "-q:v", "4", "-update", "1",
                str(out_jpg), "-y", "-loglevel", "quiet",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

    if not clip_first:
        batch_size = os.cpu_count() or 4
        for i in range(0, len(scene_files_main), batch_size):
            await asyncio.gather(*[_extract_frame(sf) for sf in scene_files_main[i:i + batch_size]])

    _all_jpg = list((auto_dir / "frames").glob("*.jpg"))
    frame_count = len({re.sub(r'_f\d+$', '', p.stem) for p in _all_jpg})
    yield f"  Frames: {frame_count} scenes ({len(_all_jpg)} files)"
    if frame_count == 0:
        raise RuntimeError("No frames extracted. All scenes may be < 5MB or unreadable.")

    # ── [5/6] CLIP scoring ────────────────────────────────────────────────────
    yield ""
    yield "[5/6] CLIP scoring..."

    scores_csv   = auto_dir / "scene_scores.csv"
    prompts_hash_file = auto_dir / "scores_prompts.hash"
    _safe_env = {k: v for k, v in os.environ.items()
                 if k not in ("ANTHROPIC_API_KEY", "LAST_FM_API_KEY")}

    import hashlib as _hashlib, configparser as _hcfg
    _hconfig = _hcfg.ConfigParser()
    _hconfig.read(SCRIPT_DIR / "config.ini")
    _model_tag = (_hconfig.get("clip_scoring", "model",      fallback="ViT-L-14") + "/" +
                  _hconfig.get("clip_scoring", "pretrained", fallback="openai"))
    _cur_hash = _hashlib.sha256(
        (params.get("positive", "") + "\n---\n" + params.get("negative", "") +
         "\n---\n" + _model_tag).encode()
    ).hexdigest()

    if clip_first:
        _csv_count = len(pd.read_csv(scores_csv)) if scores_csv.exists() else 0
        yield f"  Cached ({_csv_count} scenes, from CLIP-first scan)"
    if not clip_first and scores_csv.exists():
        try:
            _check_df = pd.read_csv(scores_csv)
            _nan_count = int(_check_df["score"].isna().sum())
            _all_frames = list((auto_dir / "frames").glob("*.jpg"))
            # Count only main-cam frames (same filter as clip_score.py)
            _back_srcs = _back_cam_sources(_cam_src_csv, cam_a)
            if _back_srcs:
                _frame_count = len({
                    re.sub(r'_f\d+$', '', f.stem) for f in _all_frames
                    if re.sub(r'-scene-\d+$', '', re.sub(r'_f\d+$', '', f.stem)) not in _back_srcs
                })
            else:
                _frame_count = len({re.sub(r'_f\d+$', '', f.stem) for f in _all_frames})
            _csv_count   = len(_check_df)
            _saved_hash  = prompts_hash_file.read_text().strip() if prompts_hash_file.exists() else None
            if _nan_count:
                scores_csv.unlink()
                yield f"  {_nan_count} scene(s) with missing scores — rescoring..."
            elif _frame_count != _csv_count:
                scores_csv.unlink()
                yield f"  Frame count mismatch ({_frame_count} frames vs {_csv_count} scored) — rescoring..."
            elif _saved_hash != _cur_hash:
                scores_csv.unlink()
                yield "  Prompts changed — rescoring..."
            else:
                yield f"  Cached ({_csv_count} scenes)"
        except Exception:
            scores_csv.unlink()
            yield "  Corrupt scores CSV — rescoring..."
    if not clip_first and not scores_csv.exists():
        _is_dual = bool(cameras and len(cameras) > 1)
        _score_all = _is_dual or bool(params.get("score_all_cams"))
        clip_env = {
            **_safe_env,
            "FRAMES_DIR":       str(auto_dir / "frames") + "/",
            "OUTPUT_CSV":       str(scores_csv),
            "EMBEDDINGS_FILE":  str(auto_dir / "scene_embeddings.npz"),
            "CAM_SOURCES":      str(auto_dir / "camera_sources.csv"),
            "AUDIO_CAM":        cam_a,
            **({"SCORE_ALL_CAMS":    "1",
                "OUTPUT_CSV_ALLCAM": str(auto_dir / "scene_scores_allcam.csv")} if _score_all else {}),
            **({"CLIP_BATCH_SIZE":   str(params["batch_size"])}   if params.get("batch_size")   else {}),
            **({"CLIP_NUM_WORKERS":  str(params["clip_workers"])}  if params.get("clip_workers")  else {}),
        }
        _gpu_lock_fd = open(_GPU_LOCK_FILE, "w")
        try:
            yield "  Waiting for GPU..."
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: fcntl.flock(_gpu_lock_fd, fcntl.LOCK_EX))
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
        finally:
            fcntl.flock(_gpu_lock_fd, fcntl.LOCK_UN)
            _gpu_lock_fd.close()
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
        if not clip_first:
            prompts_hash_file.write_text(_cur_hash)

    # ── GPS annotation (optional, additive — skipped silently if no GPS data) ──
    _gps_detected = False
    try:
        from gps_index import build_gps_index, annotate_scores_csv as _gps_annotate
        _gps_exiftool = cp.get("paths", "exiftool", fallback="exiftool")
        _gps_index = build_gps_index(work_dir, exiftool=_gps_exiftool)
        if _gps_index:
            _allcam = auto_dir / "scene_scores_allcam.csv"
            _gps_csv = _allcam if _allcam.exists() else scores_csv
            _cam_offsets: dict[str, float] = {}
            if cp.has_section("cam_offsets"):
                for _k, _v in cp.items("cam_offsets"):
                    try: _cam_offsets[_k] = float(_v)
                    except ValueError: pass
            _gps_ok = _gps_annotate(
                _gps_csv, auto_dir / "autocut", _gps_index,
                ffprobe=ffprobe, cam_offsets=_cam_offsets,
            )
            if _gps_ok:
                try:
                    _gdf = pd.read_csv(_gps_csv)
                    if "gps_speed_max" in _gdf.columns:
                        _gps_detected = bool((_gdf["gps_speed_max"].fillna(0) > 0).any())
                except Exception:
                    pass
                yield "  GPS scores annotated"
                if _gps_detected and cp.getfloat("scene_selection", "gps_weight", fallback=0.0) == 0.0:
                    _GPS_AUTO_WEIGHT = 0.35
                    _local_cp = configparser.ConfigParser()
                    _local_cfg = work_dir / "config.ini"
                    if _local_cfg.exists():
                        _local_cp.read(str(_local_cfg))
                    if not _local_cp.has_section("scene_selection"):
                        _local_cp.add_section("scene_selection")
                    _local_cp.set("scene_selection", "gps_weight", str(_GPS_AUTO_WEIGHT))
                    with open(_local_cfg, "w") as _cfg_fh:
                        _local_cp.write(_cfg_fh)
                    yield f"  GPS auto-enabled: gps_weight={_GPS_AUTO_WEIGHT} saved to config"
    except Exception as _gps_err:
        yield f"  GPS annotation skipped: {_gps_err}"

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

    # ── Write analyze_result.json (dry-run select_scenes.py for exact numbers) ──
    try:
        _ar_df = pd.read_csv(scores_csv).dropna(subset=["score"])
        _dry_env = {
            **_safe_env,
            "SCENES_DIR":       str(auto_dir / "autocut") + "/",
            "TRIMMED_DIR":      str(auto_dir / "trimmed") + "/",
            "OUTPUT_CSV":        str(scores_csv),
            "OUTPUT_LIST":       str(auto_dir / "selected_scenes.txt"),
            "CAM_SOURCES":       str(auto_dir / "camera_sources.csv"),
            "CSV_DIR":           str(auto_dir / "csv"),
            "AUDIO_CAM":         cam_a,
            "MANUAL_OVERRIDES":  str(auto_dir / "manual_overrides.json"),
            "EMBEDDINGS_FILE":   str(auto_dir / "scene_embeddings.npz"),
            "DUPLICATES_FILE":   str(auto_dir / "scene_duplicates.json"),
            "DRY_RUN":           "1",
        }
        _dry_args = [str(threshold), str(max_scene), str(per_file)]
        _dry_proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT_DIR / "select_scenes.py"), *_dry_args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_dir), env=_dry_env,
        )
        _dry_out, _ = await _dry_proc.communicate()
        _dry_lines = _dry_out.decode("utf-8", errors="replace").splitlines()

        _est_scenes, _est_dur, _est_main = None, None, None
        for _ln in _dry_lines:
            _m = re.search(r'^Selected:\s*(\d+)', _ln)
            if _m:
                _est_scenes = int(_m.group(1))
            _m = re.search(r'^Total:.*\(([\d.]+)s\)', _ln) or re.search(r'^Total:\s*([\d.]+)s', _ln)
            if _m:
                _est_dur = float(_m.group(1))
            _m = re.search(r'Main cam \([^)]+\):\s*(\d+)\s*scenes', _ln)
            if _m:
                _est_main = int(_m.group(1))

        _cam_ratio = round(_est_scenes / _est_main, 4) \
            if (_est_scenes and _est_main and _est_main > 0) else 1.0

        _ar = {
            "scene_count":            int(len(_ar_df)),
            "auto_threshold":         threshold,
            "estimated_scenes":       _est_scenes if _est_scenes is not None else 0,
            "estimated_duration_sec": round(_est_dur, 1) if _est_dur is not None else 0,
            "estimated_main_scenes":  _est_main if _est_main is not None else (_est_scenes or 0),
            "cam_ratio":              _cam_ratio,
            "gps_detected":           _gps_detected,
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

    _cam_offsets_render = params.get("cam_offsets") or {}
    if isinstance(_cam_offsets_render, str):
        try:
            import json as _json_tmp2; _cam_offsets_render = _json_tmp2.loads(_cam_offsets_render)
        except Exception:
            _cam_offsets_render = {}
    sel_env = {
        **_safe_env,
        "SCENES_DIR":  str(auto_dir / "autocut") + "/",
        "TRIMMED_DIR": str(auto_dir / "trimmed") + "/",
        "OUTPUT_CSV":        str(scores_csv),
        "OUTPUT_LIST":       str(auto_dir / "selected_scenes.txt"),
        "CAM_SOURCES":       str(auto_dir / "camera_sources.csv"),
        "CSV_DIR":           str(auto_dir / "csv"),
        "AUDIO_CAM":         cam_a,
        "MANUAL_OVERRIDES":  str(auto_dir / "manual_overrides.json"),
        "EMBEDDINGS_FILE":   str(auto_dir / "scene_embeddings.npz"),
        "DUPLICATES_FILE":   str(auto_dir / "scene_duplicates.json"),
        **({"MIN_GAP_SEC": str(min_gap_r)} if min_gap_r > 0 else {}),
        **({"CAM_OFFSETS": json.dumps(_cam_offsets_render)} if _cam_offsets_render else {}),
        "TARGET_RESOLUTION": resolution,
        "TARGET_FRAMERATE":  framerate,
        "USE_NVENC": "1" if hwaccel else "0",
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
    yield f"[DBG] enc: {vid_codec}  {resolution}@{framerate}fps  audio: {audio_bitrate}  hwaccel: {'cuda' if hwaccel else 'none'}"

    enc_cmd = [
        ffmpeg, *hwaccel, "-f", "concat", "-safe", "0",
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
        err = enc_stderr.decode("utf-8", errors="replace").strip()
        for ln in err.splitlines():
            yield f"  [ffmpeg] {ln}"
        raise RuntimeError(f"Encoding failed: {err[:200] if err else 'no output'}")

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
        _best_scene = df_scores.iloc[0]['scene']
        _frames_dir = auto_dir / "frames"
        best_frame = next(
            (p for p in [
                _frames_dir / f"{_best_scene}_f1.jpg",
                _frames_dir / f"{_best_scene}_f0.jpg",
                _frames_dir / f"{_best_scene}.jpg",
            ] if p.exists()),
            _frames_dir / f"{_best_scene}_f1.jpg"  # fallback (ffmpeg will error clearly)
        )

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

        yield "  intro/outro [1/3] intro card..."
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

        yield "  intro/outro [2/3] fading highlight..."
        # Faded highlight
        fade_out_hl = hl_dur - fade_dur
        await _run([
            ffmpeg, *hwaccel, "-i", str(highlight),
            "-vf", f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={fade_out_hl:.3f}:d={fade_dur}",
            "-c:v", vid_codec, *vid_quality, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-b:a", "192k",
            str(hl_faded), "-y", "-loglevel", "quiet",
        ])

        yield "  intro/outro [3/3] outro + merge..."
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
        highlight.unlink(missing_ok=True)
        hl_faded.unlink(missing_ok=True)
        intro_mp4.unlink(missing_ok=True)
        outro_mp4.unlink(missing_ok=True)
        concat_list.unlink(missing_ok=True)

        final_dur = await _probe_duration(final, ffprobe) or 0
        yield f"  Final: {int(final_dur//60)}:{int(final_dur%60):02d} ({final_dur:.1f}s)"

    # ── Music mix ─────────────────────────────────────────────────────────────
    if not no_music and (music_dir.is_dir() or selected_track):
        yield ""
        yield "Adding music..."

        # Pinned track: skip all index/filter logic
        if selected_track:
            _st_path = Path(selected_track)
            if _st_path.exists():
                video_to_mix = final or highlight
                vid_dur      = await _probe_video_duration(video_to_mix, ffprobe) or 0
                yield f"  Track (pinned): {_st_path.stem}"
                yield f"[DBG] music: {_st_path}  fade: {music_fade}s  vol: orig={orig_vol} music={music_vol}"
                output_music = _next_version(work_dir / "highlight.mp4")
                fade_start = vid_dur - music_fade
                await _run([
                    ffmpeg,
                    "-i", str(video_to_mix),
                    "-i", str(_st_path),
                    "-filter_complex",
                    f"[1:a]atrim=0:{vid_dur:.3f},"
                    f"afade=t=out:st={fade_start:.3f}:d={music_fade},"
                    f"volume={music_vol}[aout]",
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(output_music), "-y", "-loglevel", "quiet",
                ])
                _om_dur = await _probe_duration(output_music, ffprobe) or 0
                yield f"  → {output_music.name}  {int(_om_dur//60)}:{int(_om_dur%60):02d}"
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
                    vid_dur      = await _probe_video_duration(video_to_mix, ffprobe) or 0

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
                        output_music = _next_version(work_dir / "highlight.mp4")
                        fade_start = vid_dur - music_fade
                        await _run([
                            ffmpeg,
                            "-i", str(video_to_mix),
                            "-i", best_track["file"],
                            "-filter_complex",
                            f"[1:a]atrim=0:{vid_dur:.3f},"
                            f"afade=t=out:st={fade_start:.3f}:d={music_fade},"
                            f"volume={music_vol}[aout]",
                            "-map", "0:v", "-map", "[aout]",
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart",
                            str(output_music), "-y", "-loglevel", "quiet",
                        ])
                        _om_dur = await _probe_duration(output_music, ffprobe) or 0
                        yield f"  → {output_music.name}  {int(_om_dur//60)}:{int(_om_dur%60):02d}"

    # Clean up highlight.mp4 if still present (no_intro path — music used it directly)
    highlight.unlink(missing_ok=True)

    # ── Preview ───────────────────────────────────────────────────────────────
    yield ""
    yield "Generating preview..."
    _prev_candidates = sorted(
        [p for p in work_dir.glob("highlight-v*.mp4") if "_preview" not in p.stem],
        key=lambda p: int(m.group(1)) if (m := re.search(r'_v(\d+)', p.stem)) else 0,
    )
    if _prev_candidates:
        _prev_src = _prev_candidates[-1]
        _prev_out = _prev_src.with_name(_prev_src.stem + "_preview.mp4")
        if _prev_out.exists():
            yield f"  ✓ {_prev_out.name} (cached)"
        else:
            if vid_codec == "h264_nvenc":
                _prev_cmd = [
                    ffmpeg, *hwaccel, "-i", str(_prev_src),
                    "-vf", "scale=-2:1080",
                    "-c:v", "h264_nvenc", "-b:v", "15M", "-maxrate", "20M", "-bufsize", "30M",
                    "-preset", "p4",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(_prev_out), "-y",
                ]
            else:
                _prev_cmd = [
                    ffmpeg, "-i", str(_prev_src),
                    "-vf", "scale=-2:1080",
                    "-c:v", "libx264", "-crf", "20", "-preset", "fast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(_prev_out), "-y",
                ]
            _prev_ret, _ = await _run(_prev_cmd)
            if _prev_out.exists():
                yield f"  ✓ {_prev_out.name}"
            else:
                yield f"  ⚠ Preview failed (code {_prev_ret})"
    else:
        yield "  ⚠ No output file found"

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed    = time.time() - t_start
    scene_count = sum(1 for _ in open(selected_txt))
    hl_min, hl_sec = int(hl_dur // 60), int(hl_dur % 60)
    el_min, el_sec = int(elapsed // 60), int(elapsed % 60)

    yield ""
    yield "✓ DONE"
