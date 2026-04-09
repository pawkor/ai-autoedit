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
from pydantic import BaseModel

from webapp.state import (
    APP_DIR,
    SCRIPT_DIR,
    JOBS_DIR,
    BROWSE_ROOT,
    in_browse_root,
    jobs,
    job_semaphore,
    shorts_semaphore,
    _threshold_searches,
    _threshold_tasks,
    _proxy_tasks,
    _proxy_status,
    _LogList,
    Job,
    _run_job,
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
    "music_dir":    ("music", "dir"),
    "positive":     ("clip_prompts", "positive"),
    "negative":     ("clip_prompts", "negative"),
    "yt_title":     ("youtube", "title"),
    "yt_desc":      ("youtube", "description"),
    "shorts_text":         ("shorts",  "text_overlays"),
    "shorts_multicam":     ("shorts",  "multicam"),
    "shorts_ncs":          ("shorts",  "ncs_music"),
    "shorts_crop_offsets": ("shorts",  "crop_x_offsets"),
}


def read_job_config(work_dir: Path) -> dict:
    global_cp = configparser.ConfigParser()
    global_cp.read(str(APP_DIR / "config.ini"))

    local_cp = configparser.ConfigParser()
    cfg_path = work_dir / "config.ini"
    if cfg_path.exists():
        local_cp.read(str(cfg_path))

    result = {}
    for field, (section, key) in _JOB_CONFIG_MAP.items():
        for cp in (local_cp, global_cp):
            try:
                raw = cp.get(section, key)
                if field in ("no_intro", "no_music"):
                    result[field] = raw.strip().lower() in ("true", "1", "yes")
                elif field == "sd_min_scene":
                    v = float(raw.rstrip('s').strip())
                    result[field] = f"{int(v) if v == int(v) else v}s"
                elif field in ("threshold", "max_scene", "per_file", "target_minutes", "sd_threshold"):
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
    return result


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
        v = params.get(field)
        if v is None or v == "" or v == []:
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
    if cam_offsets and isinstance(cam_offsets, dict):
        offsets_update = {"cam_offsets": {k: str(int(v)) for k, v in cam_offsets.items() if v is not None}}
        update_config_ini(work_dir / "config.ini", offsets_update)


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
    sd_threshold:  Optional[float] = None
    sd_min_scene:  Optional[str]   = None


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
        if job.params.get("shorts_text"):    cmd.append("--text")
        if job.params.get("shorts_multicam"): cmd.append("--multicam")
        if job.params.get("shorts_ncs"):  cmd.append("--ncs")
        if version:                       cmd += ["--version", version]
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


async def _run_shorts(job: Job, count: int = 1):
    import pipeline as _pipeline
    job.status = "running"
    job.phase  = "shorts"
    await job.broadcast({"type": "status", "status": "running", "phase": "shorts"})
    job.save()
    try:
        work_dir = Path(job.params["work_dir"])
        base     = _pipeline._output_name(work_dir)
        existing_nums = [
            int(m.group(1))
            for p in work_dir.glob(f"{base}-short_v*.mp4")
            if (m := re.search(r"-short_v(\d+)\.mp4$", p.name))
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
            lock       = asyncio.Lock()

            async def _one(i: int) -> bool:
                nonlocal done_count
                async with batch_sem:
                    async with shorts_semaphore:
                        result = await _run_one_short(job, i, count, versions[i - 1])
                async with lock:
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

        if ok:
            job.status = "done"
            job.phase  = "done"
        else:
            job.log.append("ERROR: one or more shorts failed")
            job.status = "failed"
            job.phase  = "failed"
    except asyncio.CancelledError:
        job.log.append("[shorts cancelled]")
        job.status = "killed"
        job.phase  = "failed"
    except Exception as exc:
        job.log.append(f"ERROR: {exc}")
        job.status = "failed"
        job.phase  = "failed"
    job.ended_at = time.time()
    if _prom_ok:
        from webapp.state import _prom_jobs_total, _prom_job_duration
        _prom_jobs_total.labels(phase="shorts", status=job.status).inc()
        _prom_job_duration.labels(phase="shorts").observe(job.ended_at - job.started_at)
    job.save()
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
    mp4s = list(work_dir.glob("highlight*.mp4"))
    if mp4s:
        job.started_at = min(p.stat().st_mtime for p in mp4s)
        job.ended_at   = max(p.stat().st_mtime for p in mp4s)
    jobs[job_id] = job
    job.save()
    return {"id": job_id}


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
        existing_idle.params      = d
        existing_idle.log         = _LogList(JOBS_DIR / f"{existing_idle.id}.log")
        existing_idle.status      = "queued"
        existing_idle.phase       = "analyzing" if analyze_only else "rendering"
        existing_idle.started_at  = time.time()
        existing_idle.ended_at    = None
        existing_idle.analyze_result = None
        existing_idle.selected_track = None
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
            "id":         j.id,
            "status":     j.status,
            "phase":      j.phase,
            "work_dir":   j.params["work_dir"],
            "started_at": j.started_at,
            "ended_at":   j.ended_at,
        }
        for j in sorted(jobs.values(), key=lambda j: -j.created_at)
    ]


@router.post("/api/jobs/{job_id}/rerun")
async def rerun_job(job_id: str, params: JobParams):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        raise HTTPException(409, "Job is already running or queued")

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

    job.params         = d
    job.log            = _LogList(JOBS_DIR / f"{job.id}.log")
    job.status         = "queued"
    job.phase          = "analyzing"
    job.analyze_result = None
    job.selected_track = None
    job.started_at     = time.time()
    job.ended_at       = None
    job.process        = None
    job._task          = None
    job.save()

    job._task = asyncio.create_task(_run_job(job, analyze_only=True))
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


@router.post("/api/jobs/{job_id}/start-proxy")
async def start_proxy(job_id: str):
    import pipeline as _pipeline
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    existing = _proxy_tasks.get(job_id)
    if existing and not existing.done():
        return {"ok": True, "already_running": True}

    _proxy_status[job_id] = {"done": False, "total": 0, "finished": 0, "current_file": ""}

    async def _run():
        try:
            async for update in _pipeline.create_proxy(job.params, job.work_dir()):
                _proxy_status[job_id] = update
        except asyncio.CancelledError:
            _proxy_status[job_id] = {"done": True, "cancelled": True,
                                     "total": _proxy_status.get(job_id, {}).get("total", 0),
                                     "finished": _proxy_status.get(job_id, {}).get("finished", 0)}
        except Exception as e:
            _proxy_status[job_id] = {"done": True, "error": str(e)}
        finally:
            _proxy_tasks.pop(job_id, None)

    task = asyncio.create_task(_run())
    _proxy_tasks[job_id] = task
    return {"ok": True}


@router.get("/api/jobs/{job_id}/proxy-status")
async def get_proxy_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    st = _proxy_status.get(job_id)
    if st is None:
        return {"done": False, "total": 0, "finished": 0, "current_file": "", "not_started": True}
    return st


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
    if _actual_scenes is not None:
        result["actual_selected_scenes"] = _actual_scenes
    if _actual_dur is not None:
        result["actual_duration_sec"] = _actual_dur
    if _actual_thr is not None:
        result["actual_threshold"] = _actual_thr
    if result:
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
        return result
    raise HTTPException(404, "No analysis data available")


@router.post("/api/jobs/{job_id}/render")
async def render_job(job_id: str, params: RenderParams):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        raise HTTPException(409, "Job is already running or queued")

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

    job._task = asyncio.create_task(_run_job(job, analyze_only=False, selected_track=track))
    return {"id": job_id, "phase": "rendering"}


@router.post("/api/jobs/{job_id}/render-short")
async def render_short(job_id: str, data: dict = Body(default={})):
    count = max(1, min(int(data.get("count", 1)), 20))
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        raise HTTPException(409, "Job is already running or queued")
    job.log.append("")
    job.log.append(f"── Render Short{'s ×' + str(count) if count > 1 else ''} ──────────────────────────")
    job.status = "queued"
    job.phase  = "shorts"
    job.ended_at = None
    job.save()
    job._task = asyncio.create_task(_run_shorts(job, count=count))
    return {"id": job_id, "phase": "shorts", "count": count}


@router.delete("/api/jobs/{job_id}")
async def kill_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status == "running":
        if job._task and not job._task.done():
            job._task.cancel()
        elif job.process:
            try:
                os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
            except Exception:
                job.process.terminate()
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
    allowed = {"threshold", "max_scene", "per_file", "music_dir", "min_gap_sec", "music_files"}
    for k, v in data.items():
        if k in allowed:
            job.params[k] = v
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


@router.get("/api/jobs/{job_id}/frames")
async def job_frames(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    scores_csv = job.auto_dir() / "scene_scores.csv"
    frames_dir = job.auto_dir() / "frames"
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

    cam_sources_csv = job.auto_dir() / "camera_sources.csv"
    avg_back_cam_take_sec = None
    cam_map: dict = {}
    if cam_sources_csv.exists():
        cdf = pd.read_csv(cam_sources_csv)
        cam_map = dict(zip(cdf["source"], cdf["camera"]))
        df["_source"] = df["scene"].str.replace(r"-scene-\d+$", "", regex=True)
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
    if csv_dir.exists():
        _cm = cam_map if cam_sources_csv.exists() else {}
        _cams_param = job.params.get("cameras") or []
        if isinstance(_cams_param, str):
            _cams_param = [c.strip() for c in _cams_param.split(",") if c.strip()]
        _wd = job.work_dir()

        def _file_start_epoch(stem: str) -> Optional[float]:
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
                                return dt.timestamp()
                        except Exception:
                            pass
            return None

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

    frames = [
        {
            "scene":      row["scene"],
            "score":      round(float(row["score"]), 4),
            "duration":   durations.get(row["scene"]),
            "camera":     row.get("camera") if "camera" in df.columns else None,
            "frame_url":  str(frames_dir / (row['scene'] + '.jpg'))
                          if (frames_dir / (row["scene"] + ".jpg")).exists() else None,
            "file_start": file_starts.get(row["scene"]),
            "duplicate":  row["scene"] in dup_scenes,
        }
        for _, row in df.iterrows()
    ]
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
        m = re.search(r'_v(\d+)$', p.stem)
        return int(m.group(1)) if m else 0

    auto_dir = work_dir / "_autoframe"
    out_name = _pipeline._output_name(work_dir)
    seen: set[str] = set()
    for pat in (f"{out_name}_v*.mp4", f"{out_name}-short_v*.mp4"):
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
                # Also remove preview if it exists
                preview = resolved.with_name(resolved.stem + "_preview.mp4")
                if preview.exists():
                    preview.unlink()
                return {"ok": True}
    raise HTTPException(404, f"File not found: {filename}")
