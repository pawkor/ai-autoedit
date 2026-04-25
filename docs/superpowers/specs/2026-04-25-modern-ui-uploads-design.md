# Modern UI Phase 6 — YT + IG Upload Design

## Goal

Add full YouTube and Instagram upload capability to the Modern UI Results modal, with complete feature parity to the legacy UI.

## Architecture

**New file:** `webapp/static/js/modern_uploads.js`
All upload logic lives here. Adapted from `services/youtube.js` and `services/instagram.js` but scoped to Modern UI element IDs. No dependency on legacy globals (`currentJobId`, `js-workdir`). `jobId` and `workDir` are passed as parameters when opening modals.

**Modified files:**
- `webapp/static/modern.html` — 3 new modals + `<script src="/js/modern_uploads.js">` after `modern.js`
- `webapp/static/js/modern.js` — upload buttons added in `_renderResultsList()`
- `webapp/static/css/modern.css` — modal styles for upload modals

**Script load order:** `modern_music.js` → `modern_analyze.js` → `modern_shorts.js` → `modern.js` → `modern_uploads.js`

## Upload Buttons in Results List

Added to each result row in `_renderResultsList()` in `modern.js`:

| File type | Button | Condition |
|-----------|--------|-----------|
| Highlight | `▲ YT` | always; if `info.yt_url` → `✓ YT` link (clicking opens modal with existing URL pre-filled) |
| Short | `▲ YT` | always; disabled if no main video has `yt_url` |
| Short, `is_ncs=true` | `▲ IG` | always; if `info.ig_url` → `✓ IG` link |

Buttons call `mYtOpen(filePath, name, yt_url, jobId, workDir)`, `mYtsOpen(filePath, name, jobId, workDir)`, `mIgOpen(filePath, name, ncsAttr, jobId)`.

After successful upload, `loadResults()` is called to refresh the list (yt_url / ig_url badges).

## Modal 1: YT Main Upload (`m-yt-modal`)

Opened for highlight files. Fields:

- **Filename** display (`m-yt-filename`)
- **Title** input (`m-yt-title`) — pre-filled from `GET /api/job-config` `yt_title` or project name derived from `workDir` path
- **Description** textarea (`m-yt-desc`) — pre-filled from `yt_desc` or default footer
- **Notes** textarea (`m-yt-notes`) — optional notes for Claude generation; saved via `POST /api/jobs/{id}/save-yt-meta`
- **✦ Generate** button — calls `POST /api/jobs/{id}/generate-yt-meta {project_name, footer, notes}` → fills description
- **✦ AI Chapters** button — calls `POST /api/jobs/{id}/generate-metadata {}` → prepends chapter block to description
- **Privacy** radio: `public` / `unlisted` (default) / `private`
- **Playlist** select (`m-yt-playlist`) — loaded from `GET /api/youtube/playlists`; "New playlist" toggle → text input (`m-yt-new-playlist`)
- **Existing URL** input (`m-yt-existing-url`) + Save button (`POST /api/jobs/{id}/youtube-url`) + Clear button
- **Status** display (`m-yt-status`)
- **▲ Upload** button — calls `POST /api/youtube/upload`, polls `GET /api/youtube/upload/{id}` every 2s

Title/desc/notes auto-saved on blur via `POST /api/jobs/{id}/save-yt-meta`.

## Modal 2: YT Shorts Upload (`m-yts-modal`)

Opened for short files. Fields:

- **Filename** display (`m-yts-filename`)
- **Title** input (`m-yts-title`) — pre-filled from config `title` first line or project name
- **Description** textarea (`m-yts-desc`) — auto-filled: `Full video: <yt_url>\n\n<shorts_footer>`
- **Full video selector** row (`m-yts-fullvideo-row`) — hidden if ≤1 main video has `yt_url`; dropdown of main videos with yt_url; changing selection updates description link
- **✦ Generate** button — same endpoint as YT main
- **Privacy** radio: `public` (default) / `unlisted` / `private`
- **Playlist** select (`m-yts-playlist`) + new playlist toggle (`m-yts-new-playlist`)
- **Status / warning** (`m-yts-status`) — `⚠ Main video not yet published` + Upload button disabled when no main `yt_url` found
- **▲ Upload** button — `POST /api/youtube/upload`, same poll

## Modal 3: IG Reel Upload (`m-ig-modal`)

Opened for short files with `is_ncs=true`. Fields:

- **Filename** display (`m-ig-filename`)
- **Token expiry warning** (`m-ig-token-warn`) — shown if `days_until_expiry <= 5` from `GET /api/ig/status`
- **Cooldown warning** (`m-ig-cooldown-warn`) — shown if `status.ready === false`; Upload button disabled
- **Caption** textarea (`m-ig-caption`) — pre-filled: `Music: {ncs_attr} (NCS Release)\n\n{hashtags}\n{repo_url}` or hashtags-only
- **Status** display (`m-ig-status`)
- **▲ Upload** button — `POST /api/ig/upload {file_path, caption}`, polls `GET /api/ig/upload/{id}` every 5s

## Backend

No new endpoints needed. All uploads use existing routes:
- `GET /api/youtube/status`
- `GET /api/youtube/playlists`
- `POST /api/youtube/upload`
- `GET /api/youtube/upload/{id}`
- `POST /api/jobs/{id}/save-yt-meta`
- `POST /api/jobs/{id}/generate-yt-meta`
- `POST /api/jobs/{id}/generate-metadata`
- `POST /api/jobs/{id}/youtube-url`
- `GET /api/ig/status`
- `POST /api/ig/upload`
- `GET /api/ig/upload/{id}`

## CSS

Upload modals reuse existing `.m-modal` / overlay pattern from `modern.css`. New utility class `.m-upload-row` for label+input rows within modals.

## Error Handling

- YouTube not authenticated: alert, modal doesn't open
- Instagram not configured: alert, modal doesn't open
- Upload start failure: status shows `⚠ <error>`, button re-enabled
- Upload poll failure: ignored (next poll retries)
- Generate/chapters failure: status shows `⚠ <error>`, button re-enabled
