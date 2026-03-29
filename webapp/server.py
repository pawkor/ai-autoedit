#!/usr/bin/env python3
"""
autoframe web UI — FastAPI backend
Run: uvicorn webapp.server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
import mimetypes
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Safe root for directory browsing
BROWSE_ROOT = Path.home()

app = FastAPI(title="autoframe")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── In-memory job store ────────────────────────────────────────────────────────

class Job:
    def __init__(self, job_id: str, params: dict):
        self.id = job_id
        self.params = params
        self.status = "pending"   # pending | running | done | failed | killed
        self.log: list[str] = []
        self.process: Optional[asyncio.subprocess.Process] = None
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.subscribers: set[WebSocket] = set()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "params": self.params,
            "log": self.log,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    def work_dir(self) -> Path:
        return Path(self.params["work_dir"])

    def auto_dir(self) -> Path:
        subdir = self.params.get("work_subdir", "_autoframe")
        return self.work_dir() / subdir

    async def broadcast(self, msg: dict):
        dead = set()
        for ws in self.subscribers:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.add(ws)
        self.subscribers -= dead


jobs: dict[str, Job] = {}


# ── Job runner ────────────────────────────────────────────────────────────────

def build_command(params: dict) -> list[str]:
    cmd = ["/bin/bash", str(SCRIPT_DIR / "autoframe.sh")]
    if params.get("threshold"):
        cmd += ["--threshold", str(params["threshold"])]
    if params.get("max_scene"):
        cmd += ["--max-scene", str(params["max_scene"])]
    if params.get("per_file"):
        cmd += ["--per-file", str(params["per_file"])]
    if params.get("title"):
        cmd += ["--title", params["title"]]
    if params.get("cam_a"):
        cmd += ["--cam-a", params["cam_a"]]
    if params.get("cam_b"):
        cmd += ["--cam-b", params["cam_b"]]
    if params.get("no_intro"):
        cmd += ["--no-intro"]
    if params.get("no_music"):
        cmd += ["--no-music"]
    if params.get("music_genre"):
        cmd += ["--music-genre", params["music_genre"]]
    if params.get("music_artist"):
        cmd += ["--music-artist", params["music_artist"]]
    return cmd


async def run_job(job: Job):
    job.status = "running"
    await job.broadcast({"type": "status", "status": "running"})

    cmd = build_command(job.params)
    work_dir = str(job.work_dir())

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=work_dir,
        )
        job.process = proc

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            job.log.append(line)
            await job.broadcast({"type": "log", "line": line})

        await proc.wait()
        job.status = "done" if proc.returncode == 0 else "failed"
    except Exception as e:
        job.log.append(f"ERROR: {e}")
        job.status = "failed"

    job.ended_at = time.time()
    await job.broadcast({"type": "status", "status": job.status})


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/browse")
async def browse(path: str = Query(default=None)):
    root = Path(path) if path else BROWSE_ROOT
    try:
        root = root.resolve()
    except Exception:
        raise HTTPException(400, "Invalid path")

    # Security: stay within home
    if not str(root).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Outside allowed root")

    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    return {
        "path": str(root),
        "parent": str(root.parent) if root != BROWSE_ROOT else None,
        "entries": [
            {
                "name": e.name,
                "path": str(e),
                "is_dir": e.is_dir(),
                "has_mp4": e.is_dir() and any(e.glob("*.mp4")),
                "has_autoframe": e.is_dir() and (e / "_autoframe").exists(),
            }
            for e in entries
        ],
    }


class JobParams(BaseModel):
    work_dir: str
    threshold: Optional[float] = None
    max_scene: Optional[float] = None
    per_file: Optional[float] = None
    title: Optional[str] = None
    cam_a: Optional[str] = None
    cam_b: Optional[str] = None
    no_intro: bool = False
    no_music: bool = False
    music_genre: Optional[str] = None
    music_artist: Optional[str] = None
    work_subdir: str = "_autoframe"


@app.post("/api/jobs")
async def create_job(params: JobParams):
    work_dir = Path(params.work_dir).resolve()
    if not work_dir.is_dir():
        raise HTTPException(400, f"Directory not found: {work_dir}")

    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id, params.model_dump())
    job.params["work_dir"] = str(work_dir)
    jobs[job_id] = job

    asyncio.create_task(run_job(job))
    return {"id": job_id}


@app.get("/api/jobs")
async def list_jobs():
    return [
        {
            "id": j.id,
            "status": j.status,
            "work_dir": j.params["work_dir"],
            "started_at": j.started_at,
            "ended_at": j.ended_at,
        }
        for j in sorted(jobs.values(), key=lambda j: -j.started_at)
    ]


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
        await job.broadcast({"type": "status", "status": "killed"})
    return {"ok": True}


@app.websocket("/ws/{job_id}")
async def job_ws(websocket: WebSocket, job_id: str):
    job = jobs.get(job_id)
    if not job:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    # Send existing log on connect
    for line in job.log:
        await websocket.send_text(json.dumps({"type": "log", "line": line}))
    await websocket.send_text(json.dumps({"type": "status", "status": job.status}))

    if job.status in ("done", "failed", "killed"):
        await websocket.close()
        return

    job.subscribers.add(websocket)
    try:
        while True:
            await websocket.receive_text()   # keep alive / ping
    except WebSocketDisconnect:
        job.subscribers.discard(websocket)


@app.get("/api/jobs/{job_id}/frames")
async def job_frames(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    scores_csv = job.auto_dir() / "scene_scores.csv"
    frames_dir = job.auto_dir() / "frames"

    if not scores_csv.exists():
        raise HTTPException(404, "No scores yet")

    df = pd.read_csv(scores_csv)
    result = []
    for _, row in df.iterrows():
        jpg = frames_dir / f"{row['scene']}.jpg"
        result.append({
            "scene": row["scene"],
            "score": round(float(row["score"]), 4),
            "frame_url": f"/api/file?path={jpg}" if jpg.exists() else None,
        })
    return result


@app.get("/api/jobs/{job_id}/result")
async def job_result(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    work_dir = job.work_dir()
    files = {}
    for name in ("highlight.mp4", "highlight_final.mp4"):
        p = work_dir / name
        if p.exists():
            files[name] = {
                "url": f"/api/file?path={p}",
                "size_mb": round(p.stat().st_size / 1_048_576, 1),
            }
    # Music variants
    for name in ("highlight_music.mp4", "highlight_final_music.mp4"):
        p = work_dir / name
        if p.exists():
            files[name] = {
                "url": f"/api/file?path={p}",
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

    file_size = p.stat().st_size
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"

    range_header = request.headers.get("range")
    if range_header:
        # Parse "bytes=start-end"
        try:
            byte_range = range_header.replace("bytes=", "").split("-")
            start = int(byte_range[0])
            end = int(byte_range[1]) if byte_range[1] else file_size - 1
        except Exception:
            raise HTTPException(416)

        end = min(end, file_size - 1)
        chunk_size = end - start + 1

        async def range_stream():
            async with aiofiles.open(str(p), "rb") as f:
                await f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    data = await f.read(min(65536, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            range_stream(),
            status_code=206,
            media_type=mime,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
            },
        )

    # Full file
    async def full_stream():
        async with aiofiles.open(str(p), "rb") as f:
            while True:
                data = await f.read(65536)
                if not data:
                    break
                yield data

    return StreamingResponse(
        full_stream(),
        media_type=mime,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
    )
