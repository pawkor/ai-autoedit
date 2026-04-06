#!/usr/bin/env python3
"""
autoframe web UI — FastAPI backend
Run: uvicorn webapp.server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import configparser
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
import psutil
import tempfile
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError as BotoClientError
    _boto3_ok = True
except ImportError:
    _boto3_ok = False

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
    _prom_ok = True
except ImportError:
    _prom_ok = False
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request, Body, UploadFile, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
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

# ── Auth ───────────────────────────────────────────────────────────────────────
ENABLE_AUTH  = os.environ.get("ENABLE_AUTH", "false").lower() in ("1", "true", "yes")
USERS_FILE   = Path(__file__).resolve().parent / "users.json"
_sessions: dict[str, str] = {}   # token → username

def _hash_pw(password: str) -> str:
    # pbkdf2 with per-password salt; format: "pbkdf2:<salt_hex>:<hash_hex>"
    import secrets as _sec
    salt = _sec.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return f"pbkdf2:{salt.hex()}:{h.hex()}"

def _verify_pw(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2:"):
        _, salt_hex, hash_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return h.hex() == hash_hex
    # legacy SHA-256 (migrate on next login)
    return hashlib.sha256(password.encode()).hexdigest() == stored

def _load_users() -> list[dict]:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text()).get("users", [])
        except Exception:
            return []
    return []

def _save_users(users: list[dict]):
    USERS_FILE.write_text(json.dumps({"users": users}))

def _get_session_user(request: Request) -> Optional[str]:
    token = request.cookies.get("ae_session")
    return _sessions.get(token) if token else None


def _resolve_data_root() -> Optional[Path]:
    """Return the user data root: /data if non-empty (Docker), else from webapp config."""
    data_path = Path('/data')
    try:
        if data_path.is_dir() and any(data_path.iterdir()):
            return data_path
    except PermissionError:
        pass
    stored = wcfg('data_root', '')
    if stored and Path(stored).is_dir():
        return Path(stored)
    return None

DATA_ROOT: Optional[Path] = _resolve_data_root()

# ── S3 (optional) ─────────────────────────────────────────────────────────────
S3_CLIENT = None
S3_BUCKET  = os.environ.get("S3_BUCKET", "").strip()
if _boto3_ok and S3_BUCKET and os.environ.get("S3_ACCESS_KEY_ID"):
    _s3_kw: dict = {
        "aws_access_key_id":     os.environ["S3_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["S3_SECRET_ACCESS_KEY"],
        "region_name":           os.environ.get("S3_REGION", "us-east-1"),
    }
    if os.environ.get("S3_ENDPOINT_URL"):
        _s3_kw["endpoint_url"] = os.environ["S3_ENDPOINT_URL"]
    try:
        S3_CLIENT = boto3.client("s3", **_s3_kw)
    except Exception as _e:
        print(f"[S3] init error: {_e}")

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
        self.phase       = "analyzing" # analyzing | analyzed | rendering | done | failed
        self.log: list[str] = []
        self.process: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self.created_at  = time.time()
        self.started_at  = time.time()
        self.ended_at: Optional[float] = None
        self.subscribers: set[WebSocket] = set()
        self.analyze_result: Optional[dict] = None
        self.selected_track: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id":             self.id,
            "params":         self.params,
            "status":         self.status,
            "phase":          self.phase,
            "log":            self.log,
            "created_at":     self.created_at,
            "started_at":     self.started_at,
            "ended_at":       self.ended_at,
            "analyze_result": self.analyze_result,
            "selected_track": self.selected_track,
        }

    def save(self):
        path = JOBS_DIR / f"{self.id}.json"
        path.write_text(json.dumps(self.to_dict()))

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        j = cls(data["id"], data["params"])
        j.status         = data["status"]
        j.phase          = data.get("phase", "done")   # backward compat: old jobs are "done"
        j.log            = data.get("log", [])
        j.created_at     = data.get("created_at", data.get("started_at", time.time()))
        j.started_at     = data.get("started_at", time.time())
        j.ended_at       = data.get("ended_at")
        j.analyze_result = data.get("analyze_result")
        j.selected_track = data.get("selected_track")
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

# ── Threshold-search state (polling) ──────────────────────────────────────────
_threshold_searches: dict[str, dict] = {}   # search_id → status dict
_threshold_tasks:    dict[str, asyncio.Task] = {}  # search_id → running task

# ── Proxy-creation state (one task per job) ────────────────────────────────────
_proxy_tasks:  dict[str, asyncio.Task] = {}   # job_id → running task
_proxy_status: dict[str, dict] = {}           # job_id → status dict

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="autoframe")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Prometheus metrics ─────────────────────────────────────────────────────────
if _prom_ok:
    _prom_jobs_active       = Gauge("autoframe_jobs_active",               "Currently running jobs")
    _prom_jobs_queued       = Gauge("autoframe_jobs_queued",               "Currently queued jobs")
    _prom_jobs_total        = Counter("autoframe_jobs_total",              "Jobs completed", ["phase", "status"])
    _prom_job_duration      = Histogram("autoframe_job_duration_seconds",  "Job duration in seconds", ["phase"],
                                        buckets=[30, 60, 120, 300, 600, 900, 1800, 3600])
    _prom_cpu_pct           = Gauge("autoframe_cpu_percent",               "CPU utilization percent")
    _prom_ram_used          = Gauge("autoframe_ram_used_bytes",            "RAM used bytes")
    _prom_ram_total         = Gauge("autoframe_ram_total_bytes",           "RAM total bytes")
    _prom_gpu_pct           = Gauge("autoframe_gpu_utilization_percent",   "GPU utilization percent")
    _prom_gpu_vram_used     = Gauge("autoframe_gpu_vram_used_bytes",       "GPU VRAM used bytes")
    _prom_gpu_vram_total    = Gauge("autoframe_gpu_vram_total_bytes",      "GPU VRAM total bytes")

_NO_CACHE_EXTS = {".html", ".js", ".css", ".json", ".txt", ".svg", ".ico"}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not ENABLE_AUTH:
        return await call_next(request)
    path = request.url.path
    # Always allow: auth endpoints, static assets, main page, websockets, favicon
    if (path.startswith("/api/auth/") or
            path.startswith("/static/") or
            path.startswith("/ws/") or
            path in ("/", "/favicon.ico", "/metrics")):
        return await call_next(request)
    # Require valid session for everything else
    if _get_session_user(request) is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    ext  = path[path.rfind("."):].lower() if "." in path.split("/")[-1] else ""
    if ext not in {".jpg", ".jpeg", ".png", ".mp4", ".webp"}:
        response.headers["Cache-Control"] = "no-store"
    return response

_rebuild_tasks: dict[str, dict] = {}  # task_id -> {progress, total, done, ok}


@app.get("/metrics")
async def prometheus_metrics():
    if not _prom_ok:
        raise HTTPException(501, "prometheus_client not installed")
    stats = _get_stats()
    _prom_jobs_active.set(stats["running_jobs"])
    _prom_jobs_queued.set(stats["queued_jobs"])
    _prom_cpu_pct.set(stats["cpu_pct"])
    _prom_ram_used.set(stats["ram_used_gb"] * 1e9)
    _prom_ram_total.set(stats["ram_total_gb"] * 1e9)
    if stats.get("gpu"):
        gpu = stats["gpu"]
        _prom_gpu_pct.set(gpu["pct"])
        _prom_gpu_vram_used.set(gpu["vram_used_mb"] * 1_000_000)
        _prom_gpu_vram_total.set(gpu["vram_total_mb"] * 1_000_000)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


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

async def _run_job(job: Job, analyze_only: bool = False, selected_track: Optional[str] = None):
    async with job_semaphore:
        job.status = "running"
        job.phase  = "analyzing" if analyze_only else "rendering"
        job.started_at = time.time()
        await job.broadcast({"type": "status", "status": "running", "phase": job.phase})
        job.save()

        try:
            run_params = {**job.params,
                          "max_detect_workers": int(wcfg("max_detect_workers", str(os.cpu_count() or 4))),
                          "batch_size":         int(wcfg("clip_batch_size", "64")),
                          "clip_workers":       int(wcfg("clip_workers", "4"))}
            async for raw_line in pipeline.run(run_params, job.work_dir(),
                                               analyze_only=analyze_only,
                                               selected_track=selected_track):
                # \r prefix = ffmpeg progress bar update (overwrite previous line)
                for part in raw_line.split('\r'):
                    line = part.rstrip('\n').rstrip()
                    if line:
                        is_progress = bool(re.search(r'^\s*\d+%\||\s*\[[\u2588\u2591 ]+\]\s+\d+%|\b\d+%\|', line))
                        if not is_progress:
                            job.log.append(line)
                        await job.broadcast({"type": "log", "line": line})
            if analyze_only:
                job.status = "done"
                job.phase  = "analyzed"
                ar_path = job.auto_dir() / "analyze_result.json"
                if ar_path.exists():
                    try:
                        job.analyze_result = json.loads(ar_path.read_text())
                    except Exception:
                        pass
            else:
                job.status = "done"
                job.phase  = "done"
        except asyncio.CancelledError:
            job.log.append("[job cancelled]")
            job.status = "killed"
            job.phase  = "failed"
        except RuntimeError as e:
            job.log.append(f"ERROR: {e}")
            job.status = "failed"
            job.phase  = "failed"
        except Exception as e:
            job.log.append(f"ERROR: {e}")
            job.status = "failed"
            job.phase  = "failed"

        job.ended_at = time.time()
        if _prom_ok:
            phase = "analyze" if analyze_only else "render"
            _prom_jobs_total.labels(phase=phase, status=job.status).inc()
            _prom_job_duration.labels(phase=phase).observe(job.ended_at - job.started_at)
        job.save()
        await job.broadcast({"type": "status", "status": job.status, "phase": job.phase})


# ── Routes ─────────────────────────────────────────────────────────────────────

# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/auth/status")
async def auth_status(request: Request):
    users = _load_users()
    user = _get_session_user(request)
    return {
        "enabled":       ENABLE_AUTH,
        "has_users":     len(users) > 0,
        "authenticated": not ENABLE_AUTH or user is not None,
        "username":      user,
    }

@app.post("/api/auth/login")
async def auth_login(request: Request, data: dict = Body(...)):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    users = _load_users()
    match = next((u for u in users if u["username"] == username and _verify_pw(password, u["password_hash"])), None)
    if not match:
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_hex(32)
    _sessions[token] = username
    response = JSONResponse({"ok": True, "username": username})
    response.set_cookie("ae_session", token, httponly=True, samesite="strict", max_age=86400 * 30)
    return response

@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get("ae_session")
    if token:
        _sessions.pop(token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("ae_session")
    return response

@app.get("/api/auth/users")
async def get_auth_users(request: Request):
    users = _load_users()
    if ENABLE_AUTH and users and not _get_session_user(request):
        raise HTTPException(401)
    return [{"username": u["username"]} for u in users]

@app.post("/api/auth/users")
async def create_auth_user(request: Request, data: dict = Body(...)):
    # Allow creating first user without auth (bootstrap); subsequent requires auth
    if ENABLE_AUTH:
        existing = _load_users()
        if existing and not _get_session_user(request):
            raise HTTPException(401)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    users = _load_users()
    if any(u["username"] == username for u in users):
        raise HTTPException(409, "User already exists")
    users.append({"username": username, "password_hash": _hash_pw(password)})
    _save_users(users)
    return {"ok": True}

@app.delete("/api/auth/users/{username}")
async def delete_auth_user(request: Request, username: str):
    if ENABLE_AUTH and not _get_session_user(request):
        raise HTTPException(401)
    users = _load_users()
    new_users = [u for u in users if u["username"] != username]
    if len(new_users) == len(users):
        raise HTTPException(404, "User not found")
    if not new_users:
        raise HTTPException(400, "Cannot delete last user")
    _save_users(new_users)
    return {"ok": True}

@app.patch("/api/auth/users/{username}")
async def update_auth_user(request: Request, username: str, data: dict = Body(...)):
    if ENABLE_AUTH and not _get_session_user(request):
        raise HTTPException(401)
    password = data.get("password") or ""
    if not password:
        raise HTTPException(400, "Password required")
    users = _load_users()
    user = next((u for u in users if u["username"] == username), None)
    if not user:
        raise HTTPException(404, "User not found")
    user["password_hash"] = _hash_pw(password)
    _save_users(users)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    p = STATIC_DIR / "favicon.ico"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p))


@app.get("/api/config")
async def get_config():
    return {
        "browse_root":        str(BROWSE_ROOT),
        "data_root":          str(DATA_ROOT) if DATA_ROOT else None,
        "data_root_configured": DATA_ROOT is not None,
    }


@app.post("/api/config/data-root")
async def set_data_root(data: dict):
    path = data.get("path", "").strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(400, "Invalid directory")
    if not str(Path(path).resolve()).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Outside allowed root")
    save_wcfg({"data_root": path})
    global DATA_ROOT
    DATA_ROOT = Path(path)
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    return {
        "max_concurrent_jobs":  int(wcfg("max_concurrent_jobs",  "1")),
        "max_detect_workers":   int(wcfg("max_detect_workers",   str(os.cpu_count() or 4))),
        "clip_batch_size":      int(wcfg("clip_batch_size",      "64")),
        "clip_workers":         int(wcfg("clip_workers",         "4")),
        "port":                int(wcfg("port", "8000")),
        "theme":               wcfg("theme", ""),
        "lang":                wcfg("lang", ""),
        "sort_newest":         wcfg("sort_newest", ""),
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


@app.post("/api/save-prompts")
async def save_prompts(data: dict):
    work_dir = data.get("work_dir", "").strip()
    if not work_dir or not Path(work_dir).is_dir():
        raise HTTPException(400, f"work_dir not found: {work_dir}")
    positive = data.get("positive", "").strip()
    negative = data.get("negative", "").strip()
    save_prompts_to_config(Path(work_dir) / "config.ini", positive, negative)
    return {"ok": True}


@app.post("/api/music-rebuild")
async def music_rebuild(payload: dict):
    music_dir = payload.get("dir", "")
    if not music_dir:
        raise HTTPException(400, "dir required")
    d = Path(music_dir).expanduser().resolve()
    if not str(d).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Outside allowed root")
    if not d.is_dir():
        raise HTTPException(404, "Directory not found")
    cmd = [sys.executable, str(SCRIPT_DIR / "music_index.py"), str(d)]
    if payload.get("force"):        cmd.append("--force")
    if payload.get("force_genres"): cmd.append("--force-genres")

    task_id = uuid.uuid4().hex[:8]
    _rebuild_tasks[task_id] = {"progress": 0, "total": 0, "done": False, "ok": False}

    async def run():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            m = re.match(r"^TOTAL:(\d+)", line)
            if m:
                _rebuild_tasks[task_id]["total"] = int(m.group(1))
            m = re.match(r"^PROGRESS:(\d+)/(\d+)", line)
            if m:
                _rebuild_tasks[task_id]["progress"] = int(m.group(1))
        await proc.wait()
        _rebuild_tasks[task_id]["done"] = True
        _rebuild_tasks[task_id]["ok"] = proc.returncode == 0

    asyncio.create_task(run())
    return {"task_id": task_id}


@app.get("/api/music-rebuild-status/{task_id}")
async def music_rebuild_status(task_id: str):
    task = _rebuild_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.get("/api/music-files")
async def music_files_endpoint(dir: str = Query(...)):
    d = Path(dir).expanduser().resolve()
    if not str(d).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Outside allowed root")
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


@app.get("/api/count-sources")
async def count_sources(dir: str = Query(...), cameras: str = Query(default="")):
    """Count source MP4 files in camera subdirectories."""
    work_dir = Path(dir).resolve()
    if not str(work_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not work_dir.is_dir():
        raise HTTPException(404)

    def _is_source(f: Path) -> bool:
        n = f.name.lower()
        return n.endswith(".mp4") and not n.startswith("highlight") and not n.endswith(".lrv")

    cam_list = [c.strip() for c in cameras.split(",") if c.strip()] if cameras else []
    per_camera: dict[str, int] = {}
    if cam_list:
        for cam in cam_list:
            cam_dir = (work_dir / cam).resolve()
            if str(cam_dir).startswith(str(work_dir)) and cam_dir.is_dir():
                per_camera[cam] = sum(1 for f in cam_dir.glob("*.mp4") if _is_source(f))
    else:
        per_camera[""] = sum(1 for f in work_dir.glob("*.mp4") if _is_source(f))

    return {"total": sum(per_camera.values()), "per_camera": per_camera}


@app.get("/api/subdirs")
async def list_subdirs(dir: str = Query(...)):
    """List immediate subdirectories of a path (for camera subfolder picker)."""
    p = Path(dir).resolve()
    if not str(p).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not p.is_dir():
        raise HTTPException(404)
    names = sorted(
        d.name for d in p.iterdir()
        if d.is_dir() and not d.name.startswith('.') and d.name != '_autoframe'
    )
    return names


@app.post("/api/mkdir")
async def mkdir(data: dict):
    parent = Path(data.get("path", "")).resolve()
    name = (data.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(400, "Invalid folder name")
    if not str(parent).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    new_dir = parent / name
    new_dir.mkdir(exist_ok=True)
    return {"path": str(new_dir)}


@app.post("/api/upload")
async def upload_file(file: UploadFile, work_dir: str = Form(...)):
    dest_dir = Path(work_dir).resolve()
    if not str(dest_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not dest_dir.is_dir():
        raise HTTPException(400, "Directory not found")
    safe_name = Path(file.filename).name  # strip any path component
    if not safe_name:
        raise HTTPException(400, "Invalid filename")
    if Path(safe_name).suffix.lower() not in _UPLOAD_EXTS:
        raise HTTPException(400, "File type not allowed")
    dest_path = dest_dir / safe_name
    if dest_path.resolve().parent != dest_dir:
        raise HTTPException(400, "Invalid filename")
    with open(dest_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return {"ok": True, "path": str(dest_path)}


_VIDEO_EXTS  = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.m4v', '.3gp'}
_UPLOAD_EXTS = _VIDEO_EXTS | {'.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac'}

@app.get("/api/files")
async def list_files(path: str = Query(...)):
    d = Path(path).resolve()
    if not str(d).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not d.is_dir():
        raise HTTPException(400)
    files = sorted(
        [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in _VIDEO_EXTS],
        key=lambda f: f.name,
    )
    return [{"name": f.name, "path": str(f), "size": f.stat().st_size} for f in files]


@app.delete("/api/file")
async def delete_file_endpoint(path: str = Query(...)):
    f = Path(path).resolve()
    if not str(f).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not f.is_file():
        raise HTTPException(404)
    f.unlink()
    return {"ok": True}



@app.get("/api/s3/status")
async def s3_status():
    return {"configured": S3_CLIENT is not None, "bucket": S3_BUCKET or ""}


@app.get("/api/s3/list")
async def s3_list(prefix: str = Query(default="")):
    if not S3_CLIENT:
        raise HTTPException(503, "S3 not configured")
    try:
        resp = await asyncio.to_thread(
            S3_CLIENT.list_objects_v2, Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=500
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    items = [
        {"key": o["Key"], "name": o["Key"].split("/")[-1], "size": o["Size"],
         "last_modified": o["LastModified"].isoformat()}
        for o in resp.get("Contents", []) if not o["Key"].endswith("/")
    ]
    return {"items": items, "prefix": prefix, "bucket": S3_BUCKET}


@app.get("/api/s3/upload")
async def s3_upload_sse(local_path: str = Query(...), key: str = Query(...)):
    """SSE: upload a local file to S3, stream progress as JSON events."""
    if not S3_CLIENT:
        async def _err():
            yield 'data: {"error":"S3 not configured"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    local = Path(local_path).resolve()
    if not str(local).startswith(str(BROWSE_ROOT)) or not local.is_file():
        async def _err():
            yield 'data: {"error":"File not found or access denied"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    async def generate():
        size   = local.stat().st_size
        done   = [0]
        t_ref  = [time.time(), 0]      # [timestamp, bytes_at_last_sample]
        speed  = [""]

        def callback(n):
            done[0] += n
            now = time.time()
            dt  = now - t_ref[0]
            if dt >= 0.5:
                spd = (done[0] - t_ref[1]) / dt
                speed[0] = f"{spd/1_048_576:.1f} MB/s" if spd >= 1_048_576 else f"{spd/1024:.0f} KB/s"
                t_ref[0], t_ref[1] = now, done[0]

        task = asyncio.create_task(asyncio.to_thread(
            S3_CLIENT.upload_file, str(local), S3_BUCKET, key,
            Callback=callback
        ))
        while not task.done():
            pct = round(done[0] / size * 100) if size else 0
            yield f"data: {json.dumps({'pct': pct, 'speed': speed[0]})}\n\n"
            await asyncio.sleep(0.3)
        try:
            await task
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/s3/download")
async def s3_download_sse(key: str = Query(...), local_path: str = Query(...)):
    """SSE: download an S3 object to a local path, stream progress."""
    if not S3_CLIENT:
        async def _err():
            yield 'data: {"error":"S3 not configured"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    async def generate():
        try:
            head = await asyncio.to_thread(S3_CLIENT.head_object, Bucket=S3_BUCKET, Key=key)
            size = head["ContentLength"]
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return

        dest = Path(local_path).resolve()
        if not str(dest).startswith(str(BROWSE_ROOT)):
            yield f"data: {json.dumps({'error': 'Access denied'})}\n\n"
            return
        dest.parent.mkdir(parents=True, exist_ok=True)

        done   = [0]
        t_ref  = [time.time(), 0]
        speed  = [""]

        def callback(n):
            done[0] += n
            now = time.time()
            dt  = now - t_ref[0]
            if dt >= 0.5:
                spd = (done[0] - t_ref[1]) / dt
                speed[0] = f"{spd/1_048_576:.1f} MB/s" if spd >= 1_048_576 else f"{spd/1024:.0f} KB/s"
                t_ref[0], t_ref[1] = now, done[0]

        task = asyncio.create_task(asyncio.to_thread(
            S3_CLIENT.download_file, S3_BUCKET, key, str(dest), Callback=callback
        ))
        while not task.done():
            pct = round(done[0] / size * 100) if size else 0
            yield f"data: {json.dumps({'pct': pct, 'speed': speed[0]})}\n\n"
            await asyncio.sleep(0.3)
        try:
            await task
            yield f"data: {json.dumps({'done': True, 'name': dest.name})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



def _s3_prefix(work_dir: Path) -> str:
    """S3 prefix for a work_dir: relative path from BROWSE_ROOT with trailing slash.
    e.g. /data/2025/04-Grecja/04.21 → '2025/04-Grecja/04.21/'
    """
    try:
        rel = work_dir.resolve().relative_to(BROWSE_ROOT.resolve())
    except ValueError:
        rel = Path(work_dir.name)
    return str(rel).rstrip("/") + "/"


@app.get("/api/s3/source-status")
async def s3_source_status(work_dir: str = Query(...)):
    """List S3 source files vs local for each cam subfolder."""
    if not S3_CLIENT:
        raise HTTPException(503, "S3 not configured")
    wd = Path(work_dir).resolve()
    if not str(wd).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    prefix = _s3_prefix(wd)
    # list S3 objects under prefix
    try:
        resp = await asyncio.to_thread(
            S3_CLIENT.list_objects_v2, Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=2000
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    s3_files: dict[str, int] = {
        o["Key"]: o["Size"]
        for o in resp.get("Contents", [])
        if Path(o["Key"]).suffix.lower() in _VIDEO_EXTS
    }
    # group by immediate subfolder relative to prefix
    cams: dict[str, list] = {}
    for key, size in s3_files.items():
        rel = key[len(prefix):]
        parts = rel.split("/")
        cam = parts[0] if len(parts) > 1 else ""
        name = parts[-1]
        local_path = wd / rel
        cams.setdefault(cam, []).append({
            "key": key, "name": name, "size": size,
            "local": local_path.exists(),
            "local_path": str(local_path),
        })
    return {"prefix": prefix, "cams": cams}


@app.get("/api/s3/fetch-sources")
async def s3_fetch_sources(work_dir: str = Query(...), keys: str = Query(default="")):
    """SSE: download selected (or all missing) S3 source video files to local work_dir.
    keys: optional JSON array of S3 keys to fetch; if omitted, fetches all missing."""
    if not S3_CLIENT:
        async def _err():
            yield 'data: {"error":"S3 not configured"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    wd = Path(work_dir).resolve()
    if not str(wd).startswith(str(BROWSE_ROOT)):
        async def _err():
            yield 'data: {"error":"Access denied"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    selected_keys: list[str] | None = None
    if keys:
        try:
            selected_keys = json.loads(keys)
        except Exception:
            async def _err():
                yield 'data: {"error":"Invalid keys parameter"}\n\n'
            return StreamingResponse(_err(), media_type="text/event-stream")

    async def generate():
        prefix = _s3_prefix(wd)

        if selected_keys is not None:
            # Fetch only the specified keys; get sizes via head_object
            pairs: list[tuple[str, int]] = []
            for key in selected_keys:
                if not key.startswith(prefix):
                    continue
                try:
                    head = await asyncio.to_thread(S3_CLIENT.head_object, Bucket=S3_BUCKET, Key=key)
                    pairs.append((key, head["ContentLength"]))
                except Exception:
                    pairs.append((key, 0))
            missing = pairs
        else:
            try:
                resp = await asyncio.to_thread(
                    S3_CLIENT.list_objects_v2, Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=2000
                )
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                return
            files = [
                (o["Key"], o["Size"])
                for o in resp.get("Contents", [])
                if Path(o["Key"]).suffix.lower() in _VIDEO_EXTS
            ]
            missing = [
                (key, size) for key, size in files
                if not (wd / key[len(prefix):]).exists()
            ]

        if not missing:
            yield f"data: {json.dumps({'done': True, 'skipped': 0, 'fetched': 0})}\n\n"
            return

        fetched = 0
        for idx, (key, size) in enumerate(missing):
            dest = (wd / key[len(prefix):]).resolve()
            if not str(dest).startswith(str(BROWSE_ROOT)):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            name = key.split("/")[-1]
            yield f"data: {json.dumps({'file': name, 'idx': idx + 1, 'total': len(missing), 'pct': 0})}\n\n"

            done   = [0]
            t_ref  = [time.time(), 0]
            speed  = [""]

            def callback(n, _done=done, _t=t_ref, _spd=speed):
                _done[0] += n
                now, dt = time.time(), time.time() - _t[0]
                if dt >= 0.5:
                    s = (_done[0] - _t[1]) / dt
                    _spd[0] = f"{s/1_048_576:.1f} MB/s" if s >= 1_048_576 else f"{s/1024:.0f} KB/s"
                    _t[0], _t[1] = now, _done[0]

            task = asyncio.create_task(asyncio.to_thread(
                S3_CLIENT.download_file, S3_BUCKET, key, str(dest), Callback=callback
            ))
            while not task.done():
                pct = round(done[0] / size * 100) if size else 0
                yield f"data: {json.dumps({'file': name, 'idx': idx+1, 'total': len(missing), 'pct': pct, 'speed': speed[0]})}\n\n"
                await asyncio.sleep(0.4)
            try:
                await task
                fetched += 1
                yield f"data: {json.dumps({'file': name, 'idx': idx+1, 'total': len(missing), 'pct': 100})}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'error': str(exc), 'file': name})}\n\n"
                return

        yield f"data: {json.dumps({'done': True, 'fetched': fetched, 'total': len(missing)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/purge-local")
async def purge_local(data: dict = Body(...)):
    """Delete local source video files and autocut scenes to free disk space."""
    wd = Path(data.get("work_dir", "")).resolve()
    if not str(wd).startswith(str(BROWSE_ROOT)) or not wd.is_dir():
        raise HTTPException(400, "invalid work_dir")
    removed = 0
    # Delete video source files in cam subfolders (not in _autoframe/)
    for sub in wd.iterdir():
        if sub.name.startswith("_") or not sub.is_dir():
            continue
        for f in sub.iterdir():
            if f.suffix.lower() in _VIDEO_EXTS and f.is_file():
                f.unlink()
                removed += 1
    # Delete autocut scene clips
    autocut = wd / "_autoframe" / "autocut"
    if autocut.is_dir():
        for f in autocut.iterdir():
            if f.is_file():
                f.unlink()
                removed += 1
    return {"ok": True, "removed": removed}


@app.post("/api/music/save-downloaded")
async def music_save_downloaded(data: dict = Body(...)):
    """Move a yt-dlp temp file to the music directory."""
    src = Path(data.get("tmp_path", "")).resolve()
    dst_dir_raw = (data.get("music_dir") or "").strip()
    if not dst_dir_raw:
        raise HTTPException(400, "music_dir required")
    dst_dir = Path(dst_dir_raw).expanduser().resolve()
    # Source must be a real ytdl temp file
    if not str(src).startswith(tempfile.gettempdir()):
        raise HTTPException(400, "Source must be a temp file")
    if not src.is_file():
        raise HTTPException(404, "Temp file not found")
    # Destination must be under BROWSE_ROOT
    if not str(dst_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "music_dir outside allowed root")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    try:
        src.parent.rmdir()   # clean up empty temp dir
    except Exception:
        pass
    return {"ok": True, "path": str(dst)}


@app.get("/api/music/yt-download")
async def yt_download_sse(url: str = Query(...)):
    """SSE: download YouTube audio via yt-dlp, stream progress, return temp file path."""
    async def generate():
        tmp = tempfile.mkdtemp(prefix="ytdl-")
        cmd = [
            "yt-dlp", "--extract-audio", "--audio-format", "mp3",
            "--audio-quality", "0", "--no-playlist", "--newline",
            "-o", f"{tmp}/%(title)s.%(ext)s", "--", url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            yield f"data: {json.dumps({'error': 'yt-dlp not installed'})}\n\n"
            return

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            m = re.search(r"(\d+\.?\d*)%", line)
            pct = float(m.group(1)) if m else None
            yield f"data: {json.dumps({'msg': line, 'pct': pct})}\n\n"

        await proc.wait()
        mp3s = sorted(Path(tmp).glob("*.mp3"))
        if proc.returncode == 0 and mp3s:
            f = mp3s[0]
            yield f"data: {json.dumps({'done': True, 'path': str(f), 'name': f.stem})}\n\n"
        else:
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
            yield f"data: {json.dumps({'error': (stderr or 'Download failed')[-500:]})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/browse")
async def browse(path: str = Query(default=None)):
    root = Path(path).resolve() if path else (DATA_ROOT or BROWSE_ROOT)
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
    cameras:      Optional[list[str]] = None  # ordered list: first = audio cam
    cam_a:        Optional[str]   = None      # legacy; kept for backward compat
    cam_b:        Optional[str]   = None      # legacy; kept for backward compat
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


# ── Per-directory config.ini helpers ──────────────────────────────────────────
# Maps JobParams fields → (section, key) in config.ini
_JOB_CONFIG_MAP = {
    "threshold":       ("scene_selection", "threshold"),
    "max_scene":       ("scene_selection", "max_scene_sec"),
    "per_file":        ("scene_selection", "max_per_file_sec"),
    "min_take":        ("scene_selection", "min_take_sec"),
    "sd_threshold":    ("scene_detection", "threshold"),
    "sd_min_scene":    ("scene_detection", "min_scene_len"),
    "target_minutes":  ("job", "target_minutes"),
    "cameras":      ("job", "cameras"),
    "cam_a":        ("job", "cam_a"),   # legacy
    "cam_b":        ("job", "cam_b"),   # legacy
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
    "shorts_text":  ("shorts",  "text_overlays"),
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
                elif field == "sd_min_scene":
                    # Keep as string with "s" suffix (e.g. "10s") — not a plain float
                    v = float(raw.rstrip('s').strip())
                    result[field] = f"{int(v) if v == int(v) else v}s"
                elif field in ("threshold", "max_scene", "per_file", "target_minutes", "sd_threshold"):
                    result[field] = float(raw.rstrip('s').strip())
                elif field == "cameras":
                    result[field] = [c.strip() for c in raw.split(",") if c.strip()]
                else:
                    # Restore \n escapes to actual newlines for display in textarea
                    result[field] = raw.strip().replace("\\n", "\n")
                break
            except (configparser.NoSectionError, configparser.NoOptionError):
                continue
    # Synthesize cameras from legacy cam_a/cam_b if not explicitly stored
    if not result.get("cameras"):
        legacy = [c for c in [result.get("cam_a"), result.get("cam_b")] if c]
        if legacy:
            result["cameras"] = legacy
    return result


def _read_scenes_csv(csv_path: Path) -> "pd.DataFrame":
    """Read a PySceneDetect *-Scenes.csv tolerating 1- or 2-row headers."""
    sdf = pd.read_csv(csv_path)
    if "Scene Number" not in sdf.columns:
        sdf = pd.read_csv(csv_path, skiprows=1)
    return sdf


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
        if section == "clip_prompts":
            continue  # handled separately by save_prompts_to_config (multiline format)
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
            sv = sv.rstrip('s').strip() + 's'  # ensure PySceneDetect format e.g. "10s"
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
    # Remove ALL existing [clip_prompts] sections (handles duplicates)
    cleaned = re.sub(r'\[clip_prompts\].*?(?=\n\[|\Z)', '', content, flags=re.DOTALL)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    cfg_path.write_text(new_section + ("\n\n" + cleaned if cleaned else "") + "\n")


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
    # Sync in-memory job params so that the next render picks up changed values
    # (e.g. title, no_intro) without requiring a full re-analyze.
    for job in jobs.values():
        if Path(job.params.get("work_dir", "")).resolve() == work_dir:
            for k, v in data.items():
                if k != "work_dir":
                    job.params[k] = v
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
    if d.get("sd_threshold") is None:
        d["sd_threshold"] = cfg_chain[0].get("sd_threshold") or _gf("scene_detection", "threshold", 20)
    if d.get("sd_min_scene") is None:
        raw = cfg_chain[0].get("sd_min_scene")   # already "10s" string from read_job_config
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
    """Reject camera paths that escape work_dir via path traversal."""
    for cam in (cameras or []):
        if cam:
            resolved = (work_dir / cam).resolve()
            if not str(resolved).startswith(str(work_dir.resolve())):
                raise HTTPException(400, f"Invalid camera path: {cam}")


@app.post("/api/jobs")
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

    # Reuse an existing idle job for this directory (avoids duplicates when
    # draft creation and Analyze click race against each other)
    existing_idle = next(
        (j for j in jobs.values() if j.params.get("work_dir") == str(work_dir) and j.status == "idle"),
        None,
    )
    if existing_idle:
        if draft:
            return {"id": existing_idle.id}
        # Promote the idle job to running
        existing_idle.params      = d
        existing_idle.log         = []
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


@app.get("/api/jobs")
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
    _validate_cameras(params.cameras, work_dir)
    _validate_cameras([params.cam_a, params.cam_b], work_dir)

    d = _resolve_params(params.model_dump(), work_dir)
    d["work_dir"] = str(work_dir)
    save_job_config(work_dir, d)
    if params.positive or params.negative:
        save_prompts_to_config(work_dir / "config.ini", params.positive or "", params.negative or "")

    # Reset job in-place
    job.params         = d
    job.log            = []
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


@app.post("/api/jobs/{job_id}/estimate")
async def estimate_job(job_id: str, body: dict = Body({})):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        raise HTTPException(409, "Job is running")
    merged = {**job.params, **body}
    result = await pipeline.estimate(merged, job.work_dir())
    if not result:
        raise HTTPException(400, "No scores CSV — run analysis first")
    return result


@app.post("/api/jobs/{job_id}/find-threshold")
async def find_threshold_job(job_id: str, body: dict = Body({})):
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
            async for update in pipeline.find_threshold_iter(merged, job.work_dir(), target_sec):
                _threshold_searches[search_id] = update
        except asyncio.CancelledError:
            _threshold_searches[search_id] = {"done": True, "cancelled": True}
        finally:
            _threshold_tasks.pop(search_id, None)

    task = asyncio.create_task(_run())
    _threshold_tasks[search_id] = task
    return {"search_id": search_id}


@app.get("/api/threshold-search/{search_id}")
async def get_threshold_search(search_id: str):
    st = _threshold_searches.get(search_id)
    if st is None:
        raise HTTPException(404)
    return st


@app.delete("/api/threshold-search/{search_id}")
async def cancel_threshold_search(search_id: str):
    task = _threshold_tasks.pop(search_id, None)
    if task and not task.done():
        task.cancel()
    _threshold_searches.pop(search_id, None)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/start-proxy")
async def start_proxy(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    # If already running, return current status
    existing = _proxy_tasks.get(job_id)
    if existing and not existing.done():
        return {"ok": True, "already_running": True}

    _proxy_status[job_id] = {"done": False, "total": 0, "finished": 0, "current_file": ""}

    async def _run():
        try:
            async for update in pipeline.create_proxy(job.params, job.work_dir()):
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


@app.get("/api/jobs/{job_id}/proxy-status")
async def get_proxy_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    st = _proxy_status.get(job_id)
    if st is None:
        return {"done": False, "total": 0, "finished": 0, "current_file": "", "not_started": True}
    return st


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    d = job.to_dict()
    # Fill scene-selection defaults for jobs saved before _resolve_params was introduced
    try:
        work_dir = job.work_dir()
        if work_dir and work_dir.is_dir():
            d["params"] = _resolve_params(dict(d["params"]), work_dir)
    except Exception:
        pass
    return d


@app.get("/api/jobs/{job_id}/analyze-result")
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
    # Always overlay actual selection results from log (select_scenes.py output).
    # These are more accurate than the analysis-phase estimates.
    # Parse ALL matching lines so we get the LAST occurrence (most recent run).
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
    # Last-resort fallback: parse what we can from scores CSV
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
    # Compute scene_count + estimated_duration from scores CSV if not yet available
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
                # avg scene duration from PySceneDetect CSVs — cap by per_file and max_scene
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


class RenderParams(BaseModel):
    selected_track: Optional[str] = None
    threshold: Optional[float] = None
    max_scene: Optional[float] = None
    per_file: Optional[float] = None


@app.post("/api/jobs/{job_id}/render")
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
    if params.threshold is not None or params.max_scene is not None or params.per_file is not None:
        job.save()

    track = params.selected_track
    if track:
        tp = Path(track).resolve()
        if not str(tp).startswith(str(BROWSE_ROOT)):
            raise HTTPException(403, "Track path outside allowed root")
        if not tp.exists():
            raise HTTPException(400, f"Track not found: {track}")

    job.log.append("")
    job.log.append("── Render phase ──────────────────────────")
    job.status         = "queued"
    job.phase          = "rendering"
    job.ended_at       = None
    job.selected_track = track
    job.save()

    job._task = asyncio.create_task(_run_job(job, analyze_only=False, selected_track=track))
    return {"id": job_id, "phase": "rendering"}


async def _run_shorts(job: Job):
    """Run make_shorts.py for the given job, streaming output to job log."""
    async with job_semaphore:
        job.status = "running"
        job.phase  = "shorts"
        await job.broadcast({"type": "status", "status": "running", "phase": "shorts"})
        job.save()
        try:
            cmd = [
                sys.executable,
                str(SCRIPT_DIR / "make_shorts.py"),
                job.params["work_dir"],
            ]
            if job.params.get("shorts_text"):
                cmd.append("--text")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(SCRIPT_DIR),
            )
            job.process = proc
            async for raw in proc.stdout:
                for part in raw.decode("utf-8", errors="replace").split("\r"):
                    line = part.rstrip("\n").rstrip()
                    if line:
                        is_progress = bool(re.search(r'^\s*\d+%\||\[[\u2588\u2591 ]+\]\s*\d+%|\b\d+%\|', line))
                        if not is_progress:
                            job.log.append(line)
                        await job.broadcast({"type": "log", "line": line})
            await proc.wait()
            if proc.returncode == 0:
                job.status = "done"
                job.phase  = "done"
            else:
                job.log.append(f"ERROR: make_shorts.py exited with code {proc.returncode}")
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
            _prom_jobs_total.labels(phase="shorts", status=job.status).inc()
            _prom_job_duration.labels(phase="shorts").observe(job.ended_at - job.started_at)
        job.save()
        await job.broadcast({"type": "status", "status": job.status, "phase": job.phase})


@app.post("/api/jobs/{job_id}/render-short")
async def render_short(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job.status in ("running", "queued"):
        raise HTTPException(409, "Job is already running or queued")
    job.log.append("")
    job.log.append("── Render Short ──────────────────────────")
    job.status = "queued"
    job.phase  = "shorts"
    job.ended_at = None
    job.save()
    job._task = asyncio.create_task(_run_shorts(job))
    return {"id": job_id, "phase": "shorts"}


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


@app.patch("/api/jobs/{job_id}/params")
async def patch_job_params(job_id: str, data: dict = Body(...)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    allowed = {"threshold", "max_scene", "per_file", "music_dir"}
    for k, v in data.items():
        if k in allowed:
            job.params[k] = v
    job.save()
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
    await websocket.send_text(json.dumps({"type": "status", "status": job.status, "phase": job.phase}))

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

    # Build scene-duration lookup.
    # Prefer duration_cache.json (actual ffprobe values, built during DRY_RUN).
    # PySceneDetect CSV "Length (seconds)" is inflated ~10x for VFR files due to
    # container timebase mismatch, so it's only used when no cache entry exists.
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
            # Cache keys have .mp4 suffix; strip it. Override CSV values with accurate probed durations.
            for k, v in raw.items():
                durations[k.removesuffix(".mp4")] = v
        except Exception:
            pass

    df = pd.read_csv(scores_csv).sort_values("scene")
    df = df.dropna(subset=["score"])

    # Normalize scores per-camera if dual-cam (mirrors select_scenes.py)
    cam_sources_csv = job.auto_dir() / "camera_sources.csv"
    avg_back_cam_take_sec = None
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

        # Compute avg back-cam clip take (capped at max_scene) for duration estimation
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

    frames = [
        {
            "scene":     row["scene"],
            "score":     round(float(row["score"]), 4),
            "duration":  durations.get(row["scene"]),
            "camera":    row.get("camera") if "camera" in df.columns else None,
            "frame_url": str(frames_dir / (row['scene'] + '.jpg'))
                         if (frames_dir / (row["scene"] + ".jpg")).exists() else None,
        }
        for _, row in df.iterrows()
    ]
    return {"frames": frames, "back_cam": {"avg_take_sec": avg_back_cam_take_sec}}


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
                "url":          f"/api/file?path={p}",
                "size_mb":      round(p.stat().st_size / 1_048_576, 1),
                "duration_sec": _probe_duration(p),
            }

    # Versioned music mixes — newest version first
    def _ver(p: Path) -> int:
        m = re.search(r'_v(\d+)$', p.stem)
        return int(m.group(1)) if m else 0

    auto_dir = work_dir / "_autoframe"
    out_name = pipeline._output_name(work_dir)
    seen: set[str] = set()
    for pat in (f"{out_name}_v*.mp4", f"{out_name}-short_v*.mp4"):
        for p in sorted(work_dir.glob(pat), key=_ver, reverse=True):
            if p.name not in seen:
                seen.add(p.name)
                _add(p)

    # Attach stored YouTube URLs
    yt_urls = _read_yt_urls(auto_dir)
    for name in files:
        if name in yt_urls:
            files[name]["yt_url"] = yt_urls[name]

    return files


@app.delete("/api/jobs/{job_id}/result-file")
async def delete_result_file(job_id: str, filename: str = Query(...)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    work_dir = job.work_dir()
    # Only allow deleting files inside work_dir or _autoframe subdir
    candidates = [work_dir / filename, work_dir / "_autoframe" / filename]
    for p in candidates:
        resolved = p.resolve()
        if resolved.parent.resolve() in (work_dir.resolve(), (work_dir / "_autoframe").resolve()):
            if resolved.exists():
                resolved.unlink()
                return {"ok": True}
    raise HTTPException(404, f"File not found: {filename}")


def _yt_urls_path(auto_dir: Path) -> Path:
    return auto_dir / "youtube_urls.json"

def _read_yt_urls(auto_dir: Path) -> dict:
    p = _yt_urls_path(auto_dir)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}

def _write_yt_url(auto_dir: Path, filename: str, url: str) -> None:
    auto_dir.mkdir(exist_ok=True)
    urls = _read_yt_urls(auto_dir)
    urls[filename] = url
    _yt_urls_path(auto_dir).write_text(json.dumps(urls, indent=2))


@app.post("/api/jobs/{job_id}/youtube-url")
async def save_yt_url(job_id: str, payload: dict):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    filename = payload.get("filename", "").strip()
    url = payload.get("url", "").strip()
    if not filename or not url:
        raise HTTPException(400, "filename and url required")
    auto_dir = job.work_dir() / "_autoframe"
    _write_yt_url(auto_dir, filename, url)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/generate-yt-meta")
async def generate_yt_meta(job_id: str, data: dict):
    """Generate YouTube title and description via Claude API."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    project_name = data.get("project_name", "").strip()
    description  = job.params.get("description", "").strip() or ""
    footer       = data.get("footer", "").strip()   # hashtags/links to preserve

    try:
        import anthropic
        client = anthropic.Anthropic()
        ride_info = description if description else "a motorcycle ride"
        user_msg = (
            f"Project: {project_name}\n"
            f"Ride description: {ride_info}\n\n"
            "Write a YouTube title and bilingual description for this motorcycle highlight reel.\n"
            "Format (follow exactly):\n"
            "<title — max 100 chars, no quotes>\n\n"
            "<Polish (Latin script only, no Cyrillic): 2–3 sentences, each on its own line, NO blank lines between sentences>\n\n"
            "<English: 2–3 sentences, each on its own line, NO blank lines between sentences>\n\n"
            "Polish block must use only Latin characters. Single newline between sentences within each block, blank line only between the two language blocks. No hashtags, no URLs."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = msg.content[0].text.strip()
        parts = text.split("\n\n", 1)
        title    = parts[0].strip().lstrip("#").strip()
        body     = parts[1].strip() if len(parts) > 1 else ""
        full_desc = (body + "\n\n" + footer) if footer else body
        return {"ok": True, "title": title, "description": full_desc}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/jobs/{job_id}/save-yt-meta")
async def save_yt_meta(job_id: str, data: dict):
    """Persist YouTube title / description to work_dir/config.ini."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    work_dir = job.work_dir()
    title = data.get("title", "").strip()
    desc  = data.get("desc",  "").strip()
    updates: dict[str, dict[str, str]] = {}
    if title:
        updates.setdefault("youtube", {})["title"] = title
    if desc:
        updates.setdefault("youtube", {})["description"] = desc.replace("\n", "\\n")
    if updates:
        update_config_ini(work_dir / "config.ini", updates)
    return {"ok": True}


# ── YouTube ───────────────────────────────────────────────────────────────────

YT_SECRETS = WEBAPP_DIR / "youtube_client_secrets.json"
YT_TOKEN   = WEBAPP_DIR / "youtube_token.json"
YT_SCOPES  = ["https://www.googleapis.com/auth/youtube"]
if os.getenv("OAUTHLIB_INSECURE_TRANSPORT") is None and not os.getenv("HTTPS_ONLY"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # allow http; set HTTPS_ONLY=1 in prod


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
        import html as _html
        return HTMLResponse(f"<h2>YouTube auth error: {_html.escape(error)}</h2>")
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

        chunksize = 100 * 1024 * 1024  # 100 MB chunks — fewer round-trips, better throughput
        media = MediaFileUpload(str(file_path), chunksize=chunksize, resumable=True)
        req = yt.videos().insert(
            part="snippet,status",
            body={
                "snippet": {"title": payload.get("title", file_path.stem),
                            "description": payload.get("description", "")},
                "status":  {"privacyStatus": payload.get("privacy", "unlisted"),
                            "selfDeclaredMadeForKids": False},
            },
            media_body=media,
        )

        response = None
        _last_bytes = 0
        _last_time  = time.time()
        while response is None:
            status, response = req.next_chunk()
            if status:
                now       = time.time()
                cur_bytes = status.resumable_progress
                dt        = now - _last_time or 0.001
                speed_mbps = (cur_bytes - _last_bytes) * 8 / dt / 1_000_000
                _last_bytes, _last_time = cur_bytes, now
                _yt_uploads[upload_id].update({
                    "pct":       int(status.progress() * 100),
                    "speed_mbps": round(speed_mbps, 1),
                })

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
            yt_url = f"https://youtu.be/{video_id}"
            _yt_uploads[upload_id].update({"status": "done", "pct": 100, "url": yt_url})
            # Persist URL to project so Results tab shows the link
            auto_dir = file_path.parent if file_path.parent.name == "_autoframe" \
                       else file_path.parent / "_autoframe"
            _write_yt_url(auto_dir, file_path.name, yt_url)
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
async def serve_file(request: Request, path: str = Query(...), dl: int = Query(0)):
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
    if dl:
        base_headers["Content-Disposition"] = f'attachment; filename="{p.name}"'

    # Conditional request — return 304 (no body) if client already has the file
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=base_headers)
    if request.headers.get("if-modified-since") == last_mod:
        return Response(status_code=304, headers=base_headers)

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


@app.get("/api/thumb")
async def serve_thumb(request: Request, path: str = Query(...), w: int = Query(320)):
    """Serve a resized JPEG thumbnail, cached alongside the original as .thumbNNN.jpg"""
    from PIL import Image
    import io

    p = Path(path).resolve()
    if not str(p).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not p.exists():
        raise HTTPException(404)
    if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "Not an image")

    w = max(32, min(w, 1920))
    thumb_path = p.with_suffix(f".thumb{w}.jpg")

    # Regenerate if missing or stale
    if not thumb_path.exists() or thumb_path.stat().st_mtime < p.stat().st_mtime:
        def _resize():
            with Image.open(p) as img:
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                ratio = w / img.width
                h = int(img.height * ratio)
                img = img.resize((w, h), Image.LANCZOS)
                img.save(thumb_path, "JPEG", quality=82, optimize=True)
        await asyncio.get_event_loop().run_in_executor(None, _resize)

    stat      = thumb_path.stat()
    etag      = f'"{stat.st_mtime:.6f}-{stat.st_size}"'
    last_mod  = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(stat.st_mtime))
    headers   = {"ETag": etag, "Last-Modified": last_mod, "Cache-Control": "public, max-age=604800"}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    data = thumb_path.read_bytes()
    return Response(content=data, media_type="image/jpeg",
                    headers={**headers, "Content-Length": str(len(data))})
