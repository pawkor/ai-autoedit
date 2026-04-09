"""S3 routes: /api/s3/*, /api/purge-local"""

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import StreamingResponse

from webapp.state import (
    S3_CLIENT,
    S3_BUCKET,
    BROWSE_ROOT,
)

router = APIRouter()

_VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.m4v', '.3gp'}


def _s3_prefix(work_dir: Path) -> str:
    """S3 prefix for a work_dir: relative path from BROWSE_ROOT with trailing slash."""
    try:
        rel = work_dir.resolve().relative_to(BROWSE_ROOT.resolve())
    except ValueError:
        rel = Path(work_dir.name)
    return str(rel).rstrip("/") + "/"


@router.get("/api/s3/status")
async def s3_status():
    return {"configured": S3_CLIENT is not None, "bucket": S3_BUCKET or ""}


@router.get("/api/s3/list")
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


@router.get("/api/s3/upload")
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


@router.get("/api/s3/download")
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


@router.get("/api/s3/source-status")
async def s3_source_status(work_dir: str = Query(...)):
    """List S3 source files vs local for each cam subfolder."""
    if not S3_CLIENT:
        raise HTTPException(503, "S3 not configured")
    wd = Path(work_dir).resolve()
    if not str(wd).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    prefix = _s3_prefix(wd)
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


@router.get("/api/s3/fetch-sources")
async def s3_fetch_sources(work_dir: str = Query(...), keys: str = Query(default="")):
    """SSE: download selected (or all missing) S3 source video files to local work_dir."""
    if not S3_CLIENT:
        async def _err():
            yield 'data: {"error":"S3 not configured"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    wd = Path(work_dir).resolve()
    if not str(wd).startswith(str(BROWSE_ROOT)):
        async def _err():
            yield 'data: {"error":"Access denied"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    selected_keys = None
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


@router.post("/api/purge-local")
async def purge_local(data: dict = Body(...)):
    """Delete local source video files and autocut scenes to free disk space."""
    wd = Path(data.get("work_dir", "")).resolve()
    if not str(wd).startswith(str(BROWSE_ROOT)) or not wd.is_dir():
        raise HTTPException(400, "invalid work_dir")
    removed = 0
    for sub in wd.iterdir():
        if sub.name.startswith("_") or not sub.is_dir():
            continue
        for f in sub.iterdir():
            if f.suffix.lower() in _VIDEO_EXTS and f.is_file():
                f.unlink()
                removed += 1
    autocut = wd / "_autoframe" / "autocut"
    if autocut.is_dir():
        for f in autocut.iterdir():
            if f.is_file():
                f.unlink()
                removed += 1
    return {"ok": True, "removed": removed}
