"""Config / settings / about routes."""

import asyncio
import configparser
import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from webapp.state import (
    APP_DIR,
    SCRIPT_DIR,
    STATIC_DIR,
    BROWSE_ROOT,
    DATA_ROOT,
    wcfg,
    save_wcfg,
    jobs,
)

router = APIRouter()


@router.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    p = STATIC_DIR / "favicon.ico"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p))


@router.get("/api/config")
async def get_config():
    return {
        "browse_root":          str(BROWSE_ROOT),
        "data_root":            str(DATA_ROOT) if DATA_ROOT else None,
        "data_root_configured": DATA_ROOT is not None,
    }


@router.post("/api/config/data-root")
async def set_data_root(data: dict):
    import webapp.state as _st
    path = data.get("path", "").strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(400, "Invalid directory")
    if not str(Path(path).resolve()).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Outside allowed root")
    save_wcfg({"data_root": path})
    _st.DATA_ROOT = Path(path)
    return {"ok": True}


@router.get("/api/settings")
async def get_settings():
    return {
        "max_concurrent_jobs":  int(wcfg("max_concurrent_jobs",  "1")),
        "max_detect_workers":   int(wcfg("max_detect_workers",   str(os.cpu_count() or 4))),
        "clip_batch_size":      int(wcfg("clip_batch_size",      "64")),
        "clip_workers":         int(wcfg("clip_workers",         "4")),
        "port":                 int(wcfg("port", "8000")),
        "theme":                wcfg("theme", ""),
        "lang":                 wcfg("lang", ""),
        "sort_newest":          wcfg("sort_newest", ""),
    }


@router.put("/api/settings")
async def put_settings(data: dict):
    save_wcfg(data)
    return {"ok": True, "note": "restart server for max_concurrent_jobs to take effect"}


@router.post("/api/about")
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


@router.post("/api/jobs/{job_id}/save-prompts")
async def save_job_prompts(job_id: str, data: dict):
    from webapp.routers.jobs import save_prompts_to_config
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


@router.post("/api/save-prompts")
async def save_prompts(data: dict):
    from webapp.routers.jobs import save_prompts_to_config
    work_dir = data.get("work_dir", "").strip()
    if not work_dir or not Path(work_dir).is_dir():
        raise HTTPException(400, f"work_dir not found: {work_dir}")
    positive = data.get("positive", "").strip()
    negative = data.get("negative", "").strip()
    save_prompts_to_config(Path(work_dir) / "config.ini", positive, negative)
    return {"ok": True}


@router.get("/api/job-config")
async def get_job_config(dir: str):
    from webapp.routers.jobs import read_job_config
    work_dir = Path(dir).resolve()
    if not str(work_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    result = read_job_config(work_dir)
    result["_resolved"] = str(work_dir)
    work_subdir = result.get("work_subdir") or "_autoframe"
    result["_has_processed"] = (work_dir / work_subdir).is_dir() or any(work_dir.glob("highlight*.mp4"))
    return result


@router.put("/api/job-config")
async def put_job_config(data: dict):
    from webapp.routers.jobs import save_job_config
    work_dir = Path(data.get("work_dir", "")).resolve()
    if not str(work_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not work_dir.is_dir():
        raise HTTPException(400, "work_dir not found")
    save_job_config(work_dir, data)
    for job in jobs.values():
        if Path(job.params.get("work_dir", "")).resolve() == work_dir:
            for k, v in data.items():
                if k != "work_dir":
                    job.params[k] = v
    return {"ok": True}
