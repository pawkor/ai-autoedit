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

APP_DIR     = Path(__file__).resolve().parent.parent
SCRIPT_DIR  = APP_DIR / "src"

sys.path.insert(0, str(SCRIPT_DIR))
import pipeline  # noqa: E402
WEBAPP_DIR  = Path(__file__).resolve().parent
STATIC_DIR  = WEBAPP_DIR / "static"
JOBS_DIR    = WEBAPP_DIR / "jobs"
BROWSE_ROOT = Path(os.environ.get("BROWSE_ROOT", str(Path.home())))

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
        self._task: Optional[asyncio.Task] = None
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

def _proc_meminfo() -> tuple[int, int]:
    """Read MemTotal/MemAvailable from /proc/meminfo — correct inside LXC containers."""
    total = avail = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
    except Exception:
        pass
    return total - avail, total


def _container_memory() -> tuple[int, int]:
    """Return (used_bytes, total_bytes) from cgroups (matches docker stats).
    Falls back to /proc/meminfo if cgroups are unavailable.
    /proc/meminfo is bind-mounted from the LXC host so it shows the correct
    container RAM instead of the Proxmox host's physical RAM."""

    # cgroup v2
    try:
        used_total = int(Path("/sys/fs/cgroup/memory.current").read_text())
        cache = 0
        for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines():
            if line.startswith("inactive_file "):
                cache = int(line.split()[1]); break
        limit_text = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        total = _proc_meminfo()[1] if limit_text == "max" else int(limit_text)
        return max(0, used_total - cache), total
    except Exception:
        pass
    # cgroup v1
    try:
        used_total = int(Path("/sys/fs/cgroup/memory/memory.usage_in_bytes").read_text())
        cache = 0
        for line in Path("/sys/fs/cgroup/memory/memory.stat").read_text().splitlines():
            if line.startswith("total_inactive_file "):
                cache = int(line.split()[1]); break
        proc_total = _proc_meminfo()[1]
        limit = int(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes").read_text())
        total = proc_total if limit > proc_total * 0.99 else limit
        return max(0, used_total - cache), total
    except Exception:
        pass
    return _proc_meminfo()

def _get_stats() -> dict:
    global _gpu_available
    cpu       = psutil.cpu_percent(interval=None)
    ram_used, ram_total = _container_memory()
    ram_pct   = round(ram_used / ram_total * 100, 1) if ram_total else 0
    stats = {
        "cpu_pct":       round(cpu, 1),
        "ram_used_gb":   round(ram_used  / 1e9, 1),
        "ram_total_gb":  round(ram_total / 1e9, 1),
        "ram_pct":       ram_pct,
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

async def _run_job(job: Job):
    async with job_semaphore:
        job.status = "running"
        job.started_at = time.time()
        await job.broadcast({"type": "status", "status": "running"})
        job.save()

        try:
            async for raw_line in pipeline.run(job.params, job.work_dir()):
                # \r prefix = ffmpeg progress bar update (overwrite previous line)
                for part in raw_line.split('\r'):
                    line = part.rstrip('\n').rstrip()
                    if line:
                        job.log.append(line)
                        await job.broadcast({"type": "log", "line": line})
            job.status = "done"
        except asyncio.CancelledError:
            job.log.append("[job cancelled]")
            job.status = "killed"
        except RuntimeError as e:
            job.log.append(f"ERROR: {e}")
            job.status = "failed"
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


@app.get("/api/config")
async def get_config():
    return {"browse_root": str(BROWSE_ROOT)}


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
    output = out.decode("utf-8", errors="replace")
    ini_start = output.find('[clip_prompts]')
    result: dict = {"ok": proc.returncode == 0 and ini_start >= 0, "output": output}
    if ini_start >= 0:
        cp = configparser.ConfigParser()
        cp.read_string(output[ini_start:])
        result["positive"] = cp.get("clip_prompts", "positive", fallback="").strip()
        result["negative"] = cp.get("clip_prompts", "negative", fallback="").strip()
    return result


@app.post("/api/jobs/{job_id}/save-prompts")
async def save_job_prompts(job_id: str, data: dict):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    description = data.get("description", "").strip()
    positive    = data.get("positive", "").strip()
    negative    = data.get("negative", "").strip()
    if description:
        job.params["description"] = description
        job.save()
    if positive or negative:
        save_prompts_to_config(Path(job.params["work_dir"]) / "config.ini", positive, negative)
    return {"ok": True}


@app.post("/api/music-rebuild")
async def music_rebuild(payload: dict):
    music_dir = payload.get("dir", "")
    if not music_dir:
        raise HTTPException(400, "dir required")
    d = Path(music_dir).expanduser().resolve()
    if not d.is_dir():
        raise HTTPException(404, "Directory not found")
    cmd = [sys.executable, str(SCRIPT_DIR / "music_index.py"), str(d)]
    if payload.get("force"):        cmd.append("--force")
    if payload.get("force_genres"): cmd.append("--force-genres")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
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
    # Fallback: scan mp3s without index (recursive)
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
    description:  Optional[str] = None


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
    "music_dir":    ("music", "dir"),
    "positive":     ("clip_prompts", "positive"),
    "negative":     ("clip_prompts", "negative"),
}


def read_job_config(work_dir: Path) -> dict:
    """Read job-relevant keys; work_dir/config.ini overrides global config.ini."""
    global_cp = configparser.ConfigParser()
    global_cp.read(str(APP_DIR / "config.ini"))

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


def save_prompts_to_config(cfg_path: Path, positive: str, negative: str):
    """Rewrite [clip_prompts] section in config.ini preserving all other content."""
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
    replaced = re.sub(r'\[clip_prompts\].*?(?=\n\[|\Z)', new_section.rstrip(),
                      content, flags=re.DOTALL)
    if replaced == content:  # section didn't exist — append
        replaced = content.rstrip() + "\n\n" + new_section
    cfg_path.write_text(replaced)


@app.get("/api/job-config")
async def get_job_config(dir: str = Query(...)):
    """Return job-relevant values from work_dir/config.ini to pre-fill the form."""
    work_dir = Path(dir).resolve()
    if not str(work_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    result = read_job_config(work_dir)
    result["_resolved"] = str(work_dir)
    work_subdir = result.get("work_subdir") or "_autoframe"
    result["_has_processed"] = (work_dir / work_subdir).is_dir() or any(work_dir.glob("highlight*.mp4"))
    return result


@app.post("/api/jobs/import")
async def import_job(data: dict):
    """Create a completed job entry for a directory processed outside the webapp."""
    work_dir = Path(data.get("work_dir", "")).resolve()
    if not str(work_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not work_dir.is_dir():
        raise HTTPException(400, "work_dir not found")

    # Check if job for this dir already exists
    for job in jobs.values():
        if Path(job.params.get("work_dir", "")).resolve() == work_dir:
            return {"id": job.id}

    params = read_job_config(work_dir)
    params["work_dir"] = str(work_dir)
    params.setdefault("work_subdir", "_autoframe")

    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id, params)
    job.status = "done"
    job.log = ["[imported from existing files]"]
    # Use mtime of the newest highlight file as timestamp
    mp4s = list(work_dir.glob("highlight*.mp4"))
    if mp4s:
        job.started_at = min(p.stat().st_mtime for p in mp4s)
        job.ended_at   = max(p.stat().st_mtime for p in mp4s)
    jobs[job_id] = job
    job.save()
    return {"id": job_id}


@app.put("/api/job-config")
async def put_job_config(data: dict):
    """Persist individual fields to work_dir/config.ini without starting a job."""
    work_dir = Path(data.get("work_dir", "")).resolve()
    if not str(work_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not work_dir.is_dir():
        raise HTTPException(400, "work_dir not found")
    save_job_config(work_dir, data)
    return {"ok": True}


def _resolve_params(d: dict, work_dir: Path) -> dict:
    """Fill None scene-selection values from config.ini (work_dir then global), then hardcoded defaults."""
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

    job._task = asyncio.create_task(_run_job(job))
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
    job._task     = None
    job.save()

    job._task = asyncio.create_task(_run_job(job))
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



@app.get("/api/jobs/{job_id}/overrides")
async def get_overrides(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    p = job.auto_dir() / "manual_overrides.json"
    return json.loads(p.read_text()) if p.exists() else {}


@app.put("/api/jobs/{job_id}/overrides")
async def put_overrides(job_id: str, data: dict):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    p = job.auto_dir() / "manual_overrides.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return {"ok": True}


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

    df = pd.read_csv(scores_csv).sort_values("scene")
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

    def _add(p: Path):
        if p.exists():
            files[p.name] = {
                "url":     f"/api/file?path={p}",
                "size_mb": round(p.stat().st_size / 1_048_576, 1),
            }

    # Versioned music mixes — newest version first
    def _ver(p: Path) -> int:
        m = re.search(r'_v(\d+)$', p.stem)
        return int(m.group(1)) if m else 0

    for pat in ("highlight_final_music_v*.mp4", "highlight_music_v*.mp4"):
        for p in sorted(work_dir.glob(pat), key=_ver, reverse=True):
            _add(p)

    return files


# ── YouTube ───────────────────────────────────────────────────────────────────

YT_SECRETS = WEBAPP_DIR / "youtube_client_secrets.json"
YT_TOKEN   = WEBAPP_DIR / "youtube_token.json"
YT_SCOPES  = ["https://www.googleapis.com/auth/youtube"]
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # allow http for local server


def _yt_creds():
    if not YT_TOKEN.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        creds = Credentials.from_authorized_user_file(str(YT_TOKEN), YT_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
            YT_TOKEN.write_text(creds.to_json())
        return creds if creds.valid else None
    except Exception:
        return None


@app.get("/api/youtube/status")
async def yt_status():
    return {"authenticated": _yt_creds() is not None, "has_secrets": YT_SECRETS.exists()}


@app.get("/api/youtube/auth")
async def yt_auth(origin: str = Query(...)):
    if not YT_SECRETS.exists():
        raise HTTPException(400, "youtube_client_secrets.json not found in webapp/")
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(
        str(YT_SECRETS), scopes=YT_SCOPES,
        redirect_uri=f"{origin}/api/youtube/callback",
    )
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    (WEBAPP_DIR / "youtube_flow.json").write_text(json.dumps({
        "state": state, "redirect_uri": f"{origin}/api/youtube/callback",
    }))
    return {"url": auth_url}


@app.get("/api/youtube/callback")
async def yt_callback(code: str = Query(None), error: str = Query(None)):
    from fastapi.responses import HTMLResponse
    if error:
        return HTMLResponse(f"<h2>YouTube auth error: {error}</h2>")
    flow_file = WEBAPP_DIR / "youtube_flow.json"
    if not flow_file.exists():
        return HTMLResponse("<h2>OAuth flow not started — please try again.</h2>")
    flow_data = json.loads(flow_file.read_text())
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(
        str(YT_SECRETS), scopes=YT_SCOPES,
        redirect_uri=flow_data["redirect_uri"], state=flow_data["state"],
    )
    flow.fetch_token(code=code)
    YT_TOKEN.write_text(flow.credentials.to_json())
    flow_file.unlink(missing_ok=True)
    return HTMLResponse("<h2>YouTube connected! You can close this tab.</h2><script>window.close()</script>")


@app.get("/api/youtube/playlists")
async def yt_playlists():
    creds = _yt_creds()
    if not creds:
        raise HTTPException(401, "Not authenticated")
    def _fetch():
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=creds)
        resp = yt.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        return sorted(
            [{"id": i["id"], "title": i["snippet"]["title"]} for i in resp.get("items", [])],
            key=lambda x: x["title"],
        )
    return await asyncio.to_thread(_fetch)


_yt_uploads: dict = {}  # upload_id → {status, pct, url, error}

@app.post("/api/youtube/upload")
async def yt_upload(payload: dict):
    creds = _yt_creds()
    if not creds:
        raise HTTPException(401, "Not authenticated")
    file_path = Path(payload["file_path"])
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    if not str(file_path).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Access denied")

    upload_id = str(uuid.uuid4())[:8]
    _yt_uploads[upload_id] = {"status": "uploading", "pct": 0, "url": None, "error": None}

    def _do_upload():
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        yt = build("youtube", "v3", credentials=creds)

        playlist_id = payload.get("playlist_id") or None
        if payload.get("new_playlist"):
            pl = yt.playlists().insert(
                part="snippet,status",
                body={"snippet": {"title": payload["new_playlist"]},
                      "status": {"privacyStatus": payload.get("privacy", "unlisted")}},
            ).execute()
            playlist_id = pl["id"]

        chunksize = 10 * 1024 * 1024  # 10 MB chunks
        media = MediaFileUpload(str(file_path), chunksize=chunksize, resumable=True)
        req = yt.videos().insert(
            part="snippet,status",
            body={
                "snippet": {"title": payload.get("title", file_path.stem),
                            "description": payload.get("description", "")},
                "status":  {"privacyStatus": payload.get("privacy", "unlisted")},
            },
            media_body=media,
        )

        response = None
        while response is None:
            status, response = req.next_chunk()
            if status:
                _yt_uploads[upload_id]["pct"] = int(status.progress() * 100)

        video_id = response["id"]
        if playlist_id:
            yt.playlistItems().insert(
                part="snippet",
                body={"snippet": {"playlistId": playlist_id,
                                  "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
            ).execute()
        return video_id

    async def _run():
        try:
            video_id = await asyncio.to_thread(_do_upload)
            _yt_uploads[upload_id].update({"status": "done", "pct": 100,
                                           "url": f"https://youtu.be/{video_id}"})
        except Exception as e:
            _yt_uploads[upload_id].update({"status": "error", "error": str(e)})

    asyncio.create_task(_run())
    return {"upload_id": upload_id}


@app.get("/api/youtube/upload/{upload_id}")
async def yt_upload_status(upload_id: str):
    s = _yt_uploads.get(upload_id)
    if not s:
        raise HTTPException(404)
    return s


@app.delete("/api/youtube/disconnect")
async def yt_disconnect():
    YT_TOKEN.unlink(missing_ok=True)
    return {"ok": True}


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
            if parts[1]:
                # Closed range (bytes=N-M) — honour exactly (Safari is strict)
                end = min(int(parts[1]), file_size - 1)
            else:
                # Open-ended range (bytes=N-) — serve a full chunk
                end = min(start + CHUNK - 1, file_size - 1)
        except Exception:
            raise HTTPException(416)
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
