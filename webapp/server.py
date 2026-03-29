#!/usr/bin/env python3
"""
autoframe web UI — FastAPI backend
Run: uvicorn webapp.server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import configparser
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import pandas as pd

SCRIPT_DIR  = Path(__file__).resolve().parent.parent
WEBAPP_DIR  = Path(__file__).resolve().parent
STATIC_DIR  = WEBAPP_DIR / "static"
JOBS_DIR    = WEBAPP_DIR / "jobs"
BROWSE_ROOT = Path.home()

JOBS_DIR.mkdir(exist_ok=True)

# ── Webapp config (webapp/config.ini, does not touch main config.ini) ──────────

def _load_wcfg() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read(str(WEBAPP_DIR / "config.ini"))
    return cp

def wcfg(key: str, default: str) -> str:
    return _load_wcfg().get("webapp", key, fallback=default)

def save_wcfg(data: dict):
    cp = _load_wcfg()
    if "webapp" not in cp:
        cp["webapp"] = {}
    for k, v in data.items():
        cp["webapp"][k] = str(v)
    with open(WEBAPP_DIR / "config.ini", "w") as f:
        cp.write(f)

# ── Job ────────────────────────────────────────────────────────────────────────

class Job:
    def __init__(self, job_id: str, params: dict):
        self.id          = job_id
        self.params      = params
        self.status      = "queued"   # queued | running | done | failed | killed
        self.log: list[str] = []
        self.process: Optional[asyncio.subprocess.Process] = None
        self.started_at  = time.time()
        self.ended_at: Optional[float] = None
        self.subscribers: set[WebSocket] = set()

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "params":     self.params,
            "status":     self.status,
            "log":        self.log,
            "started_at": self.started_at,
            "ended_at":   self.ended_at,
        }

    def save(self):
        path = JOBS_DIR / f"{self.id}.json"
        path.write_text(json.dumps(self.to_dict()))

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        j = cls(data["id"], data["params"])
        j.status     = data["status"]
        j.log        = data.get("log", [])
        j.started_at = data.get("started_at", time.time())
        j.ended_at   = data.get("ended_at")
        return j

    def work_dir(self) -> Path:
        return Path(self.params["work_dir"])

    def auto_dir(self) -> Path:
        return self.work_dir() / self.params.get("work_subdir", "_autoframe")

    async def broadcast(self, msg: dict):
        dead = set()
        for ws in self.subscribers:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.add(ws)
        self.subscribers -= dead


jobs: dict[str, Job] = {}
job_semaphore: asyncio.Semaphore = asyncio.Semaphore(1)

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="autoframe")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def startup():
    global job_semaphore
    max_c = int(wcfg("max_concurrent_jobs", "1"))
    job_semaphore = asyncio.Semaphore(max_c)

    # Load persisted jobs (sorted by mtime so sidebar order is stable)
    for f in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            data = json.loads(f.read_text())
            job = Job.from_dict(data)
            # Jobs that were interrupted during previous run
            if job.status in ("running", "queued"):
                job.status = "failed"
                job.log.append("[server restarted — job interrupted]")
                job.ended_at = job.ended_at or time.time()
                job.save()
            jobs[job.id] = job
        except Exception:
            pass

    asyncio.create_task(_stats_broadcaster())


# ── System stats ───────────────────────────────────────────────────────────────

_gpu_available: Optional[bool] = None

def _get_stats() -> dict:
    global _gpu_available
    cpu  = psutil.cpu_percent(interval=None)
    mem  = psutil.virtual_memory()
    stats = {
        "cpu_pct":       round(cpu, 1),
        "ram_used_gb":   round(mem.used  / 1e9, 1),
        "ram_total_gb":  round(mem.total / 1e9, 1),
        "ram_pct":       mem.percent,
        "gpu":           None,
        "running_jobs":  sum(1 for j in jobs.values() if j.status == "running"),
        "queued_jobs":   sum(1 for j in jobs.values() if j.status == "queued"),
    }

    if _gpu_available is False:
        return stats

    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2
        )
        if r.returncode == 0:
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            vused, vtot = int(parts[1]), int(parts[2])
            stats["gpu"] = {
                "pct":          int(parts[0]),
                "vram_used_mb": vused,
                "vram_total_mb": vtot,
                "vram_pct":     round(vused / vtot * 100) if vtot else 0,
            }
            _gpu_available = True
        else:
            _gpu_available = False
    except Exception:
        _gpu_available = False

    return stats


_stats_subscribers: set[WebSocket] = set()

async def _stats_broadcaster():
    """Continuously compute stats and push to all /ws/stats subscribers."""
    psutil.cpu_percent(interval=None)   # prime the counter
    while True:
        await asyncio.sleep(2)
        if not _stats_subscribers:
            continue
        try:
            data = json.dumps(_get_stats())
        except Exception:
            continue
        dead = set()
        for ws in _stats_subscribers:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            _stats_subscribers.discard(ws)


# ── Job runner ─────────────────────────────────────────────────────────────────

def _build_cmd(params: dict) -> list[str]:
    cmd = ["/bin/bash", str(SCRIPT_DIR / "autoframe.sh")]

    # Always pass --threshold explicitly so THRESHOLD_EXPLICIT=1 in bash,
    # which skips the interactive read prompt entirely.
    threshold = params.get("threshold")
    if threshold is None:
        cfg = read_job_config(Path(params.get("work_dir", ".")))
        threshold = cfg.get("threshold", 0.148)
    cmd += ["--threshold", str(threshold)]

    mapping = [
        ("max_scene",     "--max-scene"),
        ("per_file",      "--per-file"),
        ("cam_a",         "--cam-a"),
        ("cam_b",         "--cam-b"),
        ("music_genre",   "--music-genre"),
        ("music_artist",  "--music-artist"),
    ]
    for key, flag in mapping:
        v = params.get(key)
        if v is not None and v != "" and v is not False:
            cmd += [flag, str(v)]
    title = params.get("title")
    if title:
        # autoframe.sh uses echo -e "$TITLE" to expand \n; pass literal \n escape
        cmd += ["--title", str(title).replace("\n", "\\n")]
    if params.get("music_dir"):
        cmd += ["--music", str(params["music_dir"])]
    music_files = params.get("music_files")
    if music_files:
        cmd += ["--music-files", ",".join(music_files)]
    if params.get("no_intro"):  cmd.append("--no-intro")
    if params.get("no_music"):  cmd.append("--no-music")
    return cmd


async def _run_job(job: Job):
    async with job_semaphore:
        job.status = "running"
        job.started_at = time.time()
        await job.broadcast({"type": "status", "status": "running"})
        job.save()

        try:
            proc = await asyncio.create_subprocess_exec(
                *_build_cmd(job.params),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(job.work_dir()),
                start_new_session=True,
            )
            job.process = proc

            async for raw in proc.stdout:
                text = raw.decode("utf-8", errors="replace")
                # \r-separated chunks = ffmpeg progress bar updates; send each separately
                for part in text.split('\r'):
                    line = part.rstrip('\n').rstrip()
                    if line:
                        job.log.append(line)
                        await job.broadcast({"type": "log", "line": line})

            await proc.wait()
            job.status = "done" if proc.returncode == 0 else "failed"
        except Exception as e:
            job.log.append(f"ERROR: {e}")
            job.status = "failed"

        job.ended_at = time.time()
        job.save()
        await job.broadcast({"type": "status", "status": job.status})


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/settings")
async def get_settings():
    return {
        "max_concurrent_jobs": int(wcfg("max_concurrent_jobs", "1")),
        "port":                int(wcfg("port", "8000")),
    }


@app.put("/api/settings")
async def put_settings(data: dict):
    save_wcfg(data)
    return {"ok": True, "note": "restart server for max_concurrent_jobs to take effect"}


@app.post("/api/about")
async def generate_about(data: dict):
    work_dir    = data.get("work_dir", "").strip()
    description = data.get("description", "").strip()
    if not description:
        raise HTTPException(400, "description required")
    if work_dir and not Path(work_dir).is_dir():
        raise HTTPException(400, f"work_dir not found: {work_dir}")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(SCRIPT_DIR / "generate_config.py"), description,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=work_dir or str(SCRIPT_DIR),
    )
    out, _ = await proc.communicate()
    return {
        "ok":     proc.returncode == 0,
        "output": out.decode("utf-8", errors="replace"),
    }


@app.post("/api/music-rebuild")
async def music_rebuild(payload: dict):
    music_dir = payload.get("dir", "")
    if not music_dir:
        raise HTTPException(400, "dir required")
    d = Path(music_dir).expanduser().resolve()
    if not d.is_dir():
        raise HTTPException(404, "Directory not found")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(SCRIPT_DIR / "music_index.py"), str(d),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return {"ok": proc.returncode == 0, "output": out.decode("utf-8", errors="replace")}


@app.get("/api/music-files")
async def music_files_endpoint(dir: str = Query(...)):
    d = Path(dir).expanduser().resolve()
    idx = d / "index.json"
    if idx.exists():
        tracks = json.loads(idx.read_text())
        return sorted(tracks, key=lambda t: t.get("title", "").lower())
    # Fallback: scan mp3s without index
    return sorted(
        [{"file": str(f), "title": f.stem, "genre": "", "duration": 0, "bpm": 0, "energy_norm": 0}
         for f in d.glob("*.mp3")],
        key=lambda t: t["title"].lower()
    )


@app.get("/api/browse")
async def browse(path: str = Query(default=None)):
    root = Path(path).resolve() if path else BROWSE_ROOT
    if not str(root).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Outside allowed root")
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    return {
        "path":   str(root),
        "parent": str(root.parent) if root != BROWSE_ROOT else None,
        "entries": [
            {
                "name":           e.name,
                "path":           str(e),
                "is_dir":         e.is_dir(),
                "has_mp4":        e.is_dir() and any(e.glob("*.mp4")),
                "has_autoframe":  e.is_dir() and (e / "_autoframe").exists(),
            }
            for e in entries
        ],
    }


class JobParams(BaseModel):
    work_dir:     str
    threshold:    Optional[float] = None
    max_scene:    Optional[float] = None
    per_file:     Optional[float] = None
    title:        Optional[str]   = None
    cam_a:        Optional[str]   = None
    cam_b:        Optional[str]   = None
    no_intro:     bool = False
    no_music:     bool = False
    music_genre:  Optional[str] = None
    music_artist: Optional[str] = None
    work_subdir:  str = "_autoframe"


# ── Per-directory config.ini helpers ──────────────────────────────────────────
# Maps JobParams fields → (section, key) in config.ini
_JOB_CONFIG_MAP = {
    "threshold":    ("scene_selection", "threshold"),
    "max_scene":    ("scene_selection", "max_scene_sec"),
    "per_file":     ("scene_selection", "max_per_file_sec"),
    "cam_a":        ("job", "cam_a"),
    "cam_b":        ("job", "cam_b"),
    "title":        ("job", "title"),
    "no_intro":     ("job", "no_intro"),
    "no_music":     ("job", "no_music"),
    "music_genre":  ("job", "music_genre"),
    "music_artist": ("job", "music_artist"),
}


def read_job_config(work_dir: Path) -> dict:
    """Read job-relevant keys; work_dir/config.ini overrides global config.ini."""
    global_cp = configparser.ConfigParser()
    global_cp.read(str(SCRIPT_DIR / "config.ini"))

    local_cp = configparser.ConfigParser()
    cfg_path = work_dir / "config.ini"
    if cfg_path.exists():
        local_cp.read(str(cfg_path))

    result = {}
    for field, (section, key) in _JOB_CONFIG_MAP.items():
        for cp in (local_cp, global_cp):   # local wins over global
            try:
                raw = cp.get(section, key)
                if field in ("no_intro", "no_music"):
                    result[field] = raw.strip().lower() in ("true", "1", "yes")
                elif field in ("threshold", "max_scene", "per_file"):
                    result[field] = float(raw)
                else:
                    # Restore \n escapes to actual newlines for display in textarea
                    result[field] = raw.strip().replace("\\n", "\n")
                break
            except (configparser.NoSectionError, configparser.NoOptionError):
                continue
    return result


def update_config_ini(cfg_path: Path, updates: dict[str, dict[str, str]]):
    """Update specific section/key pairs in config.ini preserving all other content.

    updates: {section: {key: value_str}}
    Adds missing sections/keys at end of file; never removes existing lines.
    """
    content = cfg_path.read_text() if cfg_path.exists() else ""
    lines = content.splitlines()

    current_section = None
    section_end: dict[str, int] = {}   # section → index of last non-blank line
    key_line: dict[tuple, int] = {}    # (section, key) → line index

    for i, line in enumerate(lines):
        m = re.match(r'^\[(\w+)\]', line.strip())
        if m:
            current_section = m.group(1)
        elif current_section:
            km = re.match(r'^(\w+)\s*=', line.strip())
            if km:
                key_line[(current_section, km.group(1))] = i
                section_end[current_section] = i

    # Apply updates in-place where key already exists
    result = list(lines)
    appended: dict[str, list[str]] = {}   # section → lines to append

    for section, kvs in updates.items():
        for key, value in kvs.items():
            if (section, key) in key_line:
                result[key_line[(section, key)]] = f"{key} = {value}"
            else:
                appended.setdefault(section, []).append(f"{key} = {value}")

    # Append missing keys after the last key of their section,
    # or add a new section at the end
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
    """Persist form params back to work_dir/config.ini."""
    updates: dict[str, dict[str, str]] = {}
    for field, (section, key) in _JOB_CONFIG_MAP.items():
        v = params.get(field)
        if v is None or v == "":
            continue
        sv = str(v).lower() if isinstance(v, bool) else str(v)
        # INI files can't have literal newlines in values — store as \n escape
        sv = sv.replace("\n", "\\n")
        updates.setdefault(section, {})[key] = sv

    if updates:
        update_config_ini(work_dir / "config.ini", updates)


@app.get("/api/job-config")
async def get_job_config(dir: str = Query(...)):
    """Return job-relevant values from work_dir/config.ini to pre-fill the form."""
    work_dir = Path(dir).resolve()
    if not str(work_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    return read_job_config(work_dir)


def _resolve_params(d: dict, work_dir: Path) -> dict:
    """Fill None scene-selection values from config.ini (work_dir then global), then hardcoded defaults."""
    cfg_chain = [read_job_config(work_dir)]
    global_cp = configparser.ConfigParser()
    global_cp.read(str(SCRIPT_DIR / "config.ini"))
    def _gf(section, key, fallback):
        try:    return float(global_cp.get(section, key))
        except: return fallback

    if d.get("threshold") is None:
        d["threshold"] = cfg_chain[0].get("threshold") or _gf("scene_selection", "threshold", 0.148)
    if d.get("max_scene") is None:
        d["max_scene"] = cfg_chain[0].get("max_scene") or _gf("scene_selection", "max_scene_sec", 10)
    if d.get("per_file") is None:
        d["per_file"]  = cfg_chain[0].get("per_file")  or _gf("scene_selection", "max_per_file_sec", 45)
    return d


@app.post("/api/jobs")
async def create_job(params: JobParams):
    work_dir = Path(params.work_dir).resolve()
    if not work_dir.is_dir():
        raise HTTPException(400, f"Directory not found: {work_dir}")

    d = _resolve_params(params.model_dump(), work_dir)
    d["work_dir"] = str(work_dir)

    save_job_config(work_dir, d)

    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id, d)
    jobs[job_id] = job
    job.save()

    asyncio.create_task(_run_job(job))
    return {"id": job_id}


@app.get("/api/jobs")
async def list_jobs():
    return [
        {
            "id":         j.id,
            "status":     j.status,
            "work_dir":   j.params["work_dir"],
            "started_at": j.started_at,
            "ended_at":   j.ended_at,
        }
        for j in sorted(jobs.values(), key=lambda j: -j.started_at)
    ]


@app.post("/api/jobs/{job_id}/rerun")
async def rerun_job(job_id: str, params: JobParams):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        raise HTTPException(409, "Job is already running or queued")

    work_dir = Path(params.work_dir).resolve()
    if not work_dir.is_dir():
        raise HTTPException(400, f"Directory not found: {work_dir}")

    d = _resolve_params(params.model_dump(), work_dir)
    d["work_dir"] = str(work_dir)
    save_job_config(work_dir, d)

    # Reset job in-place
    job.params    = d
    job.log       = []
    job.status    = "queued"
    job.started_at = time.time()
    job.ended_at  = None
    job.process   = None
    job.save()

    asyncio.create_task(_run_job(job))
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
async def kill_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.process and job.status == "running":
        try:
            os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
        except Exception:
            job.process.terminate()
        job.status = "killed"
        job.ended_at = time.time()
        job.save()
        await job.broadcast({"type": "status", "status": "killed"})
    return {"ok": True}


@app.post("/api/jobs/{job_id}/remove")
async def remove_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    # Kill first if still running
    if job.process and job.status == "running":
        try:
            os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
        except Exception:
            job.process.terminate()
    # Remove from memory
    jobs.pop(job_id, None)
    # Delete persisted file
    p = JOBS_DIR / f"{job_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.websocket("/ws/stats")   # must be declared before /ws/{job_id}
async def stats_ws(websocket: WebSocket):
    await websocket.accept()
    _stats_subscribers.add(websocket)
    try:
        await websocket.send_text(json.dumps(_get_stats()))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _stats_subscribers.discard(websocket)


@app.websocket("/ws/{job_id}")
async def job_ws(websocket: WebSocket, job_id: str):
    job = jobs.get(job_id)
    if not job:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    for line in job.log:
        await websocket.send_text(json.dumps({"type": "log", "line": line}))
    await websocket.send_text(json.dumps({"type": "status", "status": job.status}))

    if job.status not in ("running", "queued"):
        await websocket.close()
        return

    job.subscribers.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        job.subscribers.discard(websocket)



@app.get("/api/jobs/{job_id}/frames")
async def job_frames(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    scores_csv = job.auto_dir() / "scene_scores.csv"
    frames_dir = job.auto_dir() / "frames"
    csv_dir    = job.auto_dir() / "csv"
    if not scores_csv.exists():
        raise HTTPException(404, "No scores yet")

    # Build scene-duration lookup from per-video Scenes CSVs
    durations: dict[str, float] = {}
    if csv_dir.exists():
        for csv_path in csv_dir.glob("*-Scenes.csv"):
            video_prefix = csv_path.stem[:-len("-Scenes")]
            try:
                sdf = pd.read_csv(csv_path, skiprows=1)
                for _, srow in sdf.iterrows():
                    snum = int(srow["Scene Number"])
                    key  = f"{video_prefix}-scene-{snum:03d}"
                    durations[key] = round(float(srow["Length (seconds)"]), 2)
            except Exception:
                pass

    df = pd.read_csv(scores_csv)
    return [
        {
            "scene":     row["scene"],
            "score":     round(float(row["score"]), 4),
            "duration":  durations.get(row["scene"]),
            "frame_url": f"/api/file?path={frames_dir / (row['scene'] + '.jpg')}"
                         if (frames_dir / (row["scene"] + ".jpg")).exists() else None,
        }
        for _, row in df.iterrows()
    ]


@app.get("/api/jobs/{job_id}/result")
async def job_result(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    work_dir = job.work_dir()
    files = {}
    for name in ("highlight_final_music.mp4", "highlight_music.mp4",
                 "highlight_final.mp4", "highlight.mp4"):
        p = work_dir / name
        if p.exists():
            files[name] = {
                "url":     f"/api/file?path={p}",
                "size_mb": round(p.stat().st_size / 1_048_576, 1),
            }
    return files


@app.get("/api/file")
async def serve_file(request: Request, path: str = Query(...)):
    p = Path(path).resolve()
    if not str(p).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not p.exists():
        raise HTTPException(404)

    stat      = p.stat()
    file_size = stat.st_size
    mime      = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    etag      = f'"{stat.st_mtime:.6f}-{file_size}"'
    last_mod  = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(stat.st_mtime))

    CHUNK = 2 * 1024 * 1024  # 2 MB — large enough for smooth video buffering

    base_headers = {
        "Accept-Ranges":  "bytes",
        "ETag":           etag,
        "Last-Modified":  last_mod,
        "Cache-Control":  "public, max-age=86400",
    }

    # Conditional request — return 304 if client already has the file
    if request.headers.get("if-none-match") == etag:
        return JSONResponse(None, status_code=304, headers=base_headers)

    range_header = request.headers.get("range")
    if range_header:
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0])
            end   = int(parts[1]) if parts[1] else min(start + CHUNK - 1, file_size - 1)
        except Exception:
            raise HTTPException(416)
        end        = min(end, file_size - 1)
        chunk_size = end - start + 1

        async def range_stream():
            async with aiofiles.open(str(p), "rb") as f:
                await f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    data = await f.read(min(CHUNK, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(range_stream(), status_code=206, media_type=mime,
            headers={**base_headers,
                     "Content-Range":  f"bytes {start}-{end}/{file_size}",
                     "Content-Length": str(chunk_size)})

    async def full_stream():
        async with aiofiles.open(str(p), "rb") as f:
            while True:
                data = await f.read(CHUNK)
                if not data:
                    break
                yield data

    return StreamingResponse(full_stream(), media_type=mime,
        headers={**base_headers, "Content-Length": str(file_size)})
