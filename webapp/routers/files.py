"""File serving routes: /api/file, /api/files, /api/browse, /api/subdirs, /api/mkdir,
/api/upload, /api/thumb, /api/count-sources."""

import asyncio
import mimetypes
import time
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, Form
from fastapi.responses import StreamingResponse, Response

from webapp.state import (
    BROWSE_ROOT,
    DATA_ROOT,
)

router = APIRouter()

_VIDEO_EXTS  = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.m4v', '.3gp'}
_UPLOAD_EXTS = _VIDEO_EXTS | {'.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac'}


@router.get("/api/file")
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

    CHUNK = 2 * 1024 * 1024

    base_headers = {
        "Accept-Ranges":  "bytes",
        "ETag":           etag,
        "Last-Modified":  last_mod,
        "Cache-Control":  "public, max-age=86400",
    }
    if dl:
        base_headers["Content-Disposition"] = f'attachment; filename="{p.name}"'

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
                end = min(int(parts[1]), file_size - 1)
            else:
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


@router.get("/api/thumb")
async def serve_thumb(request: Request, path: str = Query(...), w: int = Query(320)):
    """Serve a resized JPEG thumbnail, cached alongside the original."""
    from PIL import Image

    p = Path(path).resolve()
    if not str(p).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not p.exists():
        raise HTTPException(404)
    if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "Not an image")

    w = max(32, min(w, 1920))
    thumb_path = p.with_suffix(f".thumb{w}.jpg")

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


@router.get("/api/files")
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


@router.delete("/api/file")
async def delete_file_endpoint(path: str = Query(...)):
    f = Path(path).resolve()
    if not str(f).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not f.is_file():
        raise HTTPException(404)
    f.unlink()
    return {"ok": True}


@router.get("/api/browse")
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


@router.get("/api/subdirs")
async def list_subdirs(dir: str = Query(...)):
    """List immediate subdirectories of a path."""
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


@router.post("/api/mkdir")
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


@router.post("/api/upload")
async def upload_file(file: UploadFile, work_dir: str = Form(...)):
    dest_dir = Path(work_dir).resolve()
    if not str(dest_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not dest_dir.is_dir():
        raise HTTPException(400, "Directory not found")
    safe_name = Path(file.filename).name
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


@router.get("/api/count-sources")
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
