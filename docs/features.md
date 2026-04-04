# AI-autoedit — Feature Roadmap

Status legend: ✅ Done · 🔄 In progress · 💡 Planned · ❌ Not planned

## Core pipeline

| Feature | Status | Notes |
|---|---|---|
| PySceneDetect scene splitting | ✅ | configurable threshold + min_scene_len |
| CLIP scoring (ViT-L-14, GPU) | ✅ | positive/negative prompts, neg_weight |
| Threshold-based scene selection | ✅ | per-file cap, camera balance |
| Multi-cam sync (timestamp + clock offset) | ✅ | MP4 creation_time fallback |
| Intro / outro overlay | ✅ | configurable skip via no_intro |
| Music mix with BPM/energy matching | ✅ | target duration drives track selection |
| Output file naming (YYYY-MM-Place-DD.mp4) | ✅ | _output_name() |
| Tiered CLIP thresholds per source file | ✅ | [tier_overrides] in config.ini |

## Web UI

| Feature | Status | Notes |
|---|---|---|
| Settings tab (all pipeline params) | ✅ | |
| Proxy media (480p/20fps CFR for fast scene detection) | ✅ | atomic `.tmp` → `.mp4`, auto-started on project open |
| Select scenes tab with score cards + duration badge | ✅ | |
| Select scenes manual overrides (include/exclude) | ✅ | persist across binary search |
| Target duration binary search | ✅ | + fill logic when target unreachable |
| Select scenes hover → video clip preview | ✅ | 500ms delay, reuses file-tip overlay |
| Music tab with BPM/energy sort | ✅ | |
| Music → Target dur. sort button | ✅ | picks closest track to target |
| Music checkbox selection persistence | ✅ | saved to job params on toggle |
| Render tab with duration estimate | ✅ | dual-cam aware |
| Log tab with live streaming | ✅ | |
| Log filter dropdown (Steps / Info / All) | ✅ | CSS class-based, persists in session |
| Results tab with file listing | ✅ | |
| File browser (dir picker for new project) | ✅ | |
| Video preview on hover in file browser | ✅ | |
| Themes: dark / light / gruvbox / nord / solarized | ✅ | CSS variable-based |
| Language switcher (EN / PL) | ✅ | i18n via TRANS map |
| UI preferences server-side (config.ini) | ✅ | cross-device via webapp/config.ini |
| Job sort order (newest/oldest) | ✅ | persisted server-side |
| YouTube Shorts generation (make_shorts.py) | ✅ | top-scored scenes, 1.5s shots, center crop to 9:16, `*-short_vNN.mp4` |
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
| YouTube Shorts upload (separate flow) | ✅ | dedicated modal: title (from project config.ini), description + ↺ Claude, privacy |
| Block Shorts upload if full video not yet published | ✅ | require known YouTube URL of full video before allowing Shorts upload |
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
| yt-dlp YouTube download | ✅ | |
| Waveform visualisation | 💡 | small inline waveform on music cards |
| Auto-trim silence at start/end of tracks | 💡 | |

## Quality / reliability

| Feature | Status | Notes |
|---|---|---|
| Per-job config.ini overrides | ✅ | local config wins over global |
| Job state persistence (JSON) | ✅ | survives server restart |
| Concurrent job limit | ✅ | max_concurrent_jobs in webapp/config.ini |
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
| Mobile / tablet responsive UI | Medium | 💡 | layout works but not optimised |
| Dark/light auto-follow OS preference | Low | 💡 | prefers-color-scheme media query |
| Export to Premiere / DaVinci project | Low | 💡 | EDL or XML with scene timecodes |
| Face-detection blur for bystanders | Low | 💡 | privacy feature, GDPR relevant |
| Subtitle / caption generation (Whisper) | Medium | 💡 | for spoken commentary tracks |
| Hardware-accelerated encode (NVENC) | High | 💡 | ffmpeg -c:v h264_nvenc |
| Multiple output resolutions (1080p + 4K) | Low | 💡 | |
| Automatic colour grading LUT | Low | 💡 | apply a LUT in ffmpeg filter chain |
