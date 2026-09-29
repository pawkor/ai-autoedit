# Insta360 360° support / Obsługa nagrań 360

AI-reframing of Insta360 X-series 360° recordings: CLIP scans the footage,
picks the best moments **and the best view direction**, the Insta360 MediaSDK
stitches only those fragments (FlowState stabilization + Direction Lock), and
flat 16:9 clips land in the project pool as camera `"360"` — no manual
keyframing.

## Requirements

- Insta360 **MediaSDK** for Linux — a free but licensed SDK. Its EULA forbids
  redistribution, so **nothing from the SDK ships with this repo**; you apply
  for it yourself:
  1. Apply at <https://www.insta360.com/sdk/apply> (takes ~3 business days).
  2. Download the Linux package from your account and unzip it anywhere
     (e.g. `insta360-sdk/` in the repo — that directory is gitignored).
  3. Run `./scripts/setup_insta360_sdk.sh path/to/MediaSDK-*-linux-amd64.deb`
     — it unpacks the deb without root and prints the config snippet.
- NVIDIA GPU (the stitcher and the CLIP scan both use it).

## Configuration

```ini
[paths]
insta360_mediasdk = insta360-sdk/opt-extract/opt/MediaSDK-3.1.5-linux/bin/MediaSDKTest
```

Relative paths resolve against the repo root (which is `/app` inside the
container — the compose file bind-mounts `./insta360-sdk` read-only, and the
image ships the SDK's runtime libraries). Rebuild the image + `docker compose
up -d` after enabling.

When the binary is missing, every 360 control in the UI stays hidden; the
Settings modal always shows the SDK status with a hint.

## Usage

Put the 360 recordings in a `360/` subdir of the project day, as the camera
writes them (dual-file pairs + preview):

```
~/moto/2025/04-Grecja/23/360/
  VID_20250423_082930_00_001.insv   # front lens, 2880x2880
  VID_20250423_082930_10_001.insv   # rear lens
  LRV_20250423_082930_11_001.insv   # dual-fisheye preview (used for the scan)
```

Then either click **⚈ Scan 360** in the Project modal (visible when the SDK
is configured and pairs are detected; output streams to the Log modal), or
run it manually:

```
python3 src/insta360_scan.py <work_dir> \
    [--interval 2] [--yaws 8] [--clip-dur 6] [--min-gap 30] [--per-file 8]
```

## How it works

1. **Scan** — the low-res LRV is projected to equirect (ffmpeg `v360`) and
   8 yaw views are CLIP-scored every 2 s with your `[clip_prompts]` plus
   anti-helmet negatives. Cached per file (`_autoframe/insta360/raw/`).
2. **Select** — top windows per file (score peaks, min gap), each with its
   winning yaw.
3. **Stitch** — one `MediaSDKTest` call per pair exports the frames of all
   selected windows (the SDK seeks, so cost scales with window count, not
   file length) at 3840×1920 with FlowState + Direction Lock. The horizon
   stays level regardless of how the camera was mounted — orientation comes
   from the gyro.
4. **Assemble** — each window becomes a 1920×1080 NVENC clip with audio from
   the source `.insv` and `creation_time` metadata, so multicam sync and the
   chronological arc work exactly like for any other camera. By default an
   **object-lock tracker** (reproject–track–recenter, CSRT/MIL) follows the
   scan-chosen subject through the window — a hill or landmark stays in
   frame while the bike moves — with an angular-velocity clamp (40°/s) and a
   smoothed trajectory; when tracking is unreliable the clip falls back to a
   fixed yaw. Disable with `--no-track`.
5. **Merge** — `scene_scores_allcam.csv`, `camera_sources.csv`
   (camera=`360`), `duration_cache.json` and a pool thumbnail per clip.

Re-runs are incremental: scan results and assembled window sets are cached;
changing prompts or parameters invalidates exactly the affected phase.

## Licensing note

The MediaSDK EULA allows using the SDK only to build software for Insta360
products and forbids redistributing it standalone or combining it with
copyleft-licensed code in one program. ai-autoedit (AGPL + Commons Clause)
therefore calls the SDK strictly as an **external user-supplied binary** via
subprocess — do not commit any part of the SDK to the repository.
