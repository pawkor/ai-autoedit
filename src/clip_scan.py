#!/usr/bin/env python3
"""
clip_scan.py — CLIP-first scene extraction (Option B)

Instead of scenedetect → split → CLIP, this script:
  1. Extracts frames every SAMPLE_INTERVAL seconds from each source file
     (uses 480p proxy if available for fast decode)
  2. Scores all frames with CLIP (batched, GPU)
  3. Smooths the score timeline and finds local maxima (peaks)
  4. Extracts clips ± CLIP_DUR/2 around each peak from the SOURCE file
  5. Writes one representative frame per clip to frames/ (for gallery)
  6. Writes scene_scores_allcam.csv (same format as clip_score.py)

Output naming: {source_stem}-clip-{N:03d}.mp4 in autocut/
               {source_stem}-clip-{N:03d}_f0.jpg in frames/

Environment variables:
  WORK_DIR                 source files root
  AUTO_DIR                 _autoframe directory
  CAMERAS                  comma-separated camera subfolders (empty = flat layout)
  FFMPEG                   path to ffmpeg
  FFPROBE                  path to ffprobe
  OUTPUT_CSV               main-cam scores CSV path
  OUTPUT_CSV_ALLCAM        all-cam scores CSV path
  CAM_SOURCES              camera_sources.csv path
  AUDIO_CAM                main/audio camera subfolder name
  CLIP_BATCH_SIZE          (default 64)
  CLIP_NUM_WORKERS         (default 4)
  CLIP_SCAN_INTERVAL_SEC   seconds between sampled frames (default 3)
  CLIP_SCAN_CLIP_DUR_SEC   extracted clip duration in seconds (default 8)
  CLIP_SCAN_MIN_GAP_SEC    minimum seconds between clips (default 30)
  CLIP_SCAN_SMOOTH         rolling-average window in frames (default 3)
  CLIP_SCAN_THRESHOLD      minimum score to consider as a peak (default 0.0)
"""
import configparser
import csv
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="QuickGELU mismatch")

import numpy as np
import torch
import open_clip
from PIL import Image
from torch.utils.data import Dataset, DataLoader

# ── Config ────────────────────────────────────────────────────────────────────
_script_dir = Path(__file__).resolve().parent
_cfg = configparser.ConfigParser()
_cfg.read([_script_dir.parent / "config.ini", Path(os.environ.get("WORK_DIR", ".")) / "config.ini"])

def _e(key, default=""): return os.environ.get(key, default)

WORK_DIR     = Path(_e("WORK_DIR", "."))
AUTO_DIR     = Path(_e("AUTO_DIR", str(WORK_DIR / "_autoframe")))
CAMERAS      = [c.strip() for c in _e("CAMERAS").split(",") if c.strip()]
FFMPEG       = _e("FFMPEG", "ffmpeg")
FFPROBE      = _e("FFPROBE", "ffprobe")
OUTPUT_CSV          = _e("OUTPUT_CSV",       str(AUTO_DIR / "scene_scores.csv"))
OUTPUT_CSV_ALLCAM   = _e("OUTPUT_CSV_ALLCAM", str(AUTO_DIR / "scene_scores_allcam.csv"))
CAM_SOURCES  = _e("CAM_SOURCES", str(AUTO_DIR / "camera_sources.csv"))
AUDIO_CAM    = _e("AUDIO_CAM", CAMERAS[0] if CAMERAS else "")

BATCH_SIZE    = int(_e("CLIP_BATCH_SIZE",  _cfg.get("clip_scoring", "batch_size",  fallback="64")))
NUM_WORKERS   = int(_e("CLIP_NUM_WORKERS", _cfg.get("clip_scoring", "num_workers", fallback=str(min(4, os.cpu_count() or 1)))))
INTERVAL_SEC  = float(_e("CLIP_SCAN_INTERVAL_SEC", "3"))
CLIP_DUR_SEC  = float(_e("CLIP_SCAN_CLIP_DUR_SEC",  "8"))
MIN_GAP_SEC   = float(_e("CLIP_SCAN_MIN_GAP_SEC",  "30"))
SMOOTH_WIN    = int(_e("CLIP_SCAN_SMOOTH", "3"))
SCORE_FLOOR   = float(_e("CLIP_SCAN_THRESHOLD", "0.0"))
NEG_WEIGHT    = _cfg.getfloat("clip_scoring", "neg_weight", fallback=0.5)

def _parse_prompts(raw):
    return [l.strip() for l in raw.strip().splitlines() if l.strip()]

POSITIVE_PROMPTS = _parse_prompts(_cfg.get("clip_prompts", "positive", fallback=""))
NEGATIVE_PROMPTS = _parse_prompts(_cfg.get("clip_prompts", "negative", fallback=""))

if not POSITIVE_PROMPTS or not NEGATIVE_PROMPTS:
    print("ERROR: CLIP prompts not configured. Run Settings → Describe this ride.", file=sys.stderr)
    sys.exit(1)

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".ts"}


# ── CLIP model ────────────────────────────────────────────────────────────────
if torch.cuda.is_available():    DEVICE = "cuda"
elif torch.backends.mps.is_available(): DEVICE = "mps"
else:                            DEVICE = "cpu"

print(f"Device: {DEVICE}")
if DEVICE == "cuda":
    print(f"GPU: {torch.cuda.get_device_name(0)}")
print(f"Interval: {INTERVAL_SEC}s  Clip dur: {CLIP_DUR_SEC}s  Min gap: {MIN_GAP_SEC}s")

model, _, preprocess = open_clip.create_model_and_transforms('ViT-L-14', pretrained='openai')
tokenizer = open_clip.get_tokenizer('ViT-L-14')
model = model.to(DEVICE).eval()

with torch.no_grad():
    pos_tok = tokenizer(POSITIVE_PROMPTS).to(DEVICE)
    neg_tok = tokenizer(NEGATIVE_PROMPTS).to(DEVICE)
    pos_feat = model.encode_text(pos_tok); pos_feat /= pos_feat.norm(dim=-1, keepdim=True)
    neg_feat = model.encode_text(neg_tok); neg_feat /= neg_feat.norm(dim=-1, keepdim=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
class _FrameDS(Dataset):
    def __init__(self, paths, transform):
        self.paths = paths; self.transform = transform
    def __len__(self): return len(self.paths)
    def __getitem__(self, idx):
        p = self.paths[idx]
        try:   img = self.transform(Image.open(p).convert('RGB')); ok = True
        except: img = torch.zeros(3, 224, 224); ok = False
        return img, str(p), ok


def _score_frames(frame_paths: list[Path]) -> list[float]:
    """Score a list of frame paths. Returns float list same length."""
    ds = _FrameDS(frame_paths, preprocess)
    loader = DataLoader(ds, batch_size=BATCH_SIZE, num_workers=NUM_WORKERS,
                        pin_memory=(DEVICE == "cuda"),
                        prefetch_factor=2 if NUM_WORKERS > 0 else None)
    scores_map: dict[str, float] = {}
    for imgs, paths, oks in loader:
        mask = oks.bool()
        if not mask.any(): continue
        t = imgs[mask].to(DEVICE, non_blocking=True)
        vp = [p for p, ok in zip(paths, oks.tolist()) if ok]
        with torch.no_grad(), torch.amp.autocast(
            device_type=DEVICE if DEVICE != "mps" else "cpu",
            enabled=(DEVICE == "cuda"),
        ):
            f = model.encode_image(t); f /= f.norm(dim=-1, keepdim=True)
            pf = pos_feat.to(f.dtype); nf = neg_feat.to(f.dtype)
            s = (f @ pf.T).mean(1) - (f @ nf.T).mean(1) * NEG_WEIGHT
        for path, sc in zip(vp, s.float().cpu().tolist()):
            scores_map[path] = sc
    return [scores_map.get(str(p), 0.0) for p in frame_paths]


def _smooth(arr: list[float], window: int) -> list[float]:
    if window <= 1: return arr
    out = []
    h = window // 2
    for i in range(len(arr)):
        sl = arr[max(0, i-h): i+h+1]
        out.append(sum(sl) / len(sl))
    return out


def _find_peaks(scores: list[float], min_gap_frames: int, threshold: float) -> list[int]:
    """Return indices of local maxima above threshold with min-gap enforcement."""
    n = len(scores)
    peaks: list[int] = []
    for i in range(1, n - 1):
        if scores[i] < threshold: continue
        if scores[i] < scores[i-1] or scores[i] < scores[i+1]: continue
        if peaks and (i - peaks[-1]) < min_gap_frames:
            if scores[i] > scores[peaks[-1]]: peaks[-1] = i
        else:
            peaks.append(i)
    return peaks


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try: return float(r.stdout.strip())
    except: return 0.0


def _proxy_path(sf: Path) -> Path | None:
    """Return proxy path if it exists."""
    proxy_dir = AUTO_DIR / "proxy"
    for cam in CAMERAS:
        try:
            rel = sf.relative_to(WORK_DIR / cam)
            p = proxy_dir / cam / rel.with_suffix(".mp4")
            if p.exists(): return p
        except ValueError:
            continue
    p = proxy_dir / sf.with_suffix(".mp4").name
    return p if p.exists() else None


def _extract_frames_to_dir(src: Path, out_dir: Path, interval: float) -> list[Path]:
    """Extract 1 frame per interval sec → out_dir/000001.jpg, 000002.jpg, …"""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Use proxy for faster decode if available
    scan_src = _proxy_path(src) or src
    cmd = [
        FFMPEG, "-y", "-i", str(scan_src),
        "-vf", f"fps=1/{interval},scale=trunc(iw/4)*2:trunc(ih/4)*2",
        "-q:v", "5", str(out_dir / "%06d.jpg"),
    ]
    subprocess.run(cmd, capture_output=True)
    return sorted(out_dir.glob("*.jpg"))


def _extract_clip(src: Path, start: float, duration: float, out: Path):
    """Extract clip from source with fast seek, re-encode to ensure clean boundaries."""
    start = max(0.0, start)
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{duration:.3f}",
        "-c", "copy",
        str(out),
    ]
    subprocess.run(cmd, capture_output=True)


# ── Source files ──────────────────────────────────────────────────────────────
if CAMERAS:
    source_files = sorted(
        f for cam in CAMERAS
        for f in (WORK_DIR / cam).glob("*.mp4")
        if f.suffix.lower() in VIDEO_EXT and not f.name.lower().startswith("highlight")
    )
else:
    source_files = sorted(
        f for f in WORK_DIR.glob("*.mp4")
        if f.suffix.lower() in VIDEO_EXT and not f.name.lower().startswith("highlight")
    )

print(f"Source files: {len(source_files)}")

# Write camera_sources.csv
if CAMERAS:
    with open(CAM_SOURCES, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["source", "camera"])
        for sf in source_files:
            cam = next((c for c in CAMERAS if f"/{c}/" in str(sf)), CAMERAS[-1])
            w.writerow([sf.stem, cam])

autocut_dir = AUTO_DIR / "autocut"
frames_dir  = AUTO_DIR / "frames"
autocut_dir.mkdir(parents=True, exist_ok=True)
frames_dir.mkdir(parents=True, exist_ok=True)

min_gap_frames = max(1, int(MIN_GAP_SEC / INTERVAL_SEC))
all_clips: list[dict] = []   # {scene, score, pos_score, neg_score, emb, is_main}

# ── Per-file scan → score → peaks → extract ──────────────────────────────────
for sf_idx, sf in enumerate(source_files, 1):
    cam = next((c for c in CAMERAS if f"/{c}/" in str(sf)), CAMERAS[0] if CAMERAS else "")
    is_main = (cam == AUDIO_CAM or not CAMERAS)
    dur = _probe_duration(sf)
    if dur < INTERVAL_SEC:
        print(f"  [{sf_idx}/{len(source_files)}] {sf.name}: too short ({dur:.1f}s), skipped")
        continue

    n_frames_expected = max(1, int(dur / INTERVAL_SEC))
    print(f"  [{sf_idx}/{len(source_files)}] {sf.name} ({dur:.0f}s, ~{n_frames_expected} frames)...")

    with tempfile.TemporaryDirectory(prefix="clip_scan_") as tmp:
        tmp_dir = Path(tmp)
        frame_paths = _extract_frames_to_dir(sf, tmp_dir, INTERVAL_SEC)
        if not frame_paths:
            print(f"    WARNING: no frames extracted — skipping")
            continue

        # Score
        t0 = time.time()
        raw_scores = _score_frames(frame_paths)
        elapsed = time.time() - t0
        print(f"    Scored {len(raw_scores)} frames in {elapsed:.1f}s")

        # Smooth + find peaks
        smoothed = _smooth(raw_scores, SMOOTH_WIN)
        peak_idxs = _find_peaks(smoothed, min_gap_frames, SCORE_FLOOR)
        if not peak_idxs:
            print(f"    No peaks found")
            continue

        print(f"    Peaks: {len(peak_idxs)}  (scores: {[round(smoothed[i],3) for i in peak_idxs[:8]]})")

        # Extract clips + keep peak frame
        cam_clip_n = sum(1 for c in all_clips if c["scene"].startswith(sf.stem + "-clip-"))
        for peak_i in peak_idxs:
            peak_ts = peak_i * INTERVAL_SEC  # approximate timestamp
            clip_start = max(0.0, peak_ts - CLIP_DUR_SEC / 2)
            clip_n = cam_clip_n + 1
            cam_clip_n += 1
            scene_name = f"{sf.stem}-clip-{clip_n:03d}"

            # Extract clip
            clip_out = autocut_dir / f"{scene_name}.mp4"
            clip_newly_created = not clip_out.exists()
            if clip_newly_created:
                _extract_clip(sf, clip_start, CLIP_DUR_SEC, clip_out)

            # Copy peak frame → frames/ for gallery
            # Always overwrite when clip was just created (avoids stale frame from deleted+recreated clip)
            peak_frame_src = frame_paths[peak_i]
            peak_frame_dst = frames_dir / f"{scene_name}_f0.jpg"
            if clip_newly_created or not peak_frame_dst.exists():
                import shutil
                peak_frame_dst.unlink(missing_ok=True)
                shutil.copy2(peak_frame_src, peak_frame_dst)

            all_clips.append({
                "scene":     scene_name,
                "score":     raw_scores[peak_i],
                "pos_score": 0.0,   # re-scored below if needed
                "neg_score": 0.0,
                "is_main":   is_main,
            })


# ── Re-score peak frames for accurate pos/neg columns ─────────────────────────
if all_clips:
    peak_frame_paths = [frames_dir / f"{c['scene']}_f0.jpg" for c in all_clips]
    print(f"\nRe-scoring {len(peak_frame_paths)} peak frames for CSV...")

    # We need pos/neg separately — run batched inference again on saved frames
    ds = _FrameDS(peak_frame_paths, preprocess)
    loader = DataLoader(ds, batch_size=BATCH_SIZE, num_workers=NUM_WORKERS,
                        pin_memory=(DEVICE == "cuda"),
                        prefetch_factor=2 if NUM_WORKERS > 0 else None)
    path_to_scores: dict[str, tuple] = {}
    path_to_emb:    dict[str, np.ndarray] = {}

    for imgs, paths, oks in loader:
        mask = oks.bool()
        if not mask.any(): continue
        t = imgs[mask].to(DEVICE, non_blocking=True)
        vp = [p for p, ok in zip(paths, oks.tolist()) if ok]
        with torch.no_grad(), torch.amp.autocast(
            device_type=DEVICE if DEVICE != "mps" else "cpu",
            enabled=(DEVICE == "cuda"),
        ):
            f = model.encode_image(t); f /= f.norm(dim=-1, keepdim=True)
            pf = pos_feat.to(f.dtype); nf = neg_feat.to(f.dtype)
            ps = (f @ pf.T).mean(1); ns = (f @ nf.T).mean(1)
            fs = ps - ns * NEG_WEIGHT
        embs = f.float().cpu().numpy()
        for path, p_s, n_s, f_s, emb in zip(vp, ps.float().cpu().tolist(),
                                              ns.float().cpu().tolist(),
                                              fs.float().cpu().tolist(), embs):
            path_to_scores[path] = (f_s, p_s, n_s)
            path_to_emb[path] = emb

    for clip, fp in zip(all_clips, peak_frame_paths):
        sc = path_to_scores.get(str(fp))
        if sc:
            clip["score"], clip["pos_score"], clip["neg_score"] = sc

    # Aesthetic scoring
    try:
        import torch.nn as nn
        class _MLP(nn.Module):
            def __init__(self):
                super().__init__()
                self.layers = nn.Sequential(
                    nn.Linear(768, 1024), nn.Dropout(0.2),
                    nn.Linear(1024, 128), nn.Dropout(0.2),
                    nn.Linear(128, 64), nn.Dropout(0.1),
                    nn.Linear(64, 16), nn.Linear(16, 1),
                )
            def forward(self, x): return self.layers(x)

        import urllib.request
        cache = Path.home() / ".cache/aesthetic_predictor/sac+logos+ava1-l14-linearMSE.pth"
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(
                "https://github.com/christophschuhmann/improved-aesthetic-predictor"
                "/raw/main/sac%2Blogos%2Bava1-l14-linearMSE.pth", str(cache))
        aes = _MLP()
        state = torch.load(str(cache), map_location="cpu", weights_only=True)
        if any(k.startswith("model.") for k in state): state = {k[6:]: v for k, v in state.items()}
        aes.load_state_dict(state); aes = aes.eval().to(DEVICE)
        stems = [c["scene"] for c in all_clips]
        emb_t = torch.tensor(
            np.array([path_to_emb.get(str(frames_dir / f"{s}_f0.jpg"),
                      np.zeros(768, dtype=np.float32)) for s in stems]),
            dtype=torch.float32,
        ).to(DEVICE)
        emb_t = emb_t / emb_t.norm(dim=-1, keepdim=True)
        with torch.no_grad():
            aes_vals = aes(emb_t).squeeze(-1).cpu().tolist()
        for clip, av in zip(all_clips, aes_vals):
            clip["aesthetic_score"] = round(av, 4)
        print(f"Aesthetic: {min(aes_vals):.2f}–{max(aes_vals):.2f}  mean={sum(aes_vals)/len(aes_vals):.2f}")
    except Exception as e:
        print(f"Aesthetic scoring skipped: {e}")
        for clip in all_clips: clip.setdefault("aesthetic_score", float("nan"))

    # ── Write CSVs ────────────────────────────────────────────────────────────
    fieldnames = ["scene", "score", "pos_score", "neg_score", "aesthetic_score"]
    main_clips = [c for c in all_clips if c["is_main"]]
    all_clips_sorted  = sorted(all_clips, key=lambda c: c["score"], reverse=True)
    main_clips_sorted = sorted(main_clips, key=lambda c: c["score"], reverse=True)

    with open(OUTPUT_CSV_ALLCAM, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader(); w.writerows(all_clips_sorted)
    with open(OUTPUT_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader(); w.writerows(main_clips_sorted)

    # Write duration_cache.json so the gallery knows each clip's length
    import json as _json
    _dur_cache = {f"{c['scene']}.mp4": CLIP_DUR_SEC for c in all_clips}
    (AUTO_DIR / "duration_cache.json").write_text(_json.dumps(_dur_cache))

    print(f"\nScored: {len(main_clips_sorted)} main-cam clips"
          + (f" + {len(all_clips)-len(main_clips)} back-cam" if len(all_clips) > len(main_clips) else ""))
    if main_clips_sorted:
        sc = [c["score"] for c in main_clips_sorted]
        print(f"Score range: {min(sc):.3f} – {max(sc):.3f}")
    print(f"\nTop 10:")
    for c in main_clips_sorted[:10]:
        print(f"  {c['scene']:50s} {c['score']:.4f}")
else:
    print("No clips extracted.")
    sys.exit(1)
