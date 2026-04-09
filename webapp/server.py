#!/usr/bin/env python3
"""
autoframe web UI — FastAPI backend
Run: uvicorn webapp.server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from webapp.state import (
    jobs,
    _get_session_user,
    ENABLE_AUTH,
    _NO_CACHE_EXTS,
    STATIC_DIR,
    wcfg,
    _stats_subscribers,
    _get_stats,
    _stats_broadcaster,
    _prom_ok,
)

from webapp.routers import auth, config, music, s3, youtube, instagram, files
from webapp.routers import jobs as jobs_router

app = FastAPI(title="autoframe")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Middleware ─────────────────────────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not ENABLE_AUTH:
        return await call_next(request)
    path = request.url.path
    if (path.startswith("/api/auth/") or
            path.startswith("/static/") or
            path.startswith("/ws/") or
            path in ("/", "/favicon.ico", "/metrics")):
        return await call_next(request)
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


# ── Prometheus metrics endpoint ────────────────────────────────────────────────

@app.get("/metrics")
async def prometheus_metrics():
    from fastapi import HTTPException
    if not _prom_ok:
        raise HTTPException(501, "prometheus_client not installed")
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from webapp.state import (
        _prom_jobs_active, _prom_jobs_queued,
        _prom_cpu_pct, _prom_ram_used, _prom_ram_total,
        _prom_gpu_pct, _prom_gpu_vram_used, _prom_gpu_vram_total,
    )
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


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    import time
    import webapp.state as _st

    max_c = int(wcfg("max_concurrent_jobs", "1"))
    _st.job_semaphore = asyncio.Semaphore(max_c)

    requeue: list = []
    for f in sorted(_st.JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            data = json.loads(f.read_text())
            job = _st.Job.from_dict(data)
            if job.status in ("running", "queued"):
                if job.phase in ("analyzing", "rendering"):
                    job.status = "queued"
                    job.ended_at = None
                    job.log.append("[server restarted — re-queuing]")
                    job.save()
                    requeue.append(job)
                else:
                    job.status = "failed"
                    job.log.append("[server restarted — job interrupted]")
                    job.ended_at = job.ended_at or time.time()
                    job.save()
            _st.jobs[job.id] = job
        except Exception:
            pass

    for job in requeue:
        job._task = asyncio.create_task(
            _st._run_job(job,
                         analyze_only=(job.phase == "analyzing"),
                         selected_track=job.selected_track)
        )

    asyncio.create_task(_stats_broadcaster())
    from webapp.routers.instagram import _ig_periodic_refresh
    asyncio.create_task(_ig_periodic_refresh())
    from webapp.routers.youtube import yt_analytics_periodic
    asyncio.create_task(yt_analytics_periodic())


# ── WebSockets ─────────────────────────────────────────────────────────────────

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
    if job.shorts_running:
        await websocket.send_text(json.dumps({"type": "shorts_status", "running": True}))

    if job.status not in ("running", "queued") and not job.shorts_running:
        await websocket.close()
        return

    job.subscribers.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        job.subscribers.discard(websocket)


# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(auth.router)
app.include_router(config.router)
app.include_router(music.router)
app.include_router(s3.router)
app.include_router(youtube.router)
app.include_router(instagram.router)
app.include_router(jobs_router.router)
app.include_router(files.router)
