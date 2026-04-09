"""Music routes: rebuild index, list/delete files, ACR check, yt-dlp download."""

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import StreamingResponse

from webapp.state import (
    SCRIPT_DIR,
    BROWSE_ROOT,
    _rebuild_tasks,
)

router = APIRouter()

# ── ACRCloud credentials ──────────────────────────────────────────────────────

_ACR_HOST   = os.environ.get("ACRCLOUD_HOST", "")
_ACR_KEY    = os.environ.get("ACRCLOUD_ACCESS_KEY", "")
_ACR_SECRET = os.environ.get("ACRCLOUD_ACCESS_SECRET", "")


@router.post("/api/music-rebuild")
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


@router.get("/api/music-rebuild-status/{task_id}")
async def music_rebuild_status(task_id: str):
    task = _rebuild_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/api/music-files")
async def music_files_endpoint(dir: str = Query(...)):
    d = Path(dir).expanduser().resolve()
    if not str(d).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Outside allowed root")
    idx = d / "index.json"
    if idx.exists():
        tracks = json.loads(idx.read_text())
        return sorted(tracks, key=lambda t: t.get("title", "").lower())
    return sorted(
        [{"file": str(f), "title": f.stem, "genre": "", "duration": 0, "bpm": 0, "energy_norm": 0}
         for f in d.glob("*.mp3")],
        key=lambda t: t["title"].lower()
    )


@router.delete("/api/music-file")
async def delete_music_file(path: str = Query(...)):
    p = Path(path).resolve()
    if not str(p).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)
    if not p.exists():
        raise HTTPException(404)

    for idx_dir in {p.parent, p.parent.parent}:
        idx_path = idx_dir / "index.json"
        if idx_path.exists():
            try:
                tracks = json.loads(idx_path.read_text())
                new_tracks = [t for t in tracks if Path(t.get("file", "")).resolve() != p]
                if len(new_tracks) != len(tracks):
                    idx_path.write_text(json.dumps(new_tracks, indent=2))
            except Exception:
                pass

    for hist_dir in {p.parent, p.parent.parent}:
        used_path = hist_dir / "shorts_used.json"
        if used_path.exists():
            try:
                used = json.loads(used_path.read_text())
                new_used = [u for u in used if Path(u).resolve() != p]
                if len(new_used) != len(used):
                    used_path.write_text(json.dumps(new_used))
            except Exception:
                pass

    p.unlink()
    return {"ok": True}


async def _acr_fingerprint(path: Path) -> dict:
    """Fingerprint an audio file via ACRCloud. Returns result dict."""
    import hmac, base64
    import urllib.request

    tmp = Path(tempfile.mktemp(suffix=".wav"))
    try:
        for offset in (60, 0):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", str(offset), "-t", "10",
                "-i", str(path), "-ar", "8000", "-ac", "1", "-f", "wav", str(tmp),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if tmp.exists() and tmp.stat().st_size > 100:
                break
        audio_data = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)

    timestamp = str(int(time.time()))
    string_to_sign = "\n".join(["POST", "/v1/identify", _ACR_KEY, "audio", "1", timestamp])
    sign = base64.b64encode(
        hmac.new(_ACR_SECRET.encode(), string_to_sign.encode(), digestmod="sha1").digest()
    ).decode()

    boundary = "----ACRBoundary"
    def _field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    body = b"".join([
        _field("access_key", _ACR_KEY),
        _field("sample_bytes", str(len(audio_data))),
        _field("timestamp", timestamp),
        _field("signature", sign),
        _field("data_type", "audio"),
        _field("signature_version", "1"),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"sample\"; filename=\"sample.wav\"\r\n"
         f"Content-Type: audio/wav\r\n\r\n").encode() + audio_data + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])

    req = urllib.request.Request(
        f"https://{_ACR_HOST}/v1/identify", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    loop = asyncio.get_event_loop()
    try:
        raw = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        raise RuntimeError(f"ACRCloud request failed: {e}")

    result = json.loads(raw)
    status = result.get("status", {})
    if status.get("code") == 0:
        music  = result.get("metadata", {}).get("music", [{}])[0]
        artists = ", ".join(a["name"] for a in music.get("artists", []))
        rights  = music.get("rights_claim", [])
        blocked = any(r.get("rights_owner_name") for r in rights)
        ext    = music.get("external_metadata", {})
        yt_vid = (ext.get("youtube") or [{}])[0].get("vid", "") if isinstance(ext.get("youtube"), list) else ext.get("youtube", {}).get("vid", "")
        return {
            "matched": True,
            "title":   music.get("title", ""),
            "artists": artists,
            "label":   music.get("label", ""),
            "score":   music.get("score", 0),
            "yt_video_id": yt_vid,
            "rights":  rights,
            "blocked": blocked,
        }
    elif status.get("code") == 1001:
        return {"matched": False, "msg": "No music detected"}
    else:
        return {"matched": False, "msg": status.get("msg", "Unknown")}


@router.post("/api/music/acr-check")
async def acr_check(data: dict = Body(...)):
    """Fingerprint an audio file against ACRCloud to detect Content ID claims."""
    if not (_ACR_HOST and _ACR_KEY and _ACR_SECRET):
        raise HTTPException(400, "ACRCloud credentials not configured")
    path = Path(data.get("path", "")).resolve()
    if not str(path).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "Outside allowed root")
    if not path.exists():
        raise HTTPException(404, "File not found")
    try:
        result = await _acr_fingerprint(path)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    # Persist ACR result into index.json so it survives page reloads
    idx_path = path.parent / "index.json"
    if idx_path.exists():
        try:
            tracks = json.loads(idx_path.read_text())
            for t in tracks:
                if Path(t.get("file", "")).resolve() == path:
                    t["acr_matched"] = result.get("matched", False)
                    t["acr_blocked"] = result.get("blocked", False)
                    if result.get("matched"):
                        info = f"{result.get('artists','')} — {result.get('title','')} ({result.get('label','')}) score:{result.get('score','')}"
                        rights = result.get("rights", [])
                        if rights:
                            info += "\nRights: " + json.dumps(rights)
                        t["acr_info"] = info
                    break
            idx_path.write_text(json.dumps(tracks, indent=2))
        except Exception:
            pass

    return result


@router.get("/api/acr-status")
async def acr_status():
    return {"configured": bool(_ACR_HOST and _ACR_KEY and _ACR_SECRET)}


@router.get("/api/music/yt-download")
async def yt_download_sse(url: str = Query(...)):
    """SSE: download YouTube audio via yt-dlp, stream progress, return temp file path."""
    async def generate():
        tmp = tempfile.mkdtemp(prefix="ytdl-")
        cmd = [
            "yt-dlp", "--extract-audio", "--audio-format", "mp3",
            "--audio-quality", "0", "--no-playlist", "--newline",
            "--write-info-json",
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


@router.post("/api/music/save-downloaded")
async def music_save_downloaded(data: dict = Body(...)):
    """Move a yt-dlp temp file to the music directory."""
    src = Path(data.get("tmp_path", "")).resolve()
    dst_dir_raw = (data.get("music_dir") or "").strip()
    if not dst_dir_raw:
        raise HTTPException(400, "music_dir required")
    dst_dir = Path(dst_dir_raw).expanduser().resolve()
    if not str(src).startswith(tempfile.gettempdir()):
        raise HTTPException(400, "Source must be a temp file")
    if not src.is_file():
        raise HTTPException(404, "Temp file not found")
    if not str(dst_dir).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403, "music_dir outside allowed root")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))

    yt_meta = {}
    info_src = src.with_suffix(".info.json")
    if info_src.exists():
        try:
            info = json.loads(info_src.read_text())
            yt_meta = {
                "yt_url":     info.get("webpage_url", ""),
                "yt_license": info.get("license") or "",
                "yt_channel": info.get("uploader") or info.get("channel") or "",
            }
            sidecar = dst.with_suffix(".yt.json")
            sidecar.write_text(json.dumps(yt_meta, ensure_ascii=False))
        except Exception:
            pass
        try:
            info_src.unlink()
        except Exception:
            pass

    try:
        src.parent.rmdir()
    except Exception:
        pass
    return {"ok": True, "path": str(dst), "yt_meta": yt_meta}
