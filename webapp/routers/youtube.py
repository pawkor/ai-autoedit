"""YouTube routes: /api/youtube/*, /api/jobs/{id}/youtube-url, generate-yt-meta, save-yt-meta"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

import re

from webapp.state import (
    WEBAPP_DIR,
    BROWSE_ROOT,
    in_browse_root,
    jobs,
    _prom_ok,
)

router = APIRouter()

# ── YouTube OAuth setup ───────────────────────────────────────────────────────

YT_SECRETS = WEBAPP_DIR / "youtube_client_secrets.json"
YT_TOKEN   = WEBAPP_DIR / "youtube_token.json"
YT_SCOPES  = ["https://www.googleapis.com/auth/youtube",
              "https://www.googleapis.com/auth/yt-analytics.readonly"]

if os.getenv("OAUTHLIB_INSECURE_TRANSPORT") is None and not os.getenv("HTTPS_ONLY"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

_yt_uploads: dict = {}   # upload_id → {status, pct, url, error}

# ── YouTube Analytics cache ────────────────────────────────────────────────────
# {date: {video: N, short: N}}  — last 30 days, refreshed every hour
_yt_analytics_cache: dict[str, dict[str, int]] = {}
_yt_analytics_updated: float = 0.0


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


def _iso_duration_seconds(iso: str) -> int:
    """Parse ISO 8601 duration string → total seconds. Returns 0 on error."""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', iso or '')
    if not m:
        return 0
    h, mi, s = m.groups()
    return int(float(h or 0) * 3600 + float(mi or 0) * 60 + float(s or 0))


def _fetch_yt_analytics_sync() -> dict[str, dict[str, int]]:
    """
    Fetch last 30 days of YouTube Analytics and classify views by video vs short.
    Returns {date_str: {"video": N, "short": N}}.
    Runs in a thread executor (blocking googleapis calls).
    """
    from googleapiclient.discovery import build
    from datetime import date, timedelta

    creds = _yt_creds()
    if not creds:
        return {}

    yt = build("youtube", "v3", credentials=creds)
    ya = build("youtubeAnalytics", "v2", credentials=creds)

    # ── Collect all channel video IDs (up to 500) ─────────────────────────────
    video_ids: list[str] = []
    page_token = None
    while len(video_ids) < 500:
        kwargs: dict = dict(part="id", forMine=True, type="video",
                            order="date", maxResults=50)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = yt.search().list(**kwargs).execute()
        for item in resp.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # ── Classify each video: short (≤60 s) vs video ───────────────────────────
    short_ids: set[str] = set()
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        det = yt.videos().list(part="contentDetails", id=",".join(chunk)).execute()
        for item in det.get("items", []):
            dur_secs = _iso_duration_seconds(
                item.get("contentDetails", {}).get("duration", "PT0S")
            )
            if dur_secs <= 60:
                short_ids.add(item["id"])

    end_date   = date.today()
    start_date = end_date - timedelta(days=30)

    # ── Total daily views ─────────────────────────────────────────────────────
    total_resp = ya.reports().query(
        ids="channel==MINE",
        startDate=start_date.isoformat(),
        endDate=end_date.isoformat(),
        metrics="views",
        dimensions="day",
    ).execute()
    total_by_date: dict[str, int] = {
        row[0]: int(row[1]) for row in total_resp.get("rows", [])
    }

    # ── Shorts daily views (filter to short_ids, if any) ─────────────────────
    short_by_date: dict[str, int] = {}
    if short_ids:
        filter_str = "video==" + ",".join(short_ids)
        short_resp = ya.reports().query(
            ids="channel==MINE",
            startDate=start_date.isoformat(),
            endDate=end_date.isoformat(),
            metrics="views",
            dimensions="day",
            filters=filter_str,
        ).execute()
        short_by_date = {
            row[0]: int(row[1]) for row in short_resp.get("rows", [])
        }

    # ── Combine ────────────────────────────────────────────────────────────────
    result: dict[str, dict[str, int]] = {}
    all_dates = sorted(set(total_by_date) | set(short_by_date))
    for d in all_dates:
        total  = total_by_date.get(d, 0)
        shorts = short_by_date.get(d, 0)
        result[d] = {"video": max(0, total - shorts), "short": shorts}

    return result


def _update_yt_prometheus(data: dict[str, dict[str, int]]) -> None:
    """Push analytics data to Prometheus gauges."""
    if not _prom_ok:
        return
    from webapp.state import _prom_yt_daily_views, _prom_yt_latest_views
    if _prom_yt_daily_views is None:
        return
    for d, vals in data.items():
        _prom_yt_daily_views.labels(date=d, type="video").set(vals.get("video", 0))
        _prom_yt_daily_views.labels(date=d, type="short").set(vals.get("short", 0))
    # latest available day stat (YouTube Analytics has ~2-3 day delay)
    if data and _prom_yt_latest_views is not None:
        latest = max(data.keys())
        _prom_yt_latest_views.labels(type="video").set(data[latest].get("video", 0))
        _prom_yt_latest_views.labels(type="short").set(data[latest].get("short", 0))


async def _yt_analytics_refresh_once():
    global _yt_analytics_cache, _yt_analytics_updated
    try:
        data = await asyncio.to_thread(_fetch_yt_analytics_sync)
        if data:
            _yt_analytics_cache = data
            _yt_analytics_updated = time.time()
            _update_yt_prometheus(data)
            print(f"[YT Analytics] refreshed — {len(data)} days", flush=True)
    except Exception as exc:
        print(f"[YT Analytics] refresh error: {exc}", flush=True)


async def yt_analytics_periodic():
    """Refresh YouTube Analytics at startup and every hour."""
    while True:
        await _yt_analytics_refresh_once()
        await asyncio.sleep(3600)


@router.get("/api/youtube/analytics")
async def yt_analytics():
    """Return cached daily views split by video/short (last 30 days)."""
    return {
        "data": _yt_analytics_cache,
        "updated_at": _yt_analytics_updated,
    }


@router.post("/api/youtube/analytics/refresh")
async def yt_analytics_refresh_now():
    """Trigger an immediate analytics refresh."""
    asyncio.create_task(_yt_analytics_refresh_once())
    return {"ok": True}


@router.get("/api/youtube/status")
async def yt_status():
    return {"authenticated": _yt_creds() is not None, "has_secrets": YT_SECRETS.exists()}


@router.get("/api/youtube/auth")
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


@router.get("/api/youtube/callback")
async def yt_callback(code: str = Query(None), error: str = Query(None)):
    import html as _html
    if error:
        return HTMLResponse(f"<h2>YouTube auth error: {_html.escape(error)}</h2><p>Close this tab and try again.</p><script>setTimeout(()=>window.close(),4000)</script>")
    if not code:
        return HTMLResponse("<h2>No authorization code received.</h2><p>Close this tab and try again.</p><script>setTimeout(()=>window.close(),4000)</script>")
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


@router.get("/api/youtube/playlists")
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


@router.post("/api/youtube/upload")
async def yt_upload(payload: dict):
    creds = _yt_creds()
    if not creds:
        raise HTTPException(401, "Not authenticated")
    file_path = Path(payload["file_path"]).resolve()
    if not in_browse_root(file_path):
        raise HTTPException(403, "Access denied")
    if not file_path.exists():
        raise HTTPException(404, "File not found")

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

        chunksize = 100 * 1024 * 1024
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
                    "pct":        int(status.progress() * 100),
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
            auto_dir = file_path.parent if file_path.parent.name == "_autoframe" \
                       else file_path.parent / "_autoframe"
            _write_yt_url(auto_dir, file_path.name, yt_url)
        except Exception as e:
            _yt_uploads[upload_id].update({"status": "error", "error": str(e)})

    asyncio.create_task(_run())
    return {"upload_id": upload_id}


@router.get("/api/youtube/upload/{upload_id}")
async def yt_upload_status(upload_id: str):
    s = _yt_uploads.get(upload_id)
    if not s:
        raise HTTPException(404)
    return s


@router.delete("/api/youtube/disconnect")
async def yt_disconnect():
    YT_TOKEN.unlink(missing_ok=True)
    return {"ok": True}


@router.post("/api/jobs/{job_id}/youtube-url")
async def save_yt_url(job_id: str, payload: dict):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    filename = payload.get("filename", "").strip()
    url = payload.get("url", "").strip()
    if not filename:
        raise HTTPException(400, "filename required")
    auto_dir = job.work_dir() / "_autoframe"
    if url:
        _write_yt_url(auto_dir, filename, url)
    else:
        # Clear: remove key from yt_urls.json
        urls = _read_yt_urls(auto_dir)
        urls.pop(filename, None)
        _yt_urls_path(auto_dir).write_text(json.dumps(urls, indent=2))
    return {"ok": True}


@router.post("/api/jobs/{job_id}/generate-yt-meta")
async def generate_yt_meta(job_id: str, data: dict):
    """Generate YouTube title and description via Claude API."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    project_name = data.get("project_name", "").strip()
    description  = job.params.get("description", "").strip() or ""
    footer       = data.get("footer", "").strip()
    notes        = data.get("notes", "").strip()

    try:
        import anthropic
        client = anthropic.Anthropic()
        ride_info = notes or description or "a motorcycle ride"
        user_msg = (
            f"Project: {project_name}\n"
            f"Ride notes: {ride_info}\n\n"
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


@router.post("/api/jobs/{job_id}/save-yt-meta")
async def save_yt_meta(job_id: str, data: dict):
    """Persist YouTube title / description to work_dir/config.ini."""
    from webapp.routers.jobs import update_config_ini
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    work_dir = job.work_dir()
    title = data.get("title", "").strip()
    desc  = data.get("desc",  "").strip()
    notes = data.get("notes", "").strip()
    updates: dict[str, dict[str, str]] = {}
    if title:
        updates.setdefault("youtube", {})["title"] = title
    if desc:
        updates.setdefault("youtube", {})["description"] = desc.replace("\n", "\\n")
    if notes is not None:
        updates.setdefault("youtube", {})["notes"] = notes
    if updates:
        update_config_ini(work_dir / "config.ini", updates)
    return {"ok": True}
