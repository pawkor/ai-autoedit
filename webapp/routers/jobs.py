"""Jobs routes: all /api/jobs/* routes plus helpers."""

import asyncio
import configparser
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from webapp.state import (
    APP_DIR,
    USER_DATA_DIR,
    SCRIPT_DIR,
    JOBS_DIR,
    BROWSE_ROOT,
    in_browse_root,
    jobs,
    job_semaphore,
    shorts_semaphore,
    _threshold_searches,
    _threshold_tasks,
    _LogList,
    Job,
    _run_job,
    _enqueue_job_task,
    wcfg,
    _prom_ok,
)

router = APIRouter()

# Import these at call-time to avoid circular imports at module load
# from webapp.routers.youtube import _read_yt_urls, _write_yt_url
# from webapp.routers.instagram import _read_ig_urls

# ── ACRCloud credentials (needed for _acr_preselect) ─────────────────────────
_ACR_HOST   = os.environ.get("ACRCLOUD_HOST", "")
_ACR_KEY    = os.environ.get("ACRCLOUD_ACCESS_KEY", "")
_ACR_SECRET = os.environ.get("ACRCLOUD_ACCESS_SECRET", "")


# ── Config helpers ────────────────────────────────────────────────────────────

_JOB_CONFIG_MAP = {
    "threshold":       ("scene_selection", "threshold"),
    "max_scene":       ("scene_selection", "max_scene_sec"),
    "per_file":        ("scene_selection", "max_per_file_sec"),
    "min_take":        ("scene_selection", "min_take_sec"),
    "min_gap_sec":     ("scene_selection", "min_gap_sec"),
    "sd_threshold":    ("scene_detection", "threshold"),
    "sd_min_scene":    ("scene_detection", "min_scene_len"),
    "target_minutes":  ("job", "target_minutes"),
    "cameras":      ("job", "cameras"),
    "cam_a":        ("job", "cam_a"),
    "cam_b":        ("job", "cam_b"),
    "title":        ("job", "title"),
    "no_intro":     ("job", "no_intro"),
    "no_music":     ("job", "no_music"),
    "music_genre":  ("job", "music_genre"),
    "music_artist": ("job", "music_artist"),
    "music_dir":      ("music", "dir"),
    "selected_track": ("music", "selected_track"),
    "positive":     ("clip_prompts", "positive"),
    "negative":     ("clip_prompts", "negative"),
    "yt_title":     ("youtube", "title"),
    "yt_desc":      ("youtube", "description"),
    "yt_notes":     ("youtube", "notes"),
    "shorts_text":         ("shorts",  "text_overlays"),
    "shorts_multicam":     ("shorts",  "multicam"),
    "shorts_ncs":          ("shorts",  "ncs_music"),
    "shorts_crop_offsets": ("shorts",  "crop_x_offsets"),
    "shorts_music_dir":    ("shorts",  "music_dir"),
    "clip_first":          ("clip_scan", "enabled"),
    "clip_scan_interval":  ("clip_scan", "interval_sec"),
    "clip_scan_clip_dur":  ("clip_scan", "clip_dur_sec"),
    "clip_scan_min_gap":   ("clip_scan", "min_gap_sec"),
    "beats_auto":          ("music_driven", "beats_auto"),
    "beats_method":        ("music_driven", "beats_method"),
    "beats_fast":          ("music_driven", "beats_fast"),
    "beats_mid":           ("music_driven", "beats_mid"),
    "beats_slow":          ("music_driven", "beats_slow"),
    "cam_pattern":         ("music_driven", "cam_pattern"),
    "gps_weight":               ("scene_selection", "gps_weight"),
    "gps_altitude_threshold_m": ("scene_selection", "gps_altitude_threshold_m"),
    "photos_dir":            ("photos", "dir"),
    "cc_brightness":         ("color_correct", "brightness"),
    "cc_gamma":              ("color_correct", "gamma"),
    "cc_contrast":           ("color_correct", "contrast"),
    "cc_saturation":         ("color_correct", "saturation"),
    "cc_temperature":        ("color_correct", "temperature"),
}


_DATA_DIR_PATH_FIELDS = {"music_dir", "photos_dir", "shorts_music_dir", "shorts_music_dirs"}


def _current_data_root() -> str:
    """Return active data root for $DATA_DIR expansion — /data (Docker) or wcfg value (macOS)."""
    _d = Path("/data")
    try:
        if _d.is_dir() and any(_d.iterdir()):
            return "/data"
    except PermissionError:
        pass
    stored = wcfg("data_root", "")
    return stored if stored and Path(stored).is_dir() else ""


def _expand_path(path: str) -> str:
    """Expand $DATA_DIR or /data/ prefix in a single path string."""
    if not path:
        return path
    root = _current_data_root()
    if not root:
        return path
    if "$DATA_DIR" in path:
        return path.replace("$DATA_DIR", root)
    if root != "/data" and path.startswith("/data/"):
        return root + path[5:]
    return path


def _expand_data_dir(result: dict) -> dict:
    """Replace $DATA_DIR placeholder and auto-map legacy /data/ prefix to current data root."""
    root = _current_data_root()
    if not root:
        return result
    for field in _DATA_DIR_PATH_FIELDS:
        v = result.get(field)
        if not isinstance(v, str) or not v:
            continue
        if "$DATA_DIR" in v:
            result[field] = v.replace("$DATA_DIR", root)
        elif root != "/data" and v.startswith("/data/"):
            result[field] = root + v[5:]  # /data/music → /Volumes/.../music
    return result


def _sanitize_ini(text: str) -> str:
    """Indent continuation lines so configparser doesn't choke on multiline values."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("[") and not stripped.startswith(";") and "=" not in stripped:
            line = "    " + stripped
        out.append(line)
    return "\n".join(out)


def read_job_config(work_dir: Path) -> dict:
    global_cp = configparser.ConfigParser()
    global_cp.read([str(APP_DIR / "config.ini"), str(USER_DATA_DIR / "global_config.ini")])

    local_cp = configparser.ConfigParser()
    cfg_path = work_dir / "config.ini"
    if cfg_path.exists():
        try:
            local_cp.read(str(cfg_path))
        except configparser.Error:
            local_cp.read_string(_sanitize_ini(cfg_path.read_text(encoding="utf-8", errors="replace")))

    result = {}
    for field, (section, key) in _JOB_CONFIG_MAP.items():
        for cp in (local_cp, global_cp):
            try:
                raw = cp.get(section, key)
                if not raw.strip():
                    continue
                if field in ("no_intro", "no_music", "clip_first", "beats_auto"):
                    result[field] = raw.strip().lower() in ("true", "1", "yes")
                elif field == "sd_min_scene":
                    v = float(raw.rstrip('s').strip())
                    result[field] = f"{int(v) if v == int(v) else v}s"
                elif field in ("threshold", "max_scene", "per_file", "target_minutes", "sd_threshold",
                               "clip_scan_interval", "clip_scan_clip_dur", "clip_scan_min_gap",
                               "beats_fast", "beats_mid", "beats_slow",
                               "gps_weight", "gps_altitude_threshold_m"):
                    result[field] = float(raw.rstrip('s').strip())
                elif field == "cameras":
                    result[field] = [c.strip() for c in raw.split(",") if c.strip()]
                else:
                    result[field] = raw.strip().replace("\\n", "\n")
                break
            except (configparser.NoSectionError, configparser.NoOptionError):
                continue
    if not result.get("cameras"):
        legacy = [c for c in [result.get("cam_a"), result.get("cam_b")] if c]
        if legacy:
            result["cameras"] = legacy
    for cp in (local_cp, global_cp):
        if cp.has_section("cam_offsets"):
            offsets = {k: float(v) for k, v in cp.items("cam_offsets") if v.strip()}
            if offsets:
                result["cam_offsets"] = offsets
            break
    for cp in (local_cp, global_cp):
        if cp.has_section("cam_crop_16x9"):
            crops = {k: v.strip() in ("1", "true", "yes") for k, v in cp.items("cam_crop_16x9") if v.strip()}
            if crops:
                result["cam_crop_16x9"] = crops
            break
    return _expand_data_dir(result)


def _read_scenes_csv(csv_path: Path) -> "pd.DataFrame":
    sdf = pd.read_csv(csv_path)
    if "Scene Number" not in sdf.columns:
        sdf = pd.read_csv(csv_path, skiprows=1)
    return sdf


def update_config_ini(cfg_path: Path, updates: dict[str, dict[str, str]]):
    content = cfg_path.read_text() if cfg_path.exists() else ""
    lines = content.splitlines()

    current_section = None
    section_end: dict[str, int] = {}
    key_line: dict[tuple, int] = {}

    for i, line in enumerate(lines):
        m = re.match(r'^\[(\w+)\]', line.strip())
        if m:
            current_section = m.group(1)
        elif current_section:
            km = re.match(r'^(\w+)\s*=', line.strip())
            if km:
                key_line[(current_section, km.group(1))] = i
                section_end[current_section] = i

    result = list(lines)
    appended: dict[str, list[str]] = {}

    for section, kvs in updates.items():
        for key, value in kvs.items():
            if (section, key) in key_line:
                result[key_line[(section, key)]] = f"{key} = {value}"
            else:
                appended.setdefault(section, []).append(f"{key} = {value}")

    for section, new_lines in appended.items():
        if section in section_end:
            insert_at = section_end[section] + 1
            for j, nl in enumerate(new_lines):
                result.insert(insert_at + j, nl)
        else:
            result.append("")
            result.append(f"[{section}]")
            result.extend(new_lines)

    cfg_path.write_text("\n".join(result) + "\n")


def save_job_config(work_dir: Path, params: dict):
    updates: dict[str, dict[str, str]] = {}
    for field, (section, key) in _JOB_CONFIG_MAP.items():
        if section == "clip_prompts":
            continue
        if field not in params:
            continue
        v = params.get(field)
        if v is None or v == "" or v == []:
            updates.setdefault(section, {})[key] = ""
            continue
        if isinstance(v, list):
            sv = ",".join(str(x) for x in v if x)
            if not sv:
                continue
        elif isinstance(v, bool):
            sv = str(v).lower()
        else:
            sv = str(v)
        sv = sv.replace("\n", "\\n")
        if field == "sd_min_scene":
            sv = sv.rstrip('s').strip() + 's'
        updates.setdefault(section, {})[key] = sv

    if updates:
        update_config_ini(work_dir / "config.ini", updates)

    cam_offsets = params.get("cam_offsets")
    if cam_offsets is not None and isinstance(cam_offsets, dict):
        offsets_update = {"cam_offsets": {k: str(int(v)) for k, v in cam_offsets.items() if v is not None}}
        update_config_ini(work_dir / "config.ini", offsets_update)

    cam_crop = params.get("cam_crop_16x9")
    if cam_crop is not None and isinstance(cam_crop, dict):
        crop_update = {"cam_crop_16x9": {k: ("1" if v else "0") for k, v in cam_crop.items()}}
        update_config_ini(work_dir / "config.ini", crop_update)


def save_prompts_to_config(cfg_path: Path, positive: str, negative: str):
    def fmt_multiline(text: str) -> str:
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        return "\n" + "\n".join(f"    {l}" for l in lines)

    new_section = (
        "[clip_prompts]\n"
        f"positive ={fmt_multiline(positive)}\n\n"
        f"negative ={fmt_multiline(negative)}\n"
    )

    if not cfg_path.exists():
        cfg_path.write_text(new_section)
        return

    content = cfg_path.read_text()
    cleaned = re.sub(r'\[clip_prompts\].*?(?=\n\[|\Z)', '', content, flags=re.DOTALL)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    cfg_path.write_text(new_section + ("\n\n" + cleaned if cleaned else "") + "\n")


def _resolve_params(d: dict, work_dir: Path) -> dict:
    cfg_chain = [read_job_config(work_dir)]
    global_cp = configparser.ConfigParser()
    global_cp.read(str(APP_DIR / "config.ini"))
    def _gf(section, key, fallback):
        try:    return float(global_cp.get(section, key))
        except: return fallback

    if d.get("threshold") is None:
        d["threshold"] = cfg_chain[0].get("threshold") or _gf("scene_selection", "threshold", 0.148)
    if d.get("max_scene") is None:
        d["max_scene"] = cfg_chain[0].get("max_scene") or _gf("scene_selection", "max_scene_sec", 10)
    if d.get("per_file") is None:
        d["per_file"]  = cfg_chain[0].get("per_file")  or _gf("scene_selection", "max_per_file_sec", 45)
    if d.get("min_gap_sec") is None:
        d["min_gap_sec"] = cfg_chain[0].get("min_gap_sec") or _gf("scene_selection", "min_gap_sec", 0)
    if d.get("sd_threshold") is None:
        d["sd_threshold"] = cfg_chain[0].get("sd_threshold") or _gf("scene_detection", "threshold", 20)
    if d.get("sd_min_scene") is None:
        raw = cfg_chain[0].get("sd_min_scene")
        if raw is None:
            try:
                gs = global_cp.get("scene_detection", "min_scene_len").rstrip('s').strip()
                v  = float(gs)
                raw = f"{int(v) if v == int(v) else v}s"
            except Exception:
                raw = None
        d["sd_min_scene"] = raw if raw else "10s"
    if not d.get("positive"):
        d["positive"] = cfg_chain[0].get("positive", "")
    if not d.get("negative"):
        d["negative"] = cfg_chain[0].get("negative", "")
    return d


def _validate_cameras(cameras, work_dir: Path) -> None:
    for cam in (cameras or []):
        if cam:
            resolved = (work_dir / cam).resolve()
            if not str(resolved).startswith(str(work_dir.resolve())):
                raise HTTPException(400, f"Invalid camera path: {cam}")


# ── Pydantic models ────────────────────────────────────────────────────────────

class JobParams(BaseModel):
    work_dir:     str
    threshold:    Optional[float] = None
    max_scene:    Optional[float] = None
    per_file:     Optional[float] = None
    title:        Optional[str]   = None
    cameras:      Optional[list[str]] = None
    cam_offsets:  Optional[dict] = None
    cam_a:        Optional[str]   = None
    cam_b:        Optional[str]   = None
    no_intro:     bool = False
    no_music:     bool = False
    music_genre:  Optional[str] = None
    music_artist: Optional[str] = None
    work_subdir:  str = "_autoframe"
    description:  Optional[str] = None
    positive:     Optional[str] = None
    negative:     Optional[str] = None
    sd_threshold:      Optional[float] = None
    sd_min_scene:      Optional[str]   = None
    clip_first:        bool             = True
    clip_scan_interval: Optional[float] = None
    clip_scan_clip_dur: Optional[float] = None
    clip_scan_min_gap:  Optional[float] = None
    score_all_cams:    bool             = True
    consensus_min:      Optional[int] = None


class RenderParams(BaseModel):
    selected_track: Optional[str] = None
    music_files: Optional[List[str]] = None
    threshold: Optional[float] = None
    max_scene: Optional[float] = None
    per_file: Optional[float] = None


# ── ACR pre-select helper ─────────────────────────────────────────────────────

async def _acr_preselect(job: Job) -> Optional[str]:
    """Pick a music track, checking ACRCloud for Content ID claims."""
    from webapp.routers.music import _acr_fingerprint
    import random as _random

    p = job.params
    _md = str(p.get("music_dir") or "")
    cp = configparser.ConfigParser()
    cp.read(Path(p["work_dir"]) / "config.ini")
    def _s(s, k, d=""): return cp.get(s, k, fallback=d)

    music_dir = Path(os.path.expanduser(_md)) if _md else \
                Path(os.path.expanduser(_s("music", "dir", "~/music")))
    if not music_dir.is_dir():
        return None

    index_path = music_dir / "index.json"
    if not index_path.exists():
        return None
    try:
        tracks = json.loads(index_path.read_text())
    except Exception:
        return None

    music_files_filter = p.get("music_files") or []
    if music_files_filter:
        fset = set(str(x) for x in music_files_filter)
        filtered = [t for t in tracks if t.get("file", "") in fset]
        if filtered:
            tracks = filtered

    if not tracks:
        return None

    energy_target = 0.6
    long_enough = [t for t in tracks if t.get("duration", 0) >= 180]
    pool = long_enough or tracks
    pool = sorted(pool, key=lambda t: abs(t.get("energy_norm", 0.5) - energy_target))
    candidates = pool[:max(5, len(pool))]
    _random.shuffle(candidates)

    tried = set()
    for t in candidates:
        fp = t.get("file", "")
        if not fp or fp in tried:
            continue
        tried.add(fp)
        path = Path(fp)
        if not path.exists():
            continue

        name = path.stem
        job.log.append(f"  ACR check: {name} …")
        await job.broadcast({"type": "log", "line": f"  ACR check: {name} …"})
        try:
            res = await _acr_fingerprint(path)
        except RuntimeError as e:
            job.log.append(f"  ACR error: {e} — skipping check, using track")
            await job.broadcast({"type": "log", "line": f"  ACR error: {e}"})
            return fp

        if res.get("matched") and res.get("blocked"):
            owners = ", ".join(r.get("rights_owner_name", "?") for r in res.get("rights", []) if r.get("rights_owner_name"))
            msg = f"  ⚠ Claimed by {owners} — skipping {name}"
            job.log.append(msg)
            await job.broadcast({"type": "log", "line": msg})
            continue

        status = "✓ No match" if not res.get("matched") else f"✓ Free ({res.get('artists')} — {res.get('title')})"
        msg = f"  ACR: {status} → using {name}"
        job.log.append(msg)
        await job.broadcast({"type": "log", "line": msg})
        return fp

    job.log.append("  ACR: all candidates claimed or unavailable — proceeding without pinned track")
    await job.broadcast({"type": "log", "line": "  ACR: all candidates claimed — no music"})
    return None


# ── Shorts helpers ────────────────────────────────────────────────────────────

async def _run_one_short(job: Job, idx: int, total: int, version: str = "") -> bool:
    import pipeline as _pipeline
    prefix = f"[{idx}/{total}] " if total > 1 else ""
    try:
        cmd = [sys.executable, str(SCRIPT_DIR / "make_shorts.py"), job.params["work_dir"]]
        if job.params.get("shorts_text"):      cmd.append("--text")
        if job.params.get("shorts_multicam"):  cmd.append("--multicam")
        if job.params.get("shorts_ncs"):       cmd.append("--ncs")
        if job.params.get("shorts_best"):      cmd.append("--best")
        if job.params.get("shorts_beat_sync"): cmd.append("--beat-sync")
        if job.params.get("shorts_duration"):  cmd += ["--duration", str(job.params["shorts_duration"])]
        _smds = job.params.get("shorts_music_dirs") or []
        if isinstance(_smds, str): _smds = [s.strip() for s in _smds.split(",") if s.strip()]
        if not _smds:
            _smd = job.params.get("shorts_music_dir", "").strip()
            if _smd: _smds = [_smd]
        for _d in _smds:
            if _d.strip(): cmd += ["--music-dir", _d.strip()]
        if version:                          cmd += ["--version", version]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(SCRIPT_DIR),
        )
        if total == 1:
            job.process = proc
        async for raw in proc.stdout:
            for part in raw.decode("utf-8", errors="replace").split("\r"):
                line = part.rstrip("\n").rstrip()
                if line:
                    is_progress = bool(re.search(r'^\s*\d+%\||\[[\u2588\u2591 ]+\]\s*\d+%|\b\d+%\|', line))
                    out = f"{prefix}{line}" if prefix else line
                    if not is_progress:
                        job.log.append(out)
                    await job.broadcast({"type": "log", "line": out})
        await proc.wait()
        return proc.returncode == 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"{prefix}ERROR: {exc}"
        job.log.append(msg)
        await job.broadcast({"type": "log", "line": msg})
        return False


async def _run_shorts(job: Job, count: int = 1, parallel: bool = False):
    """Run short video generation.
    parallel=True: main render is active — don't touch job.status/phase, use shorts_running flag.
    parallel=False: standalone — own job.status/phase lifecycle.
    Per-job _shorts_lock serialises back-to-back calls (queue instead of reject).
    """
    import pipeline as _pipeline
    async with job._shorts_lock:
        job.shorts_running = True
        if not parallel:
            job.status = "running"
            job.phase  = "shorts"
            await job.broadcast({"type": "status", "status": "running", "phase": "shorts"})
        await job.broadcast({"type": "shorts_status", "running": True})
        job.save()
        ok = False
        try:
            work_dir = Path(job.params["work_dir"])
            existing_nums = [
                int(m.group(1))
                for p in work_dir.glob("short-v*.mp4")
                if (m := re.search(r"^short-v(\d+)\.mp4$", p.name))
            ]
            next_num  = max(existing_nums, default=0) + 1
            versions  = [f"v{next_num + i:02d}" for i in range(count)]

            if count == 1:
                async with shorts_semaphore:
                    ok = await _run_one_short(job, 1, 1, versions[0])
            else:
                max_c   = max(1, int(wcfg("max_concurrent_jobs", "1")))
                batch_sem = asyncio.Semaphore(max_c)
                done_count = 0
                batch_lock = asyncio.Lock()

                async def _one(i: int) -> bool:
                    nonlocal done_count
                    async with batch_sem:
                        async with shorts_semaphore:
                            result = await _run_one_short(job, i, count, versions[i - 1])
                    async with batch_lock:
                        done_count += 1
                        pct = round(done_count / count * 100)
                        await job.broadcast({
                            "type":  "shorts_batch_progress",
                            "done":  done_count,
                            "total": count,
                            "pct":   pct,
                        })
                    return result

                results = await asyncio.gather(*[_one(i + 1) for i in range(count)], return_exceptions=True)
                ok = all(r is True for r in results)

            if not parallel:
                if ok:
                    job.status = "done"
                    job.phase  = "done"
                else:
                    job.log.append("ERROR: one or more shorts failed")
                    job.status = "failed"
                    job.phase  = "failed"
        except asyncio.CancelledError:
            job.log.append("[shorts cancelled]")
            if not parallel:
                job.status = "killed"
                job.phase  = "failed"
        except Exception as exc:
            job.log.append(f"ERROR: {exc}")
            if not parallel:
                job.status = "failed"
                job.phase  = "failed"
        finally:
            job.shorts_running = False
        if not parallel:
            job.ended_at = time.time()
        if _prom_ok:
            from webapp.state import _prom_jobs_total, _prom_job_duration
            _prom_jobs_total.labels(phase="shorts", status="done" if ok else "failed").inc()
            if not parallel:
                _prom_job_duration.labels(phase="shorts").observe(time.time() - job.started_at)
        job.save()
        shorts_status = "done" if ok else "failed"
        await job.broadcast({"type": "shorts_status", "running": False, "status": shorts_status})
        if not parallel:
            await job.broadcast({"type": "status", "status": job.status, "phase": job.phase})


# ── Result helpers ─────────────────────────────────────────────────────────────

def _probe_duration(path: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        val = r.stdout.strip()
        return round(float(val), 1) if val else None
    except Exception:
        return None


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/api/jobs/import")
async def import_job(data: dict):
    work_dir = Path(data.get("work_dir", "")).resolve()
    if not in_browse_root(work_dir):
        raise HTTPException(403)
    if not work_dir.is_dir():
        raise HTTPException(400, "work_dir not found")

    for job in jobs.values():
        if Path(job.params.get("work_dir", "")).resolve() == work_dir:
            return {"id": job.id}

    params = read_job_config(work_dir)
    params["work_dir"] = str(work_dir)
    params.setdefault("work_subdir", "_autoframe")

    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id, params)
    job.status = "done"
    job.log = _LogList(JOBS_DIR / f"{job_id}.log", ["[imported from existing files]"])
    mp4s = list(work_dir.glob("*-v*.mp4")) + list(work_dir.glob("*_v*.mp4"))
    if mp4s:
        job.started_at = min(p.stat().st_mtime for p in mp4s)
        job.ended_at   = max(p.stat().st_mtime for p in mp4s)
    jobs[job_id] = job
    job.save()
    return {"id": job_id}


@router.post("/api/jobs/scan-root")
async def scan_root():
    """Scan DATA_ROOT for processed projects (dirs with _autoframe/) and import them."""
    import webapp.state as _state
    root = _state.DATA_ROOT
    if not root or not root.is_dir():
        raise HTTPException(400, "data_root not configured or not a directory")

    existing_dirs = {
        Path(j.params.get("work_dir", "")).resolve()
        for j in jobs.values()
    }

    imported = 0
    for autoframe_dir in sorted(root.rglob("_autoframe")):
        if not autoframe_dir.is_dir():
            continue
        work_dir = autoframe_dir.parent.resolve()
        if not in_browse_root(work_dir):
            continue
        if work_dir in existing_dirs:
            continue
        params = read_job_config(work_dir)
        params["work_dir"] = str(work_dir)
        params.setdefault("work_subdir", "_autoframe")
        job_id = str(uuid.uuid4())[:8]
        job = Job(job_id, params)
        job.status = "done"
        job.log = _LogList(JOBS_DIR / f"{job_id}.log", ["[scanned from data root]"])
        mp4s = list(work_dir.glob("*-v*.mp4")) + list(work_dir.glob("*_v*.mp4"))
        if mp4s:
            job.started_at = min(p.stat().st_mtime for p in mp4s)
            job.ended_at   = max(p.stat().st_mtime for p in mp4s)
        jobs[job_id] = job
        job.save()
        existing_dirs.add(work_dir)
        imported += 1

    return {"imported": imported}


@router.post("/api/jobs")
async def create_job(params: JobParams, analyze_only: bool = Query(default=True), draft: bool = Query(default=False)):
    work_dir = Path(params.work_dir).resolve()
    if not work_dir.is_dir():
        raise HTTPException(400, f"Directory not found: {work_dir}")
    _validate_cameras(params.cameras, work_dir)
    _validate_cameras([params.cam_a, params.cam_b], work_dir)

    d = _resolve_params(params.model_dump(), work_dir)
    d["work_dir"] = str(work_dir)

    save_job_config(work_dir, d)
    if params.positive or params.negative:
        save_prompts_to_config(work_dir / "config.ini", params.positive or "", params.negative or "")

    existing_idle = next(
        (j for j in jobs.values() if j.params.get("work_dir") == str(work_dir) and j.status == "idle"),
        None,
    )
    if existing_idle:
        if draft:
            return {"id": existing_idle.id}
        # Preserve music + color-correction params — analyze modal doesn't send them,
        # so a plain replace would wipe selected_track, music_files, cc_* etc.
        _preserve_keys = (
            "selected_track", "music_file", "music_files",
            "cc_brightness", "cc_gamma", "cc_contrast", "cc_saturation", "cc_temperature",
        )
        for _k in _preserve_keys:
            if not d.get(_k) and existing_idle.params.get(_k):
                d[_k] = existing_idle.params[_k]
        existing_idle.params      = d
        existing_idle.log         = _LogList(JOBS_DIR / f"{existing_idle.id}.log")
        existing_idle.status      = "queued"
        existing_idle.phase       = "analyzing" if analyze_only else "rendering"
        existing_idle.started_at  = time.time()
        existing_idle.ended_at    = None
        existing_idle.analyze_result = None
        existing_idle._task       = None
        existing_idle.save()
        existing_idle._task = asyncio.create_task(_run_job(existing_idle, analyze_only=analyze_only))
        return {"id": existing_idle.id}

    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id, d)
    if draft:
        job.status = "idle"
        job.phase  = "new"
    else:
        job.phase = "analyzing" if analyze_only else "rendering"
    jobs[job_id] = job
    job.save()

    if not draft:
        job._task = asyncio.create_task(_run_job(job, analyze_only=analyze_only))
    return {"id": job_id}


@router.get("/api/jobs")
async def list_jobs():
    return [
        {
            "id":             j.id,
            "status":         j.status,
            "phase":          j.phase,
            "work_dir":       j.params["work_dir"],
            "started_at":     j.started_at,
            "ended_at":       j.ended_at,
            "progress":       j.progress,
            "progress_label": j.progress_label,
        }
        for j in sorted(jobs.values(), key=lambda j: -j.created_at)
    ]


@router.post("/api/jobs/{job_id}/rerun")
async def rerun_job(job_id: str, params: JobParams):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    work_dir = Path(params.work_dir).resolve()
    if not work_dir.is_dir():
        raise HTTPException(400, f"Directory not found: {work_dir}")
    _validate_cameras(params.cameras, work_dir)
    _validate_cameras([params.cam_a, params.cam_b], work_dir)

    d = _resolve_params(params.model_dump(), work_dir)
    d["work_dir"] = str(work_dir)
    save_job_config(work_dir, d)
    if params.positive or params.negative:
        save_prompts_to_config(work_dir / "config.ini", params.positive or "", params.negative or "")

    _preserve_rerun = (
        "music_dir", "selected_track", "music_file", "music_files",
        "shorts_music_dir", "shorts_music_dirs",
        "cc_brightness", "cc_gamma", "cc_contrast", "cc_saturation", "cc_temperature",
    )
    for _k in _preserve_rerun:
        if not d.get(_k) and job.params.get(_k):
            d[_k] = job.params[_k]
    job.params         = d
    job.log            = _LogList(JOBS_DIR / f"{job.id}.log")
    job.status         = "queued"
    job.phase          = "analyzing"
    job.analyze_result = None
    job.started_at     = time.time()
    job.ended_at       = None
    job.process        = None
    job.save()

    job._task = _enqueue_job_task(job, _run_job(job, analyze_only=True))
    return {"id": job_id}


@router.post("/api/jobs/{job_id}/estimate")
async def estimate_job(job_id: str, body: dict = Body({})):
    import pipeline as _pipeline
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        raise HTTPException(409, "Job is running")
    merged = {**job.params, **body}
    result = await _pipeline.estimate(merged, job.work_dir())
    if not result:
        raise HTTPException(400, "No scores CSV — run analysis first")
    return result


@router.post("/api/jobs/{job_id}/find-threshold")
async def find_threshold_job(job_id: str, body: dict = Body({})):
    import pipeline as _pipeline
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        raise HTTPException(409, "Job is running")
    target_sec = float(body.get("target_sec") or 0)
    if target_sec <= 0:
        raise HTTPException(400, "target_sec required")
    merged = {**job.params, **body}
    search_id = uuid.uuid4().hex[:12]
    _threshold_searches[search_id] = {"done": False, "iteration": 0, "total": 12}

    async def _run():
        try:
            async for update in _pipeline.find_threshold_iter(merged, job.work_dir(), target_sec):
                _threshold_searches[search_id] = update
        except asyncio.CancelledError:
            _threshold_searches[search_id] = {"done": True, "cancelled": True}
        finally:
            _threshold_tasks.pop(search_id, None)

    task = asyncio.create_task(_run())
    _threshold_tasks[search_id] = task
    return {"search_id": search_id}


@router.get("/api/threshold-search/{search_id}")
async def get_threshold_search(search_id: str):
    st = _threshold_searches.get(search_id)
    if st is None:
        raise HTTPException(404)
    return st


@router.delete("/api/threshold-search/{search_id}")
async def cancel_threshold_search(search_id: str):
    task = _threshold_tasks.pop(search_id, None)
    if task and not task.done():
        task.cancel()
    _threshold_searches.pop(search_id, None)
    return {"ok": True}


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    d = job.to_dict()
    try:
        work_dir = job.work_dir()
        if work_dir and work_dir.is_dir():
            d["params"] = _resolve_params(dict(d["params"]), work_dir)
    except Exception:
        pass
    return d


@router.get("/api/jobs/{job_id}/log")
async def get_job_log(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    log_path = JOBS_DIR / f"{job_id}.log"
    try:
        text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    except Exception:
        text = ""
    lines = [l for l in text.splitlines() if l]
    return {"lines": lines}


@router.delete("/api/jobs/{job_id}/log")
async def clear_job_log(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    job.log.clear_file()
    return {"ok": True}


@router.get("/api/jobs/{job_id}/analyze-result")
async def get_analyze_result(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    result = {}
    if job.analyze_result:
        result = dict(job.analyze_result)
    else:
        ar_path = job.auto_dir() / "analyze_result.json"
        if ar_path.exists():
            try:
                result = json.loads(ar_path.read_text())
            except Exception:
                pass
    _actual_thr = None
    _actual_scenes = None
    _actual_dur = None
    _final_dur = None
    for line in job.log:
        m = re.search(r'Threshold:\s*([\d.]+)', line)
        if m:
            _actual_thr = float(m.group(1))
            result.setdefault("auto_threshold", _actual_thr)
        m = re.search(r'Selected:\s*(\d+)\s*scenes', line)
        if m:
            _actual_scenes = int(m.group(1))
        m = re.search(r'Total:.*\(([\d.]+)s\)', line) or re.search(r'Total:\s*([\d.]+)s', line)
        if m:
            _actual_dur = float(m.group(1))
        m = re.search(r'Final:.*\(([\d.]+)s\)', line)
        if m:
            _final_dur = float(m.group(1))
    if _actual_scenes is not None:
        result["actual_selected_scenes"] = _actual_scenes
    # Prefer final (post-intro/outro) duration over raw clips total
    if _final_dur is not None:
        result["actual_duration_sec"] = _final_dur
    elif _actual_dur is not None:
        result["actual_duration_sec"] = _actual_dur
    if _actual_thr is not None:
        result["actual_threshold"] = _actual_thr
    if result:
        try:
            _cp_cfg = configparser.ConfigParser()
            _cp_cfg.read([str(APP_DIR / "config.ini"), str(job.work_dir() / "config.ini")])
            result["intro_dur_sec"] = _cp_cfg.getfloat("intro_outro", "duration", fallback=3.0)
        except Exception:
            result["intro_dur_sec"] = 3.0
        return result
    result = {}
    for line in job.log:
        m = re.search(r'Threshold:\s*([\d.]+)', line)
        if m:
            result["auto_threshold"] = float(m.group(1))
        m = re.search(r'Selected:\s*(\d+)\s*scenes', line)
        if m:
            result["estimated_scenes"] = int(m.group(1))
        m = re.search(r'Total:\s*([\d.]+)s', line)
        if m:
            result["estimated_duration_sec"] = float(m.group(1))
    try:
        scores_csv = job.auto_dir() / "scene_scores.csv"
        if scores_csv.exists():
            df = pd.read_csv(scores_csv).dropna(subset=["score"])
            result["scene_count"] = int(len(df))
            threshold = result.get("auto_threshold",
                        float(job.params.get("threshold") or 0.148))
            est_scenes = int((df["score"] >= threshold).sum())
            result["estimated_scenes"] = result.get("estimated_scenes", est_scenes)
            if not result.get("estimated_duration_sec"):
                avg_dur, cnt = 0.0, 0
                max_scene = float(job.params.get("max_scene") or 10)
                per_file  = float(job.params.get("per_file")  or max_scene)
                cap = min(max_scene, per_file)
                for csv_path in (job.auto_dir() / "csv").glob("*-Scenes.csv"):
                    try:
                        sdf = _read_scenes_csv(csv_path)
                        for _, row in sdf.iterrows():
                            d = float(row.get("Length (seconds)", 0) or 0)
                            if d > 0:
                                avg_dur += min(d, cap); cnt += 1
                    except Exception:
                        pass
                avg_dur = (avg_dur / cnt) if cnt else cap * 0.6
                result["estimated_duration_sec"] = round(est_scenes * avg_dur, 1)
    except Exception:
        pass
    if result:
        try:
            _cp_cfg2 = configparser.ConfigParser()
            _cp_cfg2.read([str(APP_DIR / "config.ini"), str(job.work_dir() / "config.ini")])
            result["intro_dur_sec"] = _cp_cfg2.getfloat("intro_outro", "duration", fallback=3.0)
        except Exception:
            result["intro_dur_sec"] = 3.0
        return result
    raise HTTPException(404, "No analysis data available")


@router.post("/api/jobs/{job_id}/render")
async def render_job(job_id: str, params: RenderParams):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if params.threshold is not None:
        job.params["threshold"] = params.threshold
    if params.max_scene is not None:
        job.params["max_scene"] = params.max_scene
    if params.per_file is not None:
        job.params["per_file"] = params.per_file
    if params.music_files is not None:
        job.params["music_files"] = params.music_files
    if params.threshold is not None or params.max_scene is not None or params.per_file is not None or params.music_files is not None:
        job.save()

    track = params.selected_track
    if track:
        tp = Path(track).resolve()
        if not in_browse_root(tp):
            raise HTTPException(403, "Track path outside allowed root")
        if not tp.exists():
            raise HTTPException(400, f"Track not found: {track}")

    job.log.append("")
    job.log.append("── Render phase ──────────────────────────")

    if not track and _ACR_HOST and _ACR_KEY and _ACR_SECRET:
        track = await _acr_preselect(job)

    job.status         = "queued"
    job.phase          = "rendering"
    job.ended_at       = None
    job.selected_track = track
    job.save()

    job._task = _enqueue_job_task(job, _run_job(job, analyze_only=False, selected_track=track))
    return {"id": job_id, "phase": "rendering"}


@router.post("/api/jobs/{job_id}/render-short")
async def render_short(job_id: str, data: dict = Body(default={})):
    count = max(1, min(int(data.get("count", 1)), 20))
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if data.get("best"):
        job.params["shorts_best"] = True
    else:
        job.params.pop("shorts_best", None)
    job.log.append("")
    job.log.append(f"── Render Short{'s ×' + str(count) if count > 1 else ''} ──────────────────────────")
    main_active = job.status in ("running", "queued")
    if main_active:
        # Run in parallel with main render — don't touch job.status/phase
        job._shorts_task = asyncio.create_task(_run_shorts(job, count=count, parallel=True))
    else:
        job.status = "queued"
        job.phase  = "shorts"
        job.ended_at = None
        job.save()
        job._task = asyncio.create_task(_run_shorts(job, count=count, parallel=False))
    return {"id": job_id, "phase": "shorts" if not main_active else job.phase, "count": count}


@router.post("/api/jobs/{job_id}/preview-sequence")
async def preview_sequence(job_id: str):
    """Run music_driven --dry-run → return preview_sequence.json content."""
    return await _preview_sequence_inner(job_id)


async def _preview_sequence_inner(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status == "running":
        raise HTTPException(409, "Job is running — wait for it to finish")

    auto_dir = job.auto_dir()
    seq_path = auto_dir / "preview_sequence.json"

    # Resolve music file same way as render-music-driven
    music_path_str = ""
    selected = job.params.get("selected_track") or job.params.get("music_file") or ""
    music_files = job.params.get("music_files") or []
    if not selected:
        if len(music_files) == 1:
            selected = music_files[0]
        elif len(music_files) > 1:
            import random as _rnd2
            selected = _rnd2.choice(music_files)
    if selected:
        music_path_str = _expand_path(selected)
    else:
        music_dir = job.params.get("music_dir") or ""
        if music_dir:
            music_path_str = _expand_path(music_dir)

    if not music_path_str:
        raise HTTPException(400, "No music track selected")

    save_job_config(Path(job.params["work_dir"]), job.params)

    # Persist manual_overrides → JSON so music_driven dry-run sees the bans.
    # Frontend PATCHes manual_overrides into params; subprocess reads from disk.
    _ov_payload = job.params.get("manual_overrides") or {}
    _ov_path = auto_dir / "manual_overrides.json"
    _ov_path.parent.mkdir(parents=True, exist_ok=True)
    _ov_path.write_text(json.dumps(_ov_payload))

    # Manual timeline takes priority — skip dry-run
    manual_tl = job.params.get("manual_timeline")
    if manual_tl and isinstance(manual_tl, list) and len(manual_tl) > 0:
        seq_data = {"sequence": manual_tl, "music": music_path_str}
        seq_path.write_text(json.dumps(seq_data))
        for slot in manual_tl:
            fp = slot.get("frame_path")
            slot["frame_url"] = ("/" + Path(fp).relative_to("/").as_posix()) if fp else None
        return {"sequence": manual_tl, "music": music_path_str}

    # Write photo_selection.json so dry-run can weave photo slots
    _sel_photos = job.params.get("selected_photos") or []
    _photo_sel_path = Path(job.params["work_dir"]) / "_autoframe" / "photo_selection.json"
    _photo_sel_path.parent.mkdir(parents=True, exist_ok=True)
    _photo_sel_path.write_text(json.dumps({"photos": _sel_photos}))

    cmd = [sys.executable, str(SCRIPT_DIR / "music_driven.py"),
           job.params["work_dir"], "--dry-run"]
    if music_path_str:
        if Path(music_path_str).is_file():
            cmd += ["--music", music_path_str]
        else:
            cmd += ["--music-dir", music_path_str]
    top_pct = float(job.params.get("md_top_percent", 0.40))
    cmd += ["--top-percent", str(top_pct)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(SCRIPT_DIR),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        _out = stdout.decode(errors='replace')
        print(f"[preview-sequence] {job_id}:\n{_out}", flush=True)
        if proc.returncode != 0:
            raise HTTPException(500, f"Dry-run failed:\n{_out[-2000:]}")
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        raise HTTPException(504, "Dry-run timed out (>180s)")

    if not seq_path.exists():
        raise HTTPException(500, "preview_sequence.json not written")

    data = json.loads(seq_path.read_text())

    # Attach frame URLs (relative paths → web paths)
    work_dir_str = str(job.params.get("work_dir", ""))
    for slot in data.get("sequence", []):
        fp = slot.get("frame_path")
        if fp:
            slot["frame_url"] = "/" + Path(fp).relative_to("/").as_posix()
        else:
            slot["frame_url"] = None

    return data


@router.post("/api/jobs/{job_id}/preview-render")
async def preview_render(job_id: str):
    """Render a low-quality preview from preview_sequence.json (NVENC 1080p + music)."""
    import tempfile
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status == "running":
        raise HTTPException(409, "Job is running — wait for it to finish")

    auto_dir = job.auto_dir()
    seq_path = auto_dir / "preview_sequence.json"
    if not seq_path.exists():
        raise HTTPException(400, "Run 'Build Timeline' first")

    data = json.loads(seq_path.read_text())
    sequence = data.get("sequence", [])
    if not sequence:
        raise HTTPException(400, "Empty sequence")

    # Music: prefer job param, fall back to sequence metadata
    music_path_str = (
        job.params.get("selected_track") or
        job.params.get("music_file") or
        data.get("music", "")
    )
    music_ss = sequence[0].get("music_start", 0.0)

    output = auto_dir / "preview_draft.mp4"
    concat_path = auto_dir / "preview_concat.txt"

    # Build ffconcat file — inpoint + outpoint per slot
    lines = ["ffconcat version 1.0"]
    for slot in sequence:
        cp = slot.get("clip_path", "")
        if not cp or not Path(cp).exists():
            continue
        ss   = float(slot.get("clip_ss", 0))
        dur  = float(slot.get("duration", 0))
        lines.append(f"file '{cp}'")
        lines.append(f"inpoint {ss:.4f}")
        lines.append(f"outpoint {ss + dur:.4f}")
    concat_path.write_text("\n".join(lines))

    # FFMPEG: concat → scale 1080p → NVENC; add music if available
    _vf = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
           "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
    ]
    has_music = bool(music_path_str and Path(music_path_str).exists())
    if has_music:
        cmd += ["-ss", f"{music_ss:.4f}", "-i", music_path_str]

    cmd += ["-vf", _vf, "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28"]
    if has_music:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
    else:
        cmd += ["-an"]
    cmd.append(str(output))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        if proc.returncode != 0:
            raise HTTPException(500,
                f"Preview render failed:\n{stdout.decode(errors='replace')[-2000:]}")
    except asyncio.TimeoutError:
        raise HTTPException(504, "Preview render timed out (>180s)")

    return {"url": str(output)}


def _build_preview_inputs(job, max_clips: int = 0) -> dict:
    """Build ffmpeg inputs/filter for preview endpoints. Returns dict with all needed parts."""
    import platform as _plat
    import random as _rndp

    auto_dir = job.auto_dir()
    seq_path = auto_dir / "preview_sequence.json"
    if not seq_path.exists():
        raise HTTPException(400, "Rebuild timeline first (preview_sequence.json missing)")

    data = json.loads(seq_path.read_text())
    sequence = data.get("sequence", [])
    if not sequence:
        raise HTTPException(400, "Empty sequence")

    _gcfg = configparser.ConfigParser()
    _gcfg.read(str(APP_DIR / "config.ini"))
    ffmpeg_bin = _gcfg.get("paths", "ffmpeg", fallback="ffmpeg")

    music_path_str = _expand_path(
        job.params.get("selected_track") or job.params.get("music_file") or data.get("music", "")
    )
    if music_path_str and Path(music_path_str).is_dir():
        _cands = [f for f in Path(music_path_str).iterdir()
                  if f.suffix.lower() in (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg")]
        music_path_str = str(_rndp.choice(_cands)) if _cands else ""
    music_ss = float(sequence[0].get("music_start", 0.0))

    valid_slots: list[tuple] = []
    for slot in sequence:
        dur = float(slot.get("duration", 0))
        if dur <= 0:
            continue
        if slot.get("type") == "photo":
            p = slot.get("path") or slot.get("frame_path")
            if p and Path(p).exists():
                valid_slots.append(("photo", slot, p, dur))
        else:
            cp = slot.get("clip_path")
            ss = float(slot.get("clip_ss", 0))
            if cp and Path(cp).exists():
                valid_slots.append(("video", slot, cp, ss, dur))

    if not valid_slots:
        raise HTTPException(400, "No valid clips found in sequence")
    if max_clips > 0:
        valid_slots = valid_slots[:max_clips]

    _local_cp = configparser.ConfigParser()
    _local_cp.read([str(APP_DIR / "config.ini"),
                    str(Path(job.params.get("work_dir", "")) / "config.ini")])
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from color_correct import chain_from_cp as _cc_chain
    cc_chain = _cc_chain(_local_cp)

    has_music = bool(music_path_str and Path(music_path_str).exists())
    _norm_pad  = ("fps=30,setpts=PTS-STARTPTS,"
                  "scale=1280:720:force_original_aspect_ratio=decrease,"
                  "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1")
    _norm_crop = ("fps=30,setpts=PTS-STARTPTS,"
                  "scale=1280:720:force_original_aspect_ratio=increase,"
                  "crop=1280:720,setsar=1")
    cam_crop = job.params.get("cam_crop_16x9") or {}

    import re as _re_cam
    _single_cam_crop = len(cam_crop) == 1 and next(iter(cam_crop.values()), False)
    def _per_cam_filter(clip_path: str) -> str:
        cam = _re_cam.sub(r'-(scene|clip)-\d+$', '', Path(clip_path).stem)
        use_crop = cam_crop.get(cam) if cam in cam_crop else (_single_cam_crop if cam_crop else False)
        base = _norm_crop if use_crop else _norm_pad
        return (base + "," + cc_chain) if cc_chain else base

    inputs: list[str] = []
    filter_parts: list[str] = []
    for i, item in enumerate(valid_slots):
        if item[0] == "photo":
            _, _s, path, dur = item
            inputs += ["-loop", "1", "-framerate", "30", "-t", f"{dur:.4f}", "-i", path]
            vf = (_norm_pad + "," + cc_chain) if cc_chain else _norm_pad
        else:
            _, _s, clip_path, ss, dur = item
            inputs += ["-ss", f"{ss:.4f}", "-t", f"{dur:.4f}", "-i", clip_path]
            vf = _per_cam_filter(clip_path)
        filter_parts.append(f"[{i}:v]{vf}[v{i}]")
    n = len(valid_slots)
    filter_parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[out]")

    if _plat.system() == "Darwin":
        vcodec = ["-c:v", "h264_videotoolbox", "-b:v", "3M", "-g", "30", "-pix_fmt", "yuv420p"]
    else:
        vcodec = ["-c:v", "h264_nvenc", "-preset", "p1", "-b:v", "3M", "-g", "30", "-pix_fmt", "yuv420p"]

    return dict(ffmpeg_bin=ffmpeg_bin, inputs=inputs, filter_parts=filter_parts,
                n=n, vcodec=vcodec, has_music=has_music,
                music_path_str=music_path_str, music_ss=music_ss,
                valid_slots=valid_slots)


_hls_procs: dict[str, "asyncio.subprocess.Process"] = {}


@router.post("/api/jobs/{job_id}/preview-hls")
async def start_preview_hls(job_id: str):
    """Start HLS render in background; return playlist URL after first segment appears."""
    import shutil as _sh, traceback as _tb

    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    auto_dir = job.auto_dir()
    hls_dir  = auto_dir / "preview_hls"
    playlist = hls_dir / "playlist.m3u8"

    try:
        # Kill stale HLS proc if still running
        old = _hls_procs.pop(job_id, None)
        if old and old.returncode is None:
            old.kill()

        _sh.rmtree(hls_dir, ignore_errors=True)
        hls_dir.mkdir(parents=True, exist_ok=True)

        # Limit preview clips to avoid OOM; filter_complex is smoother than concat demuxer
        MAX_PREVIEW_CLIPS = 40
        p = _build_preview_inputs(job, max_clips=MAX_PREVIEW_CLIPS)

        hls_vcodec = ["-c:v", "h264_nvenc", "-preset", "p1", "-b:v", "3M", "-g", "30", "-pix_fmt", "yuv420p"]
        hls_tail = ["-avoid_negative_ts", "make_zero",
                    "-f", "hls", "-hls_time", "3", "-hls_playlist_type", "event",
                    "-hls_flags", "append_list",
                    "-hls_segment_filename", str(hls_dir / "seg%03d.ts"),
                    str(playlist)]

        cmd = [p["ffmpeg_bin"], "-y", "-hide_banner", "-loglevel", "error"]
        cmd += p["inputs"]
        if p["has_music"]:
            cmd += ["-ss", f"{p['music_ss']:.4f}", "-i", p["music_path_str"]]
        cmd += ["-filter_complex", ";".join(p["filter_parts"]), "-map", "[out]"]
        cmd += hls_vcodec
        if p["has_music"]:
            cmd += ["-map", f"{p['n']}:a:0", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        else:
            cmd += ["-an"]
        cmd += hls_tail

        print(f"[preview-hls] starting: {p['n']} clips, music={p['has_music']}", flush=True)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _hls_procs[job_id] = proc

        # Wait for 3 segments (~9s of content) before returning URL
        for _ in range(60):
            if playlist.exists() and len(list(hls_dir.glob("seg*.ts"))) >= 3:
                break
            if proc.returncode is not None:
                err = (await proc.stderr.read()).decode(errors="replace")[-2000:]
                print(f"[preview-hls] ffmpeg failed (rc={proc.returncode}):\n{err}", flush=True)
                raise HTTPException(500, f"HLS encode failed:\n{err}")
            await asyncio.sleep(0.5)

        if not playlist.exists():
            proc.kill()
            raise HTTPException(500, "HLS init timed out (no playlist after 30s)")

        return {"url": f"/api/jobs/{job_id}/preview-hls/playlist.m3u8"}

    except HTTPException:
        raise
    except Exception as _exc:
        _tb.print_exc()
        raise HTTPException(500, f"preview-hls error: {_exc}") from _exc


@router.get("/api/jobs/{job_id}/preview-hls/{filename}")
async def serve_preview_hls(job_id: str, filename: str):
    """Serve HLS playlist and segments."""
    import re as _re
    from fastapi.responses import FileResponse as _FR2
    if not _re.match(r'^(playlist\.m3u8|seg\d+\.ts)$', filename):
        raise HTTPException(400)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    p = job.auto_dir() / "preview_hls" / filename
    if not p.exists():
        raise HTTPException(404)
    if filename.endswith(".m3u8"):
        return _FR2(str(p), media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store, no-cache"})
    return _FR2(str(p), media_type="video/mp2t",
                headers={"Cache-Control": "max-age=3600"})


@router.get("/api/jobs/{job_id}/preview-stream")
async def preview_stream(job_id: str):
    """Stream preview: per-clip SW decode → raw YUV420p pipe → single NVENC encoder → fMP4.

    Handles mixed H264/HEVC without codec lock: each clip decoded independently,
    raw frames piped to one persistent NVENC encoder. Zero temp files, instant start.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    auto_dir = job.auto_dir()
    seq_path = auto_dir / "preview_sequence.json"
    if not seq_path.exists():
        raise HTTPException(400, "Rebuild timeline first (preview_sequence.json missing)")

    data = json.loads(seq_path.read_text())
    sequence = data.get("sequence", [])
    if not sequence:
        raise HTTPException(400, "Empty sequence")

    _gcfg = configparser.ConfigParser()
    _gcfg.read(str(APP_DIR / "config.ini"))
    ffmpeg_bin = _gcfg.get("paths", "ffmpeg", fallback="ffmpeg")
    ffprobe_bin = str(Path(ffmpeg_bin).parent / "ffprobe")

    music_path_str = (
        job.params.get("selected_track") or
        job.params.get("music_file") or
        data.get("music", "")
    )
    music_ss = float(sequence[0].get("music_start", 0.0))
    has_music = bool(music_path_str and Path(music_path_str).exists())

    # If any camera has 4:3→16:9 crop enabled, force 16:9 output with center crop.
    # crop=iw:min(ih,iw*9/16) is a no-op for 16:9 sources and crops 4:3 to 16:9.
    crop_map = job.params.get("cam_crop_16x9") or {}
    need_crop = any(v for v in crop_map.values())

    W, H = 1280, 720
    if not need_crop:
        # Probe first valid clip to preserve native aspect ratio
        for slot in sequence:
            cp = slot.get("clip_path", "")
            if cp and Path(cp).exists():
                _p = await asyncio.create_subprocess_exec(
                    ffprobe_bin, "-v", "quiet", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height", "-of", "csv=p=0", cp,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                _out, _ = await _p.communicate()
                _parts = _out.decode().strip().split(",")
                if len(_parts) == 2:
                    sw, sh = int(_parts[0]), int(_parts[1])
                    H = (sh * W // sw) & ~1
                break

    dec_vf = (
        f"crop=iw:min(ih\\,iw*9/16),scale={W}:{H}:flags=fast_bilinear"
        if need_crop else
        f"scale={W}:{H}:flags=fast_bilinear"
    )
    photo_vf = (
        f"scale={W}:{H}:flags=fast_bilinear:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
    )

    enc_cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{W}x{H}", "-r", "30",
        "-i", "pipe:0",
    ]
    if has_music:
        enc_cmd += ["-ss", f"{music_ss:.4f}", "-i", music_path_str]
    enc_cmd += ["-map", "0:v:0", "-c:v", "h264_nvenc", "-preset", "p1", "-b:v", "3M", "-g", "30"]
    if has_music:
        enc_cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        enc_cmd += ["-an"]
    enc_cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]

    async def _stream():
        enc = await asyncio.create_subprocess_exec(
            *enc_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def _start_dec(slot):
            dur = float(slot.get("duration", 3.0))
            if slot.get("type") == "photo":
                photo_path = slot.get("frame_url", "")
                return await asyncio.create_subprocess_exec(
                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                    "-loop", "1", "-i", photo_path, "-t", f"{dur:.4f}",
                    "-vf", photo_vf,
                    "-pix_fmt", "yuv420p", "-r", "30",
                    "-f", "rawvideo", "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            cp  = slot["clip_path"]
            ss  = float(slot.get("clip_ss", 0))
            return await asyncio.create_subprocess_exec(
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-hwaccel", "cuda",
                "-ss", f"{ss:.4f}", "-i", cp, "-t", f"{dur:.4f}",
                "-vf", dec_vf,
                "-pix_fmt", "yuv420p", "-r", "30",
                "-f", "rawvideo", "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

        async def _feed():
            slots = [s for s in sequence
                     if (s.get("type") == "photo" and s.get("frame_url") and Path(s["frame_url"]).exists())
                     or (s.get("clip_path") and Path(s["clip_path"]).exists())]
            cur_dec = None
            nxt_dec = None
            try:
                if not slots:
                    return
                cur_dec = await _start_dec(slots[0])
                for i, slot in enumerate(slots):
                    # start next decoder while piping current — overlaps startup latency
                    if i + 1 < len(slots):
                        nxt_dec = await _start_dec(slots[i + 1])
                    else:
                        nxt_dec = None
                    while True:
                        chunk = await cur_dec.stdout.read(131072)
                        if not chunk:
                            break
                        enc.stdin.write(chunk)
                        await enc.stdin.drain()
                    await cur_dec.wait()
                    cur_dec = nxt_dec
                    nxt_dec = None
            finally:
                for p in (cur_dec, nxt_dec):
                    if p is not None and p.returncode is None:
                        p.kill()
                enc.stdin.close()

        feed_task = asyncio.create_task(_feed())
        try:
            while True:
                chunk = await enc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass
            if enc.returncode is None:
                enc.kill()

    return StreamingResponse(
        _stream(),
        media_type="video/mp4",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/api/jobs/{job_id}/render-music-driven")
async def render_music_driven(job_id: str, data: dict = Body(default={})):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    # Update beats-per-shot if sent from UI
    for _bk in ("beats_fast", "beats_mid", "beats_slow"):
        if data.get(_bk) is not None:
            job.params[_bk] = int(data[_bk])
    job.save()

    # Resolve music file: explicit → pinned track → music_files param → auto-pick from music_dir
    music_path_str = data.get("music_file") or job.selected_track or ""
    if not music_path_str:
        _mf = job.params.get("music_files") or []
        if len(_mf) == 1:
            music_path_str = _mf[0]
        elif len(_mf) > 1:
            # Multiple tracks selected — ACR preselect from that filtered pool
            if _ACR_HOST and _ACR_KEY and _ACR_SECRET:
                music_path_str = await _acr_preselect(job) or ""
            if not music_path_str:
                import random as _rnd
                music_path_str = _rnd.choice(_mf)
    if not music_path_str:
        music_dir = job.params.get("music_dir", "")
        if not music_dir:
            try:
                _cfg = configparser.ConfigParser()
                _cfg.read(SCRIPT_DIR / ".." / "config.ini")
                music_dir = _cfg.get("music", "dir", fallback="")
            except Exception:
                pass
        if music_dir:
            music_path_str = music_dir  # pass dir; music_driven.py will pick_music

    job.log.append("")
    job.log.append("── Music-driven render ───────────────────")
    job.status   = "queued"
    job.phase    = "music-driven"
    job.ended_at = None
    job.save()

    async def _run():
        async with job_semaphore:
            job.status     = "running"
            job.started_at = time.time()
            job.progress = 0
            job.progress_label = ''
            job.save()
            await job.broadcast({"type": "status", "status": "running", "phase": "music-driven"})
            try:
                # Flush in-memory params to config.ini so music_driven.py sees
                # the latest cam_pattern / beats_* / gps_weight even when the
                # frontend PUT /api/job-config was still in-flight at render time.
                save_job_config(Path(job.params["work_dir"]), job.params)

                # Persist manual_overrides → JSON so music_driven render hard-excludes banned scenes.
                _ov_payload = job.params.get("manual_overrides") or {}
                _ov_path = job.auto_dir() / "manual_overrides.json"
                _ov_path.parent.mkdir(parents=True, exist_ok=True)
                _ov_path.write_text(json.dumps(_ov_payload))

                # Write photo_selection.json so render can weave photo slots
                _sel_photos = job.params.get("selected_photos") or []
                _photo_sel_path = Path(job.params["work_dir"]) / "_autoframe" / "photo_selection.json"
                _photo_sel_path.parent.mkdir(parents=True, exist_ok=True)
                _photo_sel_path.write_text(json.dumps({"photos": _sel_photos}))

                # If user edited timeline in UI, persist that exact ordering and
                # tell music_driven to render it (skips analyse/match).
                _manual_tl = job.params.get("manual_timeline")
                _use_saved = bool(_manual_tl and isinstance(_manual_tl, list) and len(_manual_tl) > 0)
                if _use_saved:
                    _seq_path = job.auto_dir() / "preview_sequence.json"
                    _seq_path.parent.mkdir(parents=True, exist_ok=True)
                    _seq_path.write_text(json.dumps({
                        "sequence": _manual_tl,
                        "music":    music_path_str,
                    }))

                cmd = [sys.executable, str(SCRIPT_DIR / "music_driven.py"),
                       job.params["work_dir"]]
                if music_path_str:
                    if Path(music_path_str).is_file():
                        cmd += ["--music", music_path_str]
                    else:
                        cmd += ["--music-dir", music_path_str]
                top_pct = float(job.params.get("md_top_percent", 0.40))
                cmd += ["--top-percent", str(top_pct)]
                if _use_saved:
                    cmd += ["--use-saved-sequence"]

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(SCRIPT_DIR),
                )
                job.process = proc
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue
                    # WORKER_* structured progress from music_driven.py parallel encoding
                    _wm = re.match(r'^WORKER_(START|PROGRESS|DONE)\s+(\d+)(.*)', line)
                    if _wm:
                        wstate, slot = _wm.group(1), int(_wm.group(2))
                        rest = _wm.group(3).strip().split()
                        if wstate == 'START' and rest:
                            try:
                                curr, total = map(int, rest[0].split('/'))
                                await job.broadcast({"type": "worker_progress", "slot": slot, "state": "start", "curr": curr, "total": total})
                            except (ValueError, IndexError):
                                pass
                        elif wstate == 'PROGRESS' and len(rest) >= 2:
                            try:
                                pct = int(rest[0])
                                curr, total = map(int, rest[1].split('/'))
                                job.progress = round(curr / total * 100) if total else 0
                                await job.broadcast({"type": "worker_progress", "slot": slot, "state": "running", "pct": pct, "curr": curr, "total": total})
                            except (ValueError, IndexError):
                                pass
                        elif wstate == 'DONE':
                            await job.broadcast({"type": "worker_progress", "slot": slot, "state": "done"})
                        continue
                    # Track [N/M] overall progress
                    _pm = re.search(r'\[\s*(\d+)\s*/\s*(\d+)\s*\](.*)', line)
                    if _pm and int(_pm.group(2)) > 0:
                        job.progress = round(int(_pm.group(1)) / int(_pm.group(2)) * 100)
                        job.progress_label = _pm.group(3).strip()[:50]
                    job.log.append(line)
                    await job.broadcast({"type": "log", "line": line})
                await proc.wait()
                ok = proc.returncode == 0

                # Intro/outro + rename + preview
                if ok:
                    import pipeline as _pipeline
                    _work_dir   = Path(job.params["work_dir"])
                    _auto_dir   = _work_dir / "_autoframe"
                    _hl         = _auto_dir / "highlight_music_driven.mp4"
                    if _hl.exists():
                        job.phase = "postprocess"
                        await job.broadcast({"type": "status", "status": "running", "phase": "postprocess"})
                        async for _line in _pipeline.apply_postprocess(_work_dir, _hl, job.params):
                            job.log.append(_line)
                            await job.broadcast({"type": "log", "line": _line})
                    else:
                        job.log.append("⚠ highlight_music_driven.mp4 not found — skipping postprocess")
                        await job.broadcast({"type": "log", "line": "⚠ highlight_music_driven.mp4 not found"})

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                job.log.append(f"ERROR: {exc}")
                await job.broadcast({"type": "log", "line": f"ERROR: {exc}"})
                ok = False

            # Record used track in global index + write .meta.json sidecar
            if ok and music_path_str and Path(music_path_str).is_file():
                try:
                    from webapp.routers.music import record_used_track
                    from datetime import timezone as _tz2
                    _work = Path(job.params.get("work_dir", ""))
                    _render_name = ""
                    # Find the output filename from log (→ YYYY-*.mp4 line)
                    for _ll in reversed(job.log[-20:]):
                        if _ll.strip().startswith("→ ") and ".mp4" in _ll:
                            _render_name = _ll.strip()[2:].strip()
                            break
                    _yt = job.params.get("yt_url", "")
                    record_used_track(music_path_str, str(_work), _render_name, _yt)
                    # Write .meta.json so Results tab shows music name
                    if _render_name:
                        _out_p = _work / _render_name
                        if not _out_p.exists():
                            _out_p = _work / Path(_render_name).name
                        if _out_p.exists():
                            _meta = {
                                "music":        music_path_str,
                                "generated_at": datetime.now(_tz2.utc).isoformat(),
                            }
                            _out_p.with_suffix(".meta.json").write_text(
                                json.dumps(_meta, ensure_ascii=False)
                            )
                except Exception:
                    pass

            job.status   = "done" if ok else "failed"
            job.phase    = "done" if ok else "failed"
            job.ended_at = time.time()
            job.save()
            await job.broadcast({"type": "status", "status": job.status, "phase": job.phase})

    job._task = _enqueue_job_task(job, _run())
    return {"id": job_id, "phase": "music-driven"}


@router.post("/api/jobs/{job_id}/generate-metadata")
async def generate_metadata(job_id: str):
    """Run CLIP zero-shot + chapter generation for the job's selected scenes."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    work_dir = job.params.get("work_dir", "")
    if not work_dir:
        raise HTTPException(400, "No work_dir")

    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / ".." / "config.ini")
    ffprobe = cfg.get("paths", "ffprobe", fallback="ffprobe")

    try:
        import sys as _sys
        _sys.path.insert(0, str(SCRIPT_DIR))
        from metadata_gen import generate as _gen_metadata
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _gen_metadata(Path(work_dir), ffprobe=ffprobe),
        )
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.get("/api/queue")
async def get_queue():
    running, queued = [], []
    for job in jobs.values():
        if job.status == "running":
            running.append({
                "id": job.id,
                "work_dir": job.params.get("work_dir", ""),
                "phase": job.phase,
                "started_at": job.started_at,
                "shorts_running": job.shorts_running,
            })
        elif job.status == "queued":
            queued.append({
                "id": job.id,
                "work_dir": job.params.get("work_dir", ""),
                "phase": job.phase,
                "created_at": job.created_at,
            })
    running.sort(key=lambda j: j["started_at"])
    queued.sort(key=lambda j: j["created_at"])
    return {"running": running, "queued": queued}


@router.delete("/api/jobs/{job_id}/dequeue")
async def dequeue_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status != "queued":
        raise HTTPException(409, "Job is not queued")
    if job._task and not job._task.done():
        job._task.cancel()
    job.status = "failed"
    job.ended_at = time.time()
    job.log.append("[dequeued by user]")
    job.save()
    await job.broadcast({"type": "status", "status": "failed", "phase": job.phase})
    return {"ok": True}


@router.delete("/api/jobs/{job_id}")
async def kill_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        if job._task and not job._task.done():
            job._task.cancel()
        elif job.process:
            try:
                os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
            except Exception:
                job.process.terminate()
        if job._shorts_task and not job._shorts_task.done():
            job._shorts_task.cancel()
        job.status = "killed"
        job.ended_at = time.time()
        job.save()
        await job.broadcast({"type": "status", "status": "killed"})
    return {"ok": True}


@router.patch("/api/jobs/{job_id}/params")
async def patch_job_params(job_id: str, data: dict = Body(...)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    allowed = {"threshold", "max_scene", "per_file", "music_dir", "min_gap_sec", "music_files", "selected_track", "manual_timeline", "manual_overrides", "cam_pattern", "beats_auto", "beats_method", "shorts_text", "shorts_multicam", "shorts_beat_sync", "shorts_best", "shorts_duration", "shorts_music_dir", "shorts_music_dirs", "selected_photos", "cameras", "cam_offsets", "cam_crop_16x9", "cc_brightness", "cc_gamma", "cc_contrast", "cc_saturation", "cc_temperature"}
    for k, v in data.items():
        if k in allowed:
            job.params[k] = v
    if "selected_track" in data:
        job.selected_track = data["selected_track"] or None
    job.save()
    return {"ok": True}


@router.post("/api/jobs/{job_id}/remove")
async def remove_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.process and job.status == "running":
        try:
            os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
        except Exception:
            job.process.terminate()
    jobs.pop(job_id, None)
    p = JOBS_DIR / f"{job_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


@router.get("/api/jobs/{job_id}/overrides")
async def get_overrides(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    p = job.auto_dir() / "manual_overrides.json"
    return json.loads(p.read_text()) if p.exists() else {}


@router.put("/api/jobs/{job_id}/overrides")
async def put_overrides(job_id: str, data: dict):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    p = job.auto_dir() / "manual_overrides.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return {"ok": True}


@router.get("/api/jobs/{job_id}/picture-preview")
async def picture_preview(
    job_id: str,
    b: float = 0.0, g: float = 1.0, c: float = 1.0,
    s: float = 1.0, t: float = 0.0,
    idx: int = 0,
):
    """Return a scene frame with color correction applied (PNG).

    idx selects which non-banned scene to show (0 = top scoring).
    Response header X-Scene-Count = total non-banned scenes with frames.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    auto_dir = job.auto_dir()
    csv_path = auto_dir / "scene_scores_allcam.csv"
    if not csv_path.exists():
        csv_path = auto_dir / "scene_scores.csv"
    if not csv_path.exists():
        raise HTTPException(404, "No scene scores yet — run Analyze first")
    import csv as _csv
    import json as _json
    with csv_path.open() as f:
        rows = list(_csv.DictReader(f))
    if not rows:
        raise HTTPException(404, "Empty scene_scores.csv")
    _overrides_path = auto_dir / "manual_overrides.json"
    _banned: set[str] = set()
    if _overrides_path.exists():
        try:
            _ov = _json.loads(_overrides_path.read_text())
            _banned = {k for k, v in _ov.items() if v is False}
        except Exception:
            pass
    def _score(r):
        try:
            return float(r.get("max_score") or r.get("score") or 0)
        except ValueError:
            return 0.0
    rows.sort(key=_score, reverse=True)

    frames_dir = auto_dir / "frames"

    def _find_frame(scene: str):
        for pat in (f"{scene}_f*.jpg", f"{scene}_f*.png", f"{scene}.jpg", f"{scene}.png"):
            hits = sorted(frames_dir.glob(pat))
            if hits:
                return hits[len(hits) // 2]
        return None

    import re as _re
    def _source_stem(scene: str) -> str:
        return _re.sub(r'-(scene|clip)-\d+$', '', scene)

    # One best non-banned scene per source file — gives colour diversity across shots.
    seen_sources: set[str] = set()
    candidates: list[str] = []
    for r in rows:  # already sorted score desc
        sc = r.get("scene", "")
        if sc in _banned or not _find_frame(sc):
            continue
        src = _source_stem(sc)
        if src not in seen_sources:
            seen_sources.add(src)
            candidates.append(sc)
    if not candidates:
        # Fallback: include banned scenes if nothing else has frames
        seen_sources2: set[str] = set()
        for r in rows:
            sc = r.get("scene", "")
            if not _find_frame(sc):
                continue
            src = _source_stem(sc)
            if src not in seen_sources2:
                seen_sources2.add(src)
                candidates.append(sc)
    if not candidates:
        raise HTTPException(404, "No frames found — run Analyze first")

    scene_count = len(candidates)
    src_frame = _find_frame(candidates[idx % scene_count])
    media_type = "image/jpeg" if src_frame.suffix.lower() in (".jpg", ".jpeg") else "image/png"

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from color_correct import build_vf_chain
    vf = build_vf_chain(brightness=b, gamma=g, contrast=c, saturation=s, temperature=t)

    from fastapi.responses import Response, FileResponse
    headers = {"X-Scene-Count": str(scene_count)}
    if not vf:
        return FileResponse(src_frame, media_type=media_type, headers=headers)

    _gcfg = configparser.ConfigParser()
    _gcfg.read(str(APP_DIR / "config.ini"))
    ffmpeg = _gcfg.get("paths", "ffmpeg", fallback="ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(src_frame), "-vf", vf,
         "-frames:v", "1", "-f", "image2", "-c:v", "png", "pipe:1"],
        capture_output=True, timeout=10,
    )
    if proc.returncode != 0:
        raise HTTPException(500, f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-500:]}")
    return Response(content=proc.stdout, media_type="image/png", headers=headers)


@router.post("/api/jobs/{job_id}/regenerate-thumbs")
async def regenerate_thumbs(job_id: str, data: dict = Body(default={})):
    """Regenerate pool thumbnails into _autoframe/frames_cc/ with current color
    correction. Idempotent — skipped when params hash matches cache."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    # Accept overrides from body, then persist to params + config.ini
    for k in ("cc_brightness", "cc_gamma", "cc_contrast", "cc_saturation", "cc_temperature"):
        if k in data:
            job.params[k] = data[k]
    job.save()
    save_job_config(Path(job.params["work_dir"]), job.params)

    auto_dir   = job.auto_dir()
    frames_src = auto_dir / "frames"
    frames_cc  = auto_dir / "frames_cc"
    if not frames_src.exists():
        raise HTTPException(404, "No frames/ — run Analyze first")

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from color_correct import build_vf_chain
    vf = build_vf_chain(
        brightness  = float(job.params.get("cc_brightness",  0)),
        gamma       = float(job.params.get("cc_gamma",       1)),
        contrast    = float(job.params.get("cc_contrast",    1)),
        saturation  = float(job.params.get("cc_saturation",  1)),
        temperature = float(job.params.get("cc_temperature", 0)),
    )

    import shutil
    # No correction → wipe cc dir, frames endpoint falls back to frames/
    if not vf:
        if frames_cc.exists():
            shutil.rmtree(frames_cc)
        return {"regenerated": 0, "total": 0, "skipped": True}

    import hashlib
    params_hash = hashlib.sha256(vf.encode()).hexdigest()[:16]
    hash_file = frames_cc / ".params_hash"
    if hash_file.exists() and hash_file.read_text().strip() == params_hash:
        return {"regenerated": 0, "total": 0, "cached": True}

    if frames_cc.exists():
        shutil.rmtree(frames_cc)
    frames_cc.mkdir(parents=True, exist_ok=True)

    src_files = sorted(p for p in frames_src.iterdir()
                       if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not src_files:
        raise HTTPException(404, "frames/ is empty")

    _gcfg = configparser.ConfigParser()
    _gcfg.read(str(APP_DIR / "config.ini"))
    ffmpeg = _gcfg.get("paths", "ffmpeg", fallback="ffmpeg")

    def _process(src: Path) -> bool:
        dst = frames_cc / src.name
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-vf", vf, "-q:v", "3", str(dst)],
            capture_output=True, timeout=15,
        )
        return proc.returncode == 0 and dst.exists()

    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [loop.run_in_executor(pool, _process, s) for s in src_files]
        results = await asyncio.gather(*futs)

    ok = sum(1 for r in results if r)
    hash_file.write_text(params_hash)
    return {"regenerated": ok, "total": len(src_files), "params_hash": params_hash}


@router.post("/api/jobs/{job_id}/detect-cam-offsets")
async def detect_cam_offsets(job_id: str, data: dict = Body(default={})):
    """Run ffprobe on the first video file per camera and return time offsets."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    work_dir = Path(data.get("work_dir") or job.params.get("work_dir", ""))
    cameras  = data.get("cameras") or job.params.get("cameras") or []
    if not cameras or not work_dir:
        raise HTTPException(400, "work_dir and cameras required")

    VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".ts"}
    _gcfg = configparser.ConfigParser()
    _gcfg.read(str(APP_DIR / "config.ini"))
    ffprobe = _gcfg.get("paths", "ffprobe", fallback="ffprobe")

    async def _creation_time(cam: str) -> float | None:
        cam_dir = work_dir / cam
        if not cam_dir.is_dir():
            return None
        files = sorted(
            (f for f in cam_dir.iterdir() if f.suffix.lower() in VIDEO_EXT),
            key=lambda f: f.name,
        )
        if not files:
            return None
        first = files[0]
        try:
            proc = await asyncio.create_subprocess_exec(
                ffprobe, "-v", "quiet", "-print_format", "json",
                "-show_entries", "format_tags=creation_time",
                str(first),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            info = json.loads(out)
            ct_str = info.get("format", {}).get("tags", {}).get("creation_time", "")
            if not ct_str:
                return None
            from datetime import datetime, timezone
            ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
            return ct.timestamp()
        except Exception:
            return None

    times: dict[str, float | None] = {}
    for cam in cameras:
        times[cam] = await _creation_time(cam)

    # Reference = first camera that has a valid timestamp
    ref_cam = next((c for c in cameras if times.get(c) is not None), None)
    if ref_cam is None:
        raise HTTPException(422, "Could not read creation_time from any camera")

    ref_ts = times[ref_cam]
    offsets: dict[str, int] = {}
    missing: list[str] = []
    for cam in cameras:
        ts = times.get(cam)
        if ts is None:
            missing.append(cam)
            continue
        # offset to add to cam's timestamps to align with reference
        # Ignore small differences (<5 min) — those are just different recording
        # start times, not clock drift. Real drift (e.g. wrong timezone) is always
        # much larger (hours).
        raw = round(ref_ts - ts)
        offsets[cam] = raw if abs(raw) >= 300 else 0

    return {"offsets": offsets, "reference": ref_cam, "missing": missing}


@router.delete("/api/jobs/{job_id}/camera-files")
async def purge_camera_files(job_id: str, data: dict = Body(default={})):
    """Delete all _autoframe/ derived files for a removed camera subfolder."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    camera = (data.get("camera") or "").strip()
    work_dir = Path(data.get("work_dir") or job.params.get("work_dir", ""))
    if not camera or not work_dir:
        raise HTTPException(400, "camera and work_dir required")

    VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".ts"}
    cam_dir = work_dir / camera
    if not cam_dir.is_dir():
        return {"deleted": 0, "stems": []}

    stems = [p.stem for p in cam_dir.iterdir() if p.suffix.lower() in VIDEO_EXT]
    auto_dir = job.auto_dir()
    deleted = 0

    for stem in stems:
        for f in (auto_dir / "frames").glob(f"{stem}-scene-*"):
            f.unlink(missing_ok=True); deleted += 1
        for f in (auto_dir / "autocut").glob(f"{stem}-scene-*.mp4"):
            f.unlink(missing_ok=True); deleted += 1
        for f in (auto_dir / "csv").glob(f"{stem}-Scenes.csv"):
            f.unlink(missing_ok=True); deleted += 1
        for f in (auto_dir / "trimmed").glob(f"{stem}-*"):
            f.unlink(missing_ok=True); deleted += 1

    # Filter scene_scores_allcam.csv
    for csv_name in ("scene_scores_allcam.csv", "scene_scores.csv"):
        scores_csv = auto_dir / csv_name
        if not scores_csv.exists():
            continue
        lines = scores_csv.read_text().splitlines()
        kept = [lines[0]] + [l for l in lines[1:] if not any(l.startswith(s + "-") for s in stems)]
        if len(kept) < len(lines):
            scores_csv.write_text("\n".join(kept) + "\n")

    # Remove from camera_sources.csv
    cam_sources = auto_dir / "camera_sources.csv"
    if cam_sources.exists():
        lines = cam_sources.read_text().splitlines()
        kept = [l for l in lines if not any(f"/{camera}/" in l or l.split(",")[0].strip() == camera for _ in [1])]
        if len(kept) < len(lines):
            cam_sources.write_text("\n".join(kept) + "\n")

    return {"deleted": deleted, "stems": stems}


@router.get("/api/jobs/{job_id}/frames")
async def job_frames(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    allcam_csv = job.auto_dir() / "scene_scores_allcam.csv"
    scores_csv = allcam_csv if allcam_csv.exists() else job.auto_dir() / "scene_scores.csv"
    # Prefer color-corrected thumbs when available (regenerated after Picture Save).
    _cc_dir    = job.auto_dir() / "frames_cc"
    frames_dir = _cc_dir if (_cc_dir / ".params_hash").exists() else job.auto_dir() / "frames"
    csv_dir    = job.auto_dir() / "csv"
    if not scores_csv.exists():
        raise HTTPException(404, "No scores yet")

    durations: dict[str, float] = {}
    csv_durations: dict[str, float] = {}
    if csv_dir.exists():
        for csv_path in csv_dir.glob("*-Scenes.csv"):
            video_prefix = csv_path.stem[:-len("-Scenes")]
            try:
                sdf = _read_scenes_csv(csv_path)
                for _, srow in sdf.iterrows():
                    snum = int(srow["Scene Number"])
                    key  = f"{video_prefix}-scene-{snum:03d}"
                    csv_durations[key] = round(float(srow["Length (seconds)"]), 2)
            except Exception:
                pass
    durations = dict(csv_durations)
    dur_cache_path = job.auto_dir() / "duration_cache.json"
    if dur_cache_path.exists():
        try:
            raw = json.loads(dur_cache_path.read_text())
            for k, v in raw.items():
                durations[k.removesuffix(".mp4")] = v
        except Exception:
            pass

    df = pd.read_csv(scores_csv).sort_values("scene")
    df = df.dropna(subset=["score"])
    if "avg_brightness" in df.columns:
        _br = pd.to_numeric(df["avg_brightness"], errors="coerce")
        df = df[_br.isna() | (_br >= 50.0)]

    cam_sources_csv = job.auto_dir() / "camera_sources.csv"
    avg_back_cam_take_sec = None
    cam_map: dict = {}
    if cam_sources_csv.exists():
        cdf = pd.read_csv(cam_sources_csv)
        cam_map = dict(zip(cdf["source"], cdf["camera"]))
        df["_source"] = df["scene"].str.replace(r"-(scene|clip)-\d+$", "", regex=True)
        df["_camera"] = df["_source"].map(cam_map).fillna("default")
        if df["_camera"].nunique() > 1:
            for _, idx in df.groupby("_camera").groups.items():
                lo, hi = df.loc[idx, "score"].min(), df.loc[idx, "score"].max()
                if hi > lo:
                    df.loc[idx, "score"] = (df.loc[idx, "score"] - lo) / (hi - lo)
                else:
                    df.loc[idx, "score"] = 1.0
        df = df.rename(columns={"_camera": "camera"}).drop(columns=["_source"])

        cam_a = str(job.params.get("cam_a") or "")
        if not cam_a:
            _cams = job.params.get("cameras") or []
            if isinstance(_cams, str):
                _cams = [c.strip() for c in _cams.split(",") if c.strip()]
            cam_a = _cams[0] if _cams else ""
        if cam_a:
            max_scene_val = float(job.params.get("max_scene") or 10)
            back_sources = {src for src, cam in cam_map.items() if cam != cam_a}
            back_takes = [
                min(dur, max_scene_val)
                for name, dur in durations.items()
                if re.sub(r"-scene-\d+$", "", name) in back_sources
            ]
            if back_takes:
                avg_back_cam_take_sec = round(sum(back_takes) / len(back_takes), 2)

    dup_scenes: set[str] = set()
    dup_path = job.auto_dir() / "scene_duplicates.json"
    if dup_path.exists():
        try:
            dup_scenes = set(json.loads(dup_path.read_text()))
        except Exception:
            pass

    file_starts: dict[str, float] = {}
    _cm = cam_map if cam_sources_csv.exists() else {}
    _cams_param = job.params.get("cameras") or []
    if isinstance(_cams_param, str):
        _cams_param = [c.strip() for c in _cams_param.split(",") if c.strip()]
    _wd = job.work_dir()

    _epoch_cache: dict[str, Optional[float]] = {}

    def _file_start_epoch(stem: str) -> Optional[float]:
        if stem in _epoch_cache:
            return _epoch_cache[stem]
        cam = _cm.get(stem)
        dirs = [_wd / cam] if cam else ([_wd / c for c in _cams_param] if _cams_param else [_wd])
        for d in dirs:
            for ext in (".mp4", ".MP4", ".mov", ".MOV"):
                p = d / (stem + ext)
                if p.exists():
                    try:
                        r = subprocess.run(
                            ["ffprobe", "-v", "quiet", "-print_format", "json",
                             "-show_format", str(p)],
                            capture_output=True, text=True, timeout=10,
                        )
                        ct = json.loads(r.stdout)["format"].get("tags", {}).get("creation_time", "")
                        if ct:
                            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                            _epoch_cache[stem] = dt.timestamp()
                            return _epoch_cache[stem]
                    except Exception:
                        pass
        _epoch_cache[stem] = None
        return None

    if csv_dir.exists():
        for _csv in csv_dir.glob("*-Scenes.csv"):
            _stem = _csv.stem[:-len("-Scenes")]
            _fstart = _file_start_epoch(_stem)
            if _fstart is None:
                continue
            try:
                _sdf = _read_scenes_csv(_csv)
                for _, _srow in _sdf.iterrows():
                    _snum = int(_srow["Scene Number"])
                    _key = f"{_stem}-scene-{_snum:03d}"
                    _secs = float(_srow.get("Start Time (seconds)", 0) or 0)
                    file_starts[_key] = round(_fstart + _secs, 1)
            except Exception:
                pass

    # CLIP-first clips: populate file_start from offset_sec column
    if "offset_sec" in df.columns:
        for _, _row in df.iterrows():
            _scene = str(_row["scene"])
            if "-clip-" not in _scene or _scene in file_starts:
                continue
            _offset = _row.get("offset_sec")
            if _offset is None or (isinstance(_offset, float) and pd.isna(_offset)):
                continue
            _src_stem = re.sub(r"-clip-\d+$", "", _scene)
            _epoch = _file_start_epoch(_src_stem)
            if _epoch is not None:
                file_starts[_scene] = round(_epoch + float(_offset), 1)

    scored_scenes = set(df["scene"].tolist())

    frames = [
        {
            "scene":      row["scene"],
            "score":      round(float(row["score"]), 4),
            "duration":   durations.get(row["scene"]),
            "camera":     row.get("camera") if "camera" in df.columns else None,
            "frame_url":  next(
                              (str(p) for p in (
                                  # CLIP-first scenes: _f0 is the peak frame — never prefer _f1 (stale step-4 extract)
                                  [
                                      frames_dir / (row["scene"] + "_f0.jpg"),
                                      frames_dir / (row["scene"] + "_f1.jpg"),
                                      frames_dir / (row["scene"] + ".jpg"),
                                  ] if "-clip-" in row["scene"] else [
                                      frames_dir / (row["scene"] + "_f1.jpg"),
                                      frames_dir / (row["scene"] + "_f0.jpg"),
                                      frames_dir / (row["scene"] + ".jpg"),
                                  ]
                              ) if p.exists()),
                              None
                          ),
            "file_start":     file_starts.get(row["scene"]),
            "duplicate":      row["scene"] in dup_scenes,
            "avg_brightness": round(float(row["avg_brightness"]), 1) if "avg_brightness" in df.columns and pd.notna(row.get("avg_brightness")) else None,
        }
        for _, row in df.iterrows()
    ]


    frames = [f for f in frames if f["frame_url"] is not None]

    return {"frames": frames, "back_cam": {"avg_take_sec": avg_back_cam_take_sec}}


@router.get("/api/jobs/{job_id}/result")
async def job_result(job_id: str):
    import pipeline as _pipeline
    from webapp.routers.youtube import _read_yt_urls
    from webapp.routers.instagram import _read_ig_urls

    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    work_dir = job.work_dir()
    files = {}

    def _add(p: Path):
        if p.exists():
            meta_path = p.with_suffix(".meta.json")
            meta: dict = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    pass
            ncs_attr = None
            if meta.get("ncs") and meta.get("music"):
                ncs_attr = Path(meta["music"]).stem
            preview = p.with_name(p.stem + "_preview.mp4")
            music_path = meta.get("music")
            files[p.name] = {
                "url":          str(p),
                "preview_url":  str(preview) if preview.exists() else None,
                "size_mb":      round(p.stat().st_size / 1_048_576, 1),
                "duration_sec": _probe_duration(p),
                "is_ncs":       bool(meta.get("ncs")),
                "ncs_attr":     ncs_attr,
                "music":        Path(music_path).name if music_path else None,
            }

    def _ver(p: Path) -> int:
        m = re.search(r'[-_]v(\d+)$', p.stem)
        return int(m.group(1)) if m else 0

    auto_dir = work_dir / "_autoframe"
    seen: set[str] = set()
    for pat in ("*-v*.mp4", "*_v*.mp4"):
        for p in sorted(work_dir.glob(pat), key=_ver, reverse=True):
            if "_preview" in p.stem:
                continue
            if p.name not in seen:
                seen.add(p.name)
                _add(p)

    yt_urls = _read_yt_urls(auto_dir)
    for name in files:
        if name in yt_urls:
            files[name]["yt_url"] = yt_urls[name]

    ig_urls = _read_ig_urls(auto_dir)
    for name in files:
        if name in ig_urls:
            files[name]["ig_url"] = ig_urls[name]

    return files


_preview_locks: dict[str, asyncio.Lock] = {}


@router.post("/api/jobs/{job_id}/preview")
async def generate_preview(job_id: str, filename: str = Query(...)):
    """Generate _preview.mp4 for a result file using NVENC (falls back to libx264)."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    work_dir = job.work_dir()
    src = work_dir / filename
    if not src.exists():
        raise HTTPException(404, f"File not found: {filename}")
    if "/short" in filename.lower() or "short" in Path(filename).stem.lower():
        raise HTTPException(400, "Preview not supported for shorts")

    preview = src.with_name(src.stem + "_preview.mp4")
    lock_key = str(preview)
    if lock_key not in _preview_locks:
        _preview_locks[lock_key] = asyncio.Lock()

    async with _preview_locks[lock_key]:
        if preview.exists():
            return {"preview_url": str(preview)}

        # Detect NVENC availability
        ffmpeg = "ffmpeg"
        enc_proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-encoders",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        enc_out, _ = await enc_proc.communicate()
        use_nvenc = b"h264_nvenc" in enc_out

        if use_nvenc:
            cmd = [
                ffmpeg, "-y", "-hwaccel", "cuda",
                "-i", str(src),
                "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                       "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "h264_nvenc", "-rc", "vbr", "-cq", "23",
                "-b:v", "15M", "-maxrate", "20M", "-bufsize", "40M",
                "-preset", "p4",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(preview),
            ]
        else:
            cmd = [
                ffmpeg, "-y",
                "-i", str(src),
                "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                       "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "libx264", "-crf", "20", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(preview),
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        ret = await proc.wait()
        if ret != 0 or not preview.exists():
            raise HTTPException(500, "Preview generation failed")

    return {"preview_url": str(preview)}


def _get_photo_ts(path: Path) -> float:
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        import datetime as _dt
        img = Image.open(path)
        exif = img._getexif()
        if exif:
            for tag, val in exif.items():
                if TAGS.get(tag) == 'DateTimeOriginal':
                    return _dt.datetime.strptime(val, '%Y:%m:%d %H:%M:%S').timestamp()
    except Exception:
        pass
    return path.stat().st_mtime


def _make_photo_thumb(src: Path, dst: Path):
    try:
        from PIL import Image
        img = Image.open(src)
        img.thumbnail((320, 240), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, 'JPEG', quality=80)
    except Exception:
        pass


@router.get("/api/jobs/{job_id}/photos")
async def get_photos(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    cfg = read_job_config(job.work_dir())
    photos_dir = job.params.get("photos_dir") or cfg.get("photos_dir") or str(job.work_dir() / "photos")
    if not photos_dir:
        return {"photos": []}
    pd = Path(photos_dir)
    if not pd.exists() or not pd.is_dir():
        return {"photos": []}
    exts = {'.jpg', '.jpeg', '.png', '.heic', '.webp'}
    photo_files = [p for p in pd.iterdir() if p.suffix.lower() in exts]
    thumb_dir = job.auto_dir() / "photo_thumbs"
    selected = set(job.params.get("selected_photos") or [])
    result = []
    for p in photo_files:
        ts = _get_photo_ts(p)
        thumb = thumb_dir / p.name
        if not thumb.exists():
            _make_photo_thumb(p, thumb)
        result.append({
            "path":      str(p),
            "filename":  p.name,
            "timestamp": ts,
            "thumb_url": f"/api/file?path={thumb}",
            "selected":  str(p) in selected,
        })
    result.sort(key=lambda x: x["timestamp"])
    return {"photos": result}


@router.get("/api/jobs/{job_id}/suggest-clip-params")
async def suggest_clip_params(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    work_dir = job.work_dir()
    VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".ts"}
    _gcfg = configparser.ConfigParser()
    _gcfg.read(str(APP_DIR / "config.ini"))
    ffprobe_bin = _gcfg.get("paths", "ffprobe", fallback="ffprobe")

    async def _duration(f: Path) -> float:
        try:
            proc = await asyncio.create_subprocess_exec(
                ffprobe_bin, "-v", "quiet", "-print_format", "json",
                "-show_entries", "format=duration",
                str(f),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            info = json.loads(out)
            return float(info.get("format", {}).get("duration", 0))
        except Exception:
            return 0.0

    video_files = [
        f for f in work_dir.rglob("*")
        if f.suffix.lower() in VIDEO_EXT and "_autoframe" not in f.parts
    ]
    if not video_files:
        raise HTTPException(422, "No video files found in work_dir")

    durations = await asyncio.gather(*[_duration(f) for f in video_files])
    total_sec = sum(durations)
    if total_sec <= 0:
        raise HTTPException(422, "Could not read durations from video files")

    clip_dur = min(12, max(6, round(total_sec / 180)))
    min_gap  = min(120, max(20, round(total_sec / 30 / 5) * 5))
    interval = max(3, clip_dur // 3)

    return {
        "clip_dur":  clip_dur,
        "interval":  interval,
        "min_gap":   min_gap,
        "total_sec": round(total_sec),
    }


@router.get("/api/suggest-clip-params")
async def suggest_clip_params_by_dir(work_dir: str = Query(...)):
    wd = Path(work_dir)
    if not wd.is_dir():
        raise HTTPException(404, "Directory not found")

    VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".ts"}
    _gcfg = configparser.ConfigParser()
    _gcfg.read(str(APP_DIR / "config.ini"))
    ffprobe_bin = _gcfg.get("paths", "ffprobe", fallback="ffprobe")

    async def _duration(f: Path) -> float:
        try:
            proc = await asyncio.create_subprocess_exec(
                ffprobe_bin, "-v", "quiet", "-print_format", "json",
                "-show_entries", "format=duration",
                str(f),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            info = json.loads(out)
            return float(info.get("format", {}).get("duration", 0))
        except Exception:
            return 0.0

    video_files = [
        f for f in wd.rglob("*")
        if f.suffix.lower() in VIDEO_EXT and "_autoframe" not in f.parts
    ]
    if not video_files:
        raise HTTPException(422, "No video files found in directory")

    durations = await asyncio.gather(*[_duration(f) for f in video_files])
    total_sec = sum(durations)
    if total_sec <= 0:
        raise HTTPException(422, "Could not read durations from video files")

    clip_dur = min(12, max(6, round(total_sec / 180)))
    min_gap  = min(120, max(20, round(total_sec / 30 / 5) * 5))
    interval = max(3, clip_dur // 3)

    return {
        "clip_dur":  clip_dur,
        "interval":  interval,
        "min_gap":   min_gap,
        "total_sec": round(total_sec),
    }


@router.delete("/api/jobs/{job_id}/result-file")
async def delete_result_file(job_id: str, filename: str = Query(...)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    work_dir = job.work_dir()
    candidates = [work_dir / filename, work_dir / "_autoframe" / filename]
    for p in candidates:
        resolved = p.resolve()
        if resolved.parent.resolve() in (work_dir.resolve(), (work_dir / "_autoframe").resolve()):
            if resolved.exists():
                resolved.unlink()
                for _sidecar in (
                    resolved.with_name(resolved.stem + "_preview.mp4"),
                    resolved.with_suffix(".meta.json"),
                ):
                    if _sidecar.exists():
                        _sidecar.unlink()
                return {"ok": True}
    raise HTTPException(404, f"File not found: {filename}")
