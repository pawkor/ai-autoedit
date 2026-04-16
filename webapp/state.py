#!/usr/bin/env python3
"""
Shared state for the autoframe webapp.
Imported by routers — never imports from routers (no circular imports).
"""

import asyncio
import configparser
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles  # noqa: F401 — re-exported for router use
import psutil
import tempfile  # noqa: F401 — re-exported for router use

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError as BotoClientError  # noqa: F401
    _boto3_ok = True
except ImportError:
    _boto3_ok = False

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
    _prom_ok = True
except ImportError:
    _prom_ok = False

from fastapi import WebSocket

# ── Paths ─────────────────────────────────────────────────────────────────────

APP_DIR    = Path(__file__).resolve().parent.parent
SCRIPT_DIR = APP_DIR / "src"

sys.path.insert(0, str(SCRIPT_DIR))
import pipeline  # noqa: E402

WEBAPP_DIR  = Path(__file__).resolve().parent
STATIC_DIR  = WEBAPP_DIR / "static"
JOBS_DIR    = WEBAPP_DIR / "jobs"
BROWSE_ROOT = Path(os.environ.get("BROWSE_ROOT", str(Path.home())))

JOBS_DIR.mkdir(exist_ok=True)

_BROWSE_ROOT_RESOLVED: Path | None = None


def in_browse_root(p: Path) -> bool:
    """Return True iff resolved path p is inside BROWSE_ROOT (safe path check)."""
    global _BROWSE_ROOT_RESOLVED
    if _BROWSE_ROOT_RESOLVED is None:
        _BROWSE_ROOT_RESOLVED = BROWSE_ROOT.resolve()
    try:
        p.resolve().relative_to(_BROWSE_ROOT_RESOLVED)
        return True
    except ValueError:
        return False

# ── Auth ──────────────────────────────────────────────────────────────────────

ENABLE_AUTH  = os.environ.get("ENABLE_AUTH", "false").lower() in ("1", "true", "yes")
USERS_FILE   = WEBAPP_DIR / "users.json"
_sessions: dict[str, str] = {}   # token → username


def _hash_pw(password: str) -> str:
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


def _get_session_user(request) -> Optional[str]:
    token = request.cookies.get("ae_session")
    return _sessions.get(token) if token else None


# ── Webapp config ─────────────────────────────────────────────────────────────

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


# ── Data root ─────────────────────────────────────────────────────────────────

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

# ── Job classes ───────────────────────────────────────────────────────────────

class _LogList(list):
    """list[str] that also appends to a .log file immediately on each append()."""
    def __init__(self, path: Path, items: list[str] | None = None):
        super().__init__(items or [])
        self._path = path
        if items:
            try:
                with open(self._path, "a", encoding="utf-8") as fh:
                    for line in items:
                        fh.write(line + "\n")
            except Exception:
                pass

    def append(self, line: str):       # type: ignore[override]
        super().append(line)
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def clear_file(self):
        try:
            self._path.write_text("")
        except Exception:
            pass
        self.clear()


class Job:
    def __init__(self, job_id: str, params: dict):
        self.id          = job_id
        self.params      = params
        self.status      = "queued"
        self.phase       = "analyzing"
        self.log         = _LogList(JOBS_DIR / f"{job_id}.log")
        self.process: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._shorts_task: Optional[asyncio.Task] = None
        self.shorts_running: bool = False
        self._shorts_lock: asyncio.Lock = asyncio.Lock()
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
            "created_at":     self.created_at,
            "started_at":     self.started_at,
            "ended_at":       self.ended_at,
            "analyze_result": self.analyze_result,
            "selected_track": self.selected_track,
            "shorts_running": self.shorts_running,
        }

    def save(self):
        path = JOBS_DIR / f"{self.id}.json"
        path.write_text(json.dumps(self.to_dict()))

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        j = cls(data["id"], data["params"])
        j.status         = data["status"]
        j.phase          = data.get("phase", "done")
        old_log = data.get("log")
        log_path = JOBS_DIR / f"{j.id}.log"
        if old_log and not log_path.exists():
            j.log = _LogList(log_path, old_log)
        else:
            j.log = _LogList(log_path)
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
shorts_semaphore: asyncio.Semaphore = asyncio.Semaphore(4)

# ── Polling state dicts ───────────────────────────────────────────────────────

_threshold_searches: dict[str, dict] = {}
_threshold_tasks:    dict[str, asyncio.Task] = {}

_proxy_tasks:  dict[str, asyncio.Task] = {}
_proxy_status: dict[str, dict] = {}

_rebuild_tasks: dict[str, dict] = {}   # task_id → {progress, total, done, ok}

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
    # YouTube Analytics — daily views (last 30 days), labels: date (YYYY-MM-DD), type (video|short)
    _prom_yt_daily_views    = Gauge("yt_channel_daily_views",              "YouTube daily views by type", ["date", "type"])
    # YouTube Analytics — latest available day (no date label, for stat panels)
    _prom_yt_latest_views   = Gauge("yt_channel_latest_daily_views",       "YouTube latest available day views", ["type"])
else:
    _prom_jobs_active = _prom_jobs_queued = _prom_jobs_total = _prom_job_duration = None
    _prom_cpu_pct = _prom_ram_used = _prom_ram_total = None
    _prom_gpu_pct = _prom_gpu_vram_used = _prom_gpu_vram_total = None
    _prom_yt_daily_views = None
    _prom_yt_latest_views = None

_NO_CACHE_EXTS = {".html", ".js", ".css", ".json", ".txt", ".svg", ".ico"}

# ── System stats ───────────────────────────────────────────────────────────────

_gpu_available: Optional[bool] = None


def _proc_meminfo() -> tuple[int, int]:
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
    """Return (used_bytes, total_bytes) from cgroups, falling back to /proc/meminfo."""
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
    psutil.cpu_percent(interval=None)
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


# ── Job task queue helper ──────────────────────────────────────────────────────

def _enqueue_job_task(job: "Job", coro) -> "asyncio.Task":
    """Schedule coro as job._task.
    If a task is already running/pending, chain coro after it instead of rejecting.
    Returns the new asyncio.Task.
    """
    import asyncio as _aio
    old = job._task
    if old and not old.done():
        async def _chain():
            try:
                await old
            except Exception:
                pass
            await coro
        task = _aio.create_task(_chain())
    else:
        task = _aio.create_task(coro)
    return task


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
                if selected_track:
                    try:
                        import re as _re
                        from datetime import timezone as _tz
                        work_dir = job.work_dir()
                        out_name = pipeline._output_name(work_dir)
                        def _ver(p):
                            m2 = _re.search(r'_v(\d+)$', p.stem)
                            return int(m2.group(1)) if m2 else 0
                        candidates = [p for p in work_dir.glob(f"{out_name}_v*.mp4")
                                      if "_preview" not in p.stem]
                        if candidates:
                            latest = max(candidates, key=_ver)
                            meta = {
                                "music":        selected_track,
                                "generated_at": datetime.now(_tz.utc).isoformat(),
                            }
                            latest.with_suffix(".meta.json").write_text(
                                json.dumps(meta, ensure_ascii=False)
                            )
                            try:
                                from webapp.routers.music import record_used_track
                                _yt = job.params.get("yt_url", "")
                                record_used_track(selected_track, str(work_dir), latest.name, _yt)
                            except Exception:
                                pass
                    except Exception:
                        pass
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
