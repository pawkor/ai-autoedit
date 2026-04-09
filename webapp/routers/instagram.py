"""Instagram Reels routes: /api/ig/*, _ig_periodic_refresh, all _ig_* helpers."""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Body

from webapp.state import (
    WEBAPP_DIR,
    BROWSE_ROOT,
)

router = APIRouter()

# ── Instagram credentials ─────────────────────────────────────────────────────

_IG_TOKEN      = os.environ.get("IG_ACCESS_TOKEN", "")
_IG_APP_ID     = os.environ.get("IG_APP_ID", "")
_IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "")
_IG_USER_ID    = os.environ.get("IG_USER_ID", "")
_IG_API_VER    = "v20.0"
_IG_GRAPH      = f"https://graph.facebook.com/{_IG_API_VER}"

_IG_TOKEN_FILE       = WEBAPP_DIR / "ig_token.json"
_IG_LAST_UPLOAD_FILE = WEBAPP_DIR / "ig_last_upload.json"

_ig_uploads: dict[str, dict] = {}


def _ig_urls_path(auto_dir: Path) -> Path:
    return auto_dir / "ig_urls.json"


def _read_ig_urls(auto_dir: Path) -> dict:
    p = _ig_urls_path(auto_dir)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _write_ig_url(auto_dir: Path, filename: str, url: str) -> None:
    auto_dir.mkdir(exist_ok=True)
    urls = _read_ig_urls(auto_dir)
    urls[filename] = url
    _ig_urls_path(auto_dir).write_text(json.dumps(urls, indent=2))


def _ig_token_data() -> dict:
    try:
        if _IG_TOKEN_FILE.exists():
            return json.loads(_IG_TOKEN_FILE.read_text())
    except Exception:
        pass
    return {"token": _IG_TOKEN, "expires_at": None}


def _ig_current_token() -> str:
    d = _ig_token_data()
    return d.get("token") or _IG_TOKEN


def _ig_days_until_expiry() -> Optional[float]:
    d = _ig_token_data()
    exp = d.get("expires_at")
    if not exp:
        return None
    return (exp - time.time()) / 86400


def _ig_configured() -> bool:
    return bool(_ig_current_token() and _IG_USER_ID)


def _ig_refresh_token_sync() -> dict:
    import urllib.request as _ur
    token = _ig_current_token()
    url = (f"https://graph.instagram.com/refresh_access_token"
           f"?grant_type=ig_refresh_token&access_token={token}")
    with _ur.urlopen(url, timeout=30) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message", str(resp["error"])))
    new_token  = resp["access_token"]
    expires_in = int(resp.get("expires_in", 5183944))
    expires_at = time.time() + expires_in
    data = {"token": new_token, "expires_at": expires_at, "refreshed_at": time.time()}
    _IG_TOKEN_FILE.write_text(json.dumps(data))
    return data


async def _ig_maybe_refresh():
    """Refresh token if it expires within 5 days. Called at startup and periodically."""
    if not _ig_current_token():
        return
    days = _ig_days_until_expiry()
    if days is None:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _ig_graph_get,
                f"{_IG_GRAPH}/me",
                {"fields": "id", "access_token": _ig_current_token()},
            )
        except Exception:
            pass
        return
    if days <= 5:
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _ig_refresh_token_sync)
            new_days = (data["expires_at"] - time.time()) / 86400
            print(f"[IG] Token refreshed — expires in {new_days:.0f} days", flush=True)
        except Exception as exc:
            print(f"[IG] Token refresh failed: {exc}", flush=True)


async def _ig_periodic_refresh():
    """Run _ig_maybe_refresh at startup and every 24 h."""
    while True:
        await _ig_maybe_refresh()
        await asyncio.sleep(86400)


def _ig_min_hours() -> float:
    import configparser as _cp
    from webapp.state import APP_DIR
    cfg = _cp.ConfigParser()
    cfg.read([APP_DIR / "config.ini"])
    try:
        return float(cfg.get("instagram", "min_hours_between_uploads", fallback="4"))
    except Exception:
        return 4.0


def _ig_last_time() -> Optional[float]:
    try:
        return json.loads(_IG_LAST_UPLOAD_FILE.read_text()).get("last_upload")
    except Exception:
        return None


def _ig_save_last_time():
    _IG_LAST_UPLOAD_FILE.write_text(json.dumps({"last_upload": time.time()}))


def _ig_graph_post(url: str, params: dict) -> dict:
    import urllib.request as _ur
    import urllib.parse as _up
    body = _up.urlencode(params).encode()
    with _ur.urlopen(_ur.Request(url, data=body, method="POST"), timeout=30) as r:
        return json.loads(r.read())


def _ig_graph_get(url: str, params: dict) -> dict:
    import urllib.request as _ur
    import urllib.parse as _up
    with _ur.urlopen(f"{url}?{_up.urlencode(params)}", timeout=30) as r:
        return json.loads(r.read())


def _ig_upload_bytes(upload_uri: str, file_path: Path) -> dict:
    import urllib.request as _ur
    file_size = file_path.stat().st_size
    with open(file_path, "rb") as fh:
        data = fh.read()
    req = _ur.Request(upload_uri, data=data, method="POST")
    req.add_header("Authorization", f"OAuth {_ig_current_token()}")
    req.add_header("offset", "0")
    req.add_header("file_size", str(file_size))
    req.add_header("Content-Type", "application/octet-stream")
    with _ur.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


@router.get("/api/ig/status")
async def ig_status():
    last    = _ig_last_time()
    min_h   = _ig_min_hours()
    cooling = 0.0
    if last:
        elapsed_h = (time.time() - last) / 3600
        cooling = max(0.0, min_h - elapsed_h)
    return {
        "configured":           _ig_configured(),
        "last_upload":          last,
        "min_hours":            min_h,
        "cooldown_remaining_h": round(cooling, 2),
        "ready":                cooling == 0.0,
        "days_until_expiry":    _ig_days_until_expiry(),
    }


@router.post("/api/ig/upload")
async def ig_upload_start(data: dict = Body(...)):
    if not _ig_configured():
        raise HTTPException(400, "Instagram not configured — set IG_ACCESS_TOKEN and IG_USER_ID in .env")

    last  = _ig_last_time()
    min_h = _ig_min_hours()
    if last and min_h > 0:
        elapsed_h = (time.time() - last) / 3600
        if elapsed_h < min_h:
            rem_min = round((min_h - elapsed_h) * 60)
            raise HTTPException(429, f"Cooldown active — wait {rem_min} more minutes")

    file_path = data.get("file_path", "")
    caption   = data.get("caption", "")
    p = Path(file_path).resolve()
    if not p.exists():
        raise HTTPException(400, "File not found")
    if not str(p).startswith(str(BROWSE_ROOT)):
        raise HTTPException(403)

    upload_id = str(uuid.uuid4())[:8]
    _ig_uploads[upload_id] = {"status": "pending", "message": "Submitting…"}
    asyncio.create_task(_do_ig_upload(upload_id, p, caption))
    return {"upload_id": upload_id}


async def _do_ig_upload(upload_id: str, file_path: Path, caption: str):
    loop  = asyncio.get_event_loop()
    state = _ig_uploads[upload_id]
    try:
        # Step 1 — create resumable container
        state.update(status="uploading", message="Initialising upload…")
        resp = await loop.run_in_executor(None, _ig_graph_post,
            f"{_IG_GRAPH}/{_IG_USER_ID}/media",
            {"media_type": "REELS", "upload_type": "resumable",
             "caption": caption, "access_token": _ig_current_token()},
        )
        if "error" in resp:
            raise RuntimeError(resp["error"].get("message", str(resp["error"])))
        container_id = resp["id"]
        upload_uri   = resp["uri"]

        # Step 2 — upload video bytes directly to Instagram
        size_mb = round(file_path.stat().st_size / 1_048_576, 1)
        state["message"] = f"Uploading {size_mb} MB to Instagram…"
        upload_result = await loop.run_in_executor(None, _ig_upload_bytes, upload_uri, file_path)
        if not upload_result.get("success"):
            raise RuntimeError(f"Upload rejected: {upload_result}")

        # Step 3 — poll until FINISHED
        state["message"] = "Processing on Instagram…"
        for _ in range(72):
            await asyncio.sleep(10)
            info = await loop.run_in_executor(None, _ig_graph_get,
                f"{_IG_GRAPH}/{container_id}",
                {"fields": "status_code,status", "access_token": _ig_current_token()},
            )
            sc = info.get("status_code", "")
            state["message"] = f"Processing on Instagram… ({sc})"
            if sc == "FINISHED":
                break
            if sc in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"IG processing error: {info.get('status', sc)}")
        else:
            raise RuntimeError("Timed out waiting for IG video processing (12 min)")

        # Step 4 — publish
        state["message"] = "Publishing…"
        result = await loop.run_in_executor(None, _ig_graph_post,
            f"{_IG_GRAPH}/{_IG_USER_ID}/media_publish",
            {"creation_id": container_id, "access_token": _ig_current_token()},
        )
        if "error" in result:
            raise RuntimeError(result["error"].get("message", str(result["error"])))

        reel_url = f"https://www.instagram.com/reel/{result['id']}/"
        _ig_save_last_time()
        try:
            _write_ig_url(file_path.parent / "_autoframe", file_path.name, reel_url)
        except Exception:
            pass
        state.update(status="done", message="Published!", media_id=result["id"], url=reel_url)
    except Exception as exc:
        state.update(status="error", message=str(exc))


@router.get("/api/ig/upload/{upload_id}")
async def ig_upload_status(upload_id: str):
    s = _ig_uploads.get(upload_id)
    if not s:
        raise HTTPException(404)
    return s
