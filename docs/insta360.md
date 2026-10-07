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

## Forward exclusion & mount calibration

The 360 camera's value is the angles the helmet/handlebar cams can't cover,
so window selection **hard-excludes a 180° wedge centered on "forward"**
(right/left/back only — see `FORWARD_EXCLUDE_HALF_DEG` in
`src/insta360_scan.py`). Raw yaw=0 is the camera **body's** own reference
axis (the dual-fisheye seam), not the direction of travel — it only lines up
with "forward" if the camera happens to be mounted with that seam pointed
along the bike's heading. A different mount angle needs a one-time
calibration offset: `[insta360] forward_yaw_deg` in `config.ini` (global or
per-project override, same as `auto_scan`).

**To find the right value for a given mount:** dump one labeled frame per
yaw direction from a representative LRV file and eyeball which one shows the
road/handlebars ahead (same view the helmet cam already has):

```bash
ffmpeg -hide_banner -loglevel error -y -ss 30 -i path/to/clip.lrv \
  -filter_complex "
    [0:v]v360=input=dfisheye:output=e:ih_fov=190:iv_fov=190:roll=90,split=8[e0][e1][e2][e3][e4][e5][e6][e7];
    [e0]v360=e:flat:h_fov=100:v_fov=62:yaw=0:w=960:h=540[v0];
    [e1]v360=e:flat:h_fov=100:v_fov=62:yaw=45:w=960:h=540[v1];
    [e2]v360=e:flat:h_fov=100:v_fov=62:yaw=90:w=960:h=540[v2];
    [e3]v360=e:flat:h_fov=100:v_fov=62:yaw=135:w=960:h=540[v3];
    [e4]v360=e:flat:h_fov=100:v_fov=62:yaw=180:w=960:h=540[v4];
    [e5]v360=e:flat:h_fov=100:v_fov=62:yaw=-135:w=960:h=540[v5];
    [e6]v360=e:flat:h_fov=100:v_fov=62:yaw=-90:w=960:h=540[v6];
    [e7]v360=e:flat:h_fov=100:v_fov=62:yaw=-45:w=960:h=540[v7]" \
  -map "[v0]" y000.jpg -map "[v1]" y045.jpg -map "[v2]" y090.jpg \
  -map "[v3]" y135.jpg -map "[v4]" y180.jpg -map "[v5]" y225.jpg \
  -map "[v6]" y270.jpg -map "[v7]" y315.jpg
```

Whichever `yNNN.jpg` shows dead-ahead is your `forward_yaw_deg` (the `roll=90`
matches `LRV_ROLL` — only change it if your rig isn't the usual sideways bike
mount). This is a **one-time calibration per physical mount** — it only
needs redoing if the camera is remounted at a different rotation, not per
ride.

## Licensing note

The MediaSDK EULA allows using the SDK only to build software for Insta360
products and forbids redistributing it standalone or combining it with
copyleft-licensed code in one program. ai-autoedit (AGPL + Commons Clause)
therefore calls the SDK strictly as an **external user-supplied binary** via
subprocess — do not commit any part of the SDK to the repository.
