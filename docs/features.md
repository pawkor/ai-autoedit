# AI-autoedit — Feature Roadmap

Status legend: ✅ Done · 🔄 In progress · 💡 Planned · ❌ Not planned

## Core pipeline

| Feature | Status | Notes |
|---|---|---|
| PySceneDetect scene splitting | ✅ | configurable threshold + min_scene_len |
| CLIP-first mode | ✅ | skips scenedetect; frame scanning + CLIP peak extraction; `-clip-NNN` clips |
| CLIP scoring (ViT-L-14, GPU) | ✅ | positive/negative prompts, neg_weight; multi-frame (_f0/_f1/_f2) |
| Score all cameras (allcam CSV) | ✅ | `scene_scores_allcam.csv`; auto-enabled with CLIP-first |
| LAION Aesthetic Predictor | ✅ | MLP on ViT-L-14 embeddings; `aesthetic_score` column in CSV |
| Threshold-based scene selection | ✅ | per-file cap, camera balance; Traditional mode only; instant client-side binary search |
| GPS speed/turn scoring | ✅ | exiftool GPS extraction, Haversine speed+bearing, `gps_weight` blends into CLIP score |
| Music-driven render | ✅ | beat/segment sync (cuts land on downbeats via `_bar_ceil()` rounding to nearest bar), source diversity, per-shot camera alternation, chronological arc (morning→evening), full-res intro, configurable beats-per-tier (▼▲), camera cut pattern, respects manual gallery exclusions, hard-excludes clips with `final_score < 0` |
| Multi-cam sync (timestamp + clock offset) | ✅ | MP4 creation_time fallback |
| Intro / outro overlay | ✅ | full-res frame from autocut clip (not thumbnail) |
| Music mix with BPM/energy matching | ✅ | target duration drives track selection |
| Output file naming (YYYY-MM-Place-DD.mp4) | ✅ | _output_name() |
| Tiered CLIP thresholds per source file | ✅ | [tier_overrides] in config.ini |

## Web UI

| Feature | Status | Notes |
|---|---|---|
| Settings tab (all pipeline params) | ✅ | |
| Music-driven primary UI (Traditional mode hidden) | ✅ | ♪ Music-driven = main button; threshold/per_file behind "Traditional mode" toggle |
| ⚠ Re-analyze badge | ✅ | warns when CLIP-first setting doesn't match existing scenes |
| Proxy media (480p/20fps CFR for fast scene detection) | ✅ | atomic `.tmp` → `.mp4`, auto-started on project open |
| Select scenes tab with score cards + duration badge | ✅ | |
| Select scenes manual overrides (include/exclude) | ✅ | persist across binary search |
| Target duration binary search | ✅ | + fill logic when target unreachable |
| Select scenes hover → video clip preview | ✅ | 500ms delay, reuses file-tip overlay |
| Music tab with BPM/energy sort | ✅ | auto-re-sorts by estimated duration on every tab switch (calculateGalleryStats refresh) |
| Music → Target dur. sort button | ✅ | picks closest track to target |
| Music checkbox selection persistence | ✅ | saved to job params on toggle |
| Render tab with duration estimate | ✅ | dual-cam aware |
| Log tab with live streaming | ✅ | |
| Log filter dropdown (Steps / Info / All) | ✅ | CSS class-based, persists in session |
| Render mini progress in tab row | ✅ | step name + bar + % + ETA, visible from any tab during active render, auto-hides on completion |
| Results tab with file listing | ✅ | |
| File browser (dir picker for new project) | ✅ | |
| Video preview on hover in file browser | ✅ | |
| Themes: dark / light / gruvbox / nord / solarized | ✅ | CSS variable-based |
| Language switcher (EN / PL) | ✅ | i18n via TRANS map |
| UI preferences server-side (config.ini) | ✅ | cross-device via webapp/config.ini |
| Job sort order (a..z / z..a) | ✅ | alphabetical by work_dir path, persisted server-side |
| YouTube Shorts generation (make_shorts.py) | ✅ | fully random alternating cam-a/cam-b, 1.5s shots, center crop to 9:16, `*-short_vNN.mp4` |
| Shorts per-camera crop X offset | ✅ | `[shorts] crop_x_offsets` in config.ini, e.g. `back=-250` shifts back-cam crop left |
| Shorts visible in Results tab | ✅ | `*short*` filename detected, separate **▲ YT Shorts** button |
| ▶ Render Short button in Render tab | ✅ | streams log + pulsing progress bar, separate from Render Highlight |

## YouTube integration

| Feature | Status | Notes |
|---|---|---|
| OAuth2 connect + token refresh | ✅ | |
| Upload with progress (SSE) | ✅ | |
| Playlist select / create | ✅ | |
| Existing URL linking | ✅ | |
| Auto-generate title/description via Claude | ✅ | ↺ button in upload modal |
| Hashtag / footer line preservation on regen | ✅ | detects trailing hashtag/URL lines |
| Save title/desc to config.ini | ✅ | persists across modal opens |
| YouTube Shorts upload (separate flow) | ✅ | dedicated modal: title (from project config.ini), description + ↺ Claude, privacy, playlist |
| Block Shorts upload if full video not yet published | ✅ | require known YouTube URL of full video before allowing Shorts upload |
| Playlist select / create — Shorts modal | ✅ | same UX as regular upload modal |
| Upload description without title (both modals) | ✅ | description starts with hashtags/footer, not repeated title |
| Instagram Reels upload | ✅ | Creator/Business account, Graph API v20.0, NCS attribution auto-fill, token auto-refresh, resumable upload |
| Scheduled / delayed upload | 💡 | useful for time-zone optimised posting |
| Chapter markers in description | 💡 | derive from scene timestamps |

## S3 / cloud

| Feature | Status | Notes |
|---|---|---|
| S3 source listing | ✅ | |
| S3 upload with SSE progress | ✅ | |
| S3 download with SSE progress | ✅ | |
| Fetch sources from S3 | ✅ | |
| Purge local copies after S3 upload | ✅ | |
| Multi-bucket support | 💡 | currently one bucket per .env |

## Music library

| Feature | Status | Notes |
|---|---|---|
| BPM / energy / loudness analysis | ✅ | librosa |
| Last.fm genre lookup | ✅ | |
| yt-dlp YouTube download | ✅ | saves `.yt.json` sidecar with license + source URL |
| YouTube license badge (CC / ©) | ✅ | shown per track in Music tab for YT-sourced files |
| Delete track from library | ✅ | removes file, index.json entry, shorts_used.json entry |
| ACRCloud Content ID check (manual) | ✅ | ⚙ button per track, fingerprint via ACRCloud API |
| ACRCloud auto pre-check before render | ✅ | skips claimed tracks, retries until free candidate found |
| Global used-tracks index | ✅ | `webapp/jobs/used_tracks.json`; red ● per track + ⚠ footer warning when previously used; tooltip shows project/render/date/YT; auto-recorded after every render (music-driven and traditional) |
| Music offset auto-fill | ✅ | offset field pre-filled from `intro_outro.duration` on project load |
| CLIP prompts editor modal | ✅ | 80×80% modal opened from inspector "AI / CLIP ↗" button; About/Generate/Save + two-column POSITIVE/NEGATIVE editor |
| Waveform visualisation | 💡 | small inline waveform on music cards |
| Auto-trim silence at start/end of tracks | 💡 | |

## Quality / reliability

| Feature | Status | Notes |
|---|---|---|
| Per-job config.ini overrides | ✅ | local config wins over global |
| Job state persistence (JSON) | ✅ | survives server restart |
| Concurrent job limit | ✅ | max_concurrent_jobs in webapp/config.ini — changes take effect immediately, no restart needed |
| Parallel Shorts rendering | ✅ | `shorts_semaphore` (limit 4) independent of main pipeline semaphore |
| GPU batch size tuning | ✅ | batch_size, num_workers |
| Graceful stop mid-render | 💡 | currently kills process, no cleanup step |
| Re-run individual pipeline steps | 💡 | skip already-done steps |
| Duplicate frame detection | 💡 | skip near-identical scenes (cosine sim) |

## Distribution

| Feature | Status | Notes |
|---|---|---|
| Docker Compose (current) | ✅ | Linux, requires Docker + NVIDIA Container Toolkit |
| Docker Desktop guide (Windows / Mac) | 💡 | CPU-only; Docker Desktop makes it accessible to non-technical users |
| Standalone installer — Linux AppImage | 💡 | bundles Python + deps; CUDA optional (detect at runtime) |
| Standalone installer — macOS .pkg / .app | 💡 | CPU-only (no CUDA on Apple Silicon/Intel); homebrew for ffmpeg |
| Standalone installer — Windows .exe | 💡 | CPU-only or CUDA if NVIDIA driver present; NSIS/Inno Setup |
| install.sh one-liner (Linux / Mac) | 💡 | creates venv, fetches ffmpeg, launches webapp |

## Potential community requests (researched)

| Feature | Demand | Status | Notes |
|---|---|---|---|
| Mobile / tablet responsive UI | Medium | ✅ | sidebar drawer, hamburger, 2-row tabs, dvh, inspector slide-in (⚙), touch targets 44px, gallery auto-fill grid |
| Dark/light auto-follow OS preference | Low | 💡 | prefers-color-scheme media query |
| Export to Premiere / DaVinci project | Low | 💡 | EDL or XML with scene timecodes |
| Face-detection blur for bystanders | Low | 💡 | privacy feature, GDPR relevant |
| Subtitle / caption generation (Whisper) | Medium | 💡 | for spoken commentary tracks |
| Hardware-accelerated encode (NVENC) | High | ✅ | h264_nvenc used in pipeline |
| Multiple output resolutions (1080p + 4K) | Low | 💡 | |
| Automatic colour grading LUT | Low | 💡 | apply a LUT in ffmpeg filter chain |
