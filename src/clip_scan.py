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
  CLIP_SCAN_PHASE          all | reselect | reextract (default: all)
"""
import configparser
import csv
import json as _json
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
os.environ.setdefault("HUGGINGFACE_HUB_VERBOSITY", "error")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
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
_default_workers = "0" if __import__("platform").system() == "Darwin" else str(min(4, os.cpu_count() or 1))
NUM_WORKERS   = int(_e("CLIP_NUM_WORKERS", _cfg.get("clip_scoring", "num_workers", fallback=_default_workers)))
INTERVAL_SEC  = float(_e("CLIP_SCAN_INTERVAL_SEC", "3"))
CLIP_DUR_SEC  = float(_e("CLIP_SCAN_CLIP_DUR_SEC",  "8"))
MIN_GAP_SEC   = float(_e("CLIP_SCAN_MIN_GAP_SEC",  "30"))
SMOOTH_WIN    = int(_e("CLIP_SCAN_SMOOTH", "3"))
SCORE_FLOOR   = float(_e("CLIP_SCAN_THRESHOLD", "0.0"))
NEG_WEIGHT    = _cfg.getfloat("clip_scoring", "neg_weight", fallback=0.5)
CLIP_SCAN_PHASE = _e("CLIP_SCAN_PHASE", "all")   # all | reselect | reextract

# Intermediate cache dirs
_raw_scores_dir = AUTO_DIR / "frame_raw_scores"   # per-source raw CLIP scores
_peaks_dir      = AUTO_DIR / "selected_peaks"     # per-source peak timestamps

def _parse_prompts(raw):
    return [l.strip() for l in raw.strip().splitlines() if l.strip()]

POSITIVE_PROMPTS = _parse_prompts(_cfg.get("clip_prompts", "positive", fallback=""))
NEGATIVE_PROMPTS = _parse_prompts(_cfg.get("clip_prompts", "negative", fallback=""))

if not POSITIVE_PROMPTS or not NEGATIVE_PROMPTS:
    print("ERROR: CLIP prompts not configured. Run Settings → Describe this ride.", file=sys.stderr)
    sys.exit(1)

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".ts"}


# ── CLIP model (lazy — skipped for reextract) ─────────────────────────────────
if torch.cuda.is_available():    DEVICE = "cuda"
elif torch.backends.mps.is_available(): DEVICE = "mps"
else:                            DEVICE = "cpu"

print(f"Device: {DEVICE}")
if DEVICE == "cuda":
    print(f"GPU: {torch.cuda.get_device_name(0)}")
print(f"Phase: {CLIP_SCAN_PHASE}  Interval: {INTERVAL_SEC}s  Clip dur: {CLIP_DUR_SEC}s  Min gap: {MIN_GAP_SEC}s")

_model = _preprocess = _tokenizer = _pos_feat = _neg_feat = None

def _ensure_model():
    global _model, _preprocess, _tokenizer, _pos_feat, _neg_feat
    if _model is not None:
        return
    print("Loading CLIP model...")
    _model, _, _preprocess = open_clip.create_model_and_transforms('ViT-L-14', pretrained='openai')
    _tokenizer = open_clip.get_tokenizer('ViT-L-14')
    _model = _model.to(DEVICE).eval()
    with torch.no_grad():
        pos_tok = _tokenizer(POSITIVE_PROMPTS).to(DEVICE)
        neg_tok = _tokenizer(NEGATIVE_PROMPTS).to(DEVICE)
        _pos_feat = _model.encode_text(pos_tok); _pos_feat /= _pos_feat.norm(dim=-1, keepdim=True)
        _neg_feat = _model.encode_text(neg_tok); _neg_feat /= _neg_feat.norm(dim=-1, keepdim=True)


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
    _ensure_model()
    import platform as _plat
    _nw = 0 if _plat.system() == "Darwin" else NUM_WORKERS
    ds = _FrameDS(frame_paths, _preprocess)
    loader = DataLoader(ds, batch_size=BATCH_SIZE, num_workers=_nw,
                        pin_memory=(DEVICE == "cuda"),
                        prefetch_factor=2 if _nw > 0 else None)
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
            f = _model.encode_image(t); f /= f.norm(dim=-1, keepdim=True)
            pf = _pos_feat.to(f.dtype); nf = _neg_feat.to(f.dtype)
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


def _extract_frames_to_dir(src: Path, out_dir: Path, interval: float) -> list[Path]:
    """Extract 1 frame per interval sec → out_dir/000001.jpg, 000002.jpg, …"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-i", str(src),
        "-vf", f"fps=1/{interval},scale=trunc(iw/4)*2:trunc(ih/4)*2",
        "-q:v", "5", str(out_dir / "%06d.jpg"),
    ]
    subprocess.run(cmd, capture_output=True)
    return sorted(out_dir.glob("*.jpg"))


def _extract_single_frame(src: Path, ts: float, out: Path):
    """Extract one frame at timestamp ts from src."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-ss", f"{ts:.3f}", "-i", str(src),
        "-vframes", "1", "-q:v", "5", str(out), "-loglevel", "error",
    ]
    subprocess.run(cmd, capture_output=True)


_X264_CRF    = str(_cfg.getint("video", "x264_crf",    fallback=15))
_X264_PRESET = _cfg.get("video", "x264_preset", fallback="fast")


def _extract_clip(src: Path, start: float, duration: float, out: Path):
    """Extract clip from source, re-encode to H264 for uniform codec in concat."""
    start = max(0.0, start)
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-crf", _X264_CRF, "-preset", _X264_PRESET,
        "-bf", "0",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        str(out), "-loglevel", "error",
    ]
    subprocess.run(cmd, capture_output=True)


# ── Source files ──────────────────────────────────────────────────────────────
def _find_videos(directory):
    seen = set()
    for ext in VIDEO_EXT:
        for f in directory.glob(f"*{ext}"):
            if f not in seen:
                seen.add(f); yield f
        for f in directory.glob(f"*{ext.upper()}"):
            if f not in seen:
                seen.add(f); yield f

if CAMERAS:
    source_files = sorted(
        f for cam in CAMERAS
        for f in _find_videos(WORK_DIR / cam)
        if not f.name.lower().startswith("highlight")
    )
else:
    source_files = sorted(
        f for f in _find_videos(WORK_DIR)
        if not f.name.lower().startswith("highlight")
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

all_clips: list[dict] = []


# ── Phase: reextract — re-cut clips only, keep frames + CSVs ─────────────────
if CLIP_SCAN_PHASE == "reextract":
    for sf_idx, sf in enumerate(source_files, 1):
        peaks_path = _peaks_dir / f"{sf.stem}.json"
        if not peaks_path.exists():
            print(f"  [{sf_idx}/{len(source_files)}] {sf.name}: no cached peaks — skipping")
            continue
        pd = _json.loads(peaks_path.read_text())
        cam = next((c for c in CAMERAS if f"/{c}/" in str(sf)), CAMERAS[0] if CAMERAS else "")
        is_main = (cam == AUDIO_CAM or not CAMERAS)
        for old in autocut_dir.glob(f"{sf.stem}-clip-*.mp4"):
            old.unlink()
        for i, peak in enumerate(pd["peaks"], 1):
            clip_start = max(0.0, peak["ts"] - CLIP_DUR_SEC / 2)
            scene_name = f"{sf.stem}-clip-{i:03d}"
            _extract_clip(sf, clip_start, CLIP_DUR_SEC, autocut_dir / f"{scene_name}.mp4")
            all_clips.append({"scene": scene_name, "score": peak["score"],
                               "pos_score": 0.0, "neg_score": 0.0,
                               "is_main": is_main, "offset_sec": clip_start})
        pd["clip_dur"] = CLIP_DUR_SEC
        (_peaks_dir / f"{sf.stem}.json").write_text(_json.dumps(pd))
        print(f"  [{sf_idx}/{len(source_files)}] {sf.name}: {len(pd['peaks'])} clips re-extracted")

    _dur_cache = {f"{c['scene']}.mp4": CLIP_DUR_SEC for c in all_clips}
    (AUTO_DIR / "duration_cache.json").write_text(_json.dumps(_dur_cache))
    print(f"\nRe-extracted: {len(all_clips)} clips with dur={CLIP_DUR_SEC}s  (frames+CSVs unchanged)")
    sys.exit(0)


# ── Phase: reselect — reload raw scores, re-pick peaks, re-extract ────────────
if CLIP_SCAN_PHASE == "reselect":
    _ensure_model()
    min_gap_frames = max(1, int(MIN_GAP_SEC / INTERVAL_SEC))
    for sf_idx, sf in enumerate(source_files, 1):
        raw_path = _raw_scores_dir / f"{sf.stem}.json"
        if not raw_path.exists():
            print(f"  [{sf_idx}/{len(source_files)}] {sf.name}: no cached raw scores — skipping")
            continue
        raw_data   = _json.loads(raw_path.read_text())
        raw_scores = raw_data["scores"]
        timestamps = raw_data["timestamps"]
        cam = next((c for c in CAMERAS if f"/{c}/" in str(sf)), CAMERAS[0] if CAMERAS else "")
        is_main = (cam == AUDIO_CAM or not CAMERAS)
        for old in autocut_dir.glob(f"{sf.stem}-clip-*.mp4"):
            old.unlink()
        for old in frames_dir.glob(f"{sf.stem}-clip-*_f0.jpg"):
            old.unlink()
        smoothed   = _smooth(raw_scores, SMOOTH_WIN)
        peak_idxs  = _find_peaks(smoothed, min_gap_frames, SCORE_FLOOR)
        if not peak_idxs:
            print(f"  [{sf_idx}/{len(source_files)}] {sf.name}: no peaks")
            continue
        _peaks_list = []
        for i, peak_i in enumerate(peak_idxs, 1):
            peak_ts    = timestamps[peak_i]
            clip_start = max(0.0, peak_ts - CLIP_DUR_SEC / 2)
            scene_name = f"{sf.stem}-clip-{i:03d}"
            _extract_clip(sf, clip_start, CLIP_DUR_SEC, autocut_dir / f"{scene_name}.mp4")
            _extract_single_frame(sf, peak_ts, frames_dir / f"{scene_name}_f0.jpg")
            all_clips.append({"scene": scene_name, "score": raw_scores[peak_i],
                               "pos_score": 0.0, "neg_score": 0.0,
                               "is_main": is_main, "offset_sec": clip_start})
            _peaks_list.append({"ts": peak_ts, "score": smoothed[peak_i], "clip_name": scene_name})
        _peaks_dir.mkdir(parents=True, exist_ok=True)
        (_peaks_dir / f"{sf.stem}.json").write_text(
            _json.dumps({"min_gap": MIN_GAP_SEC, "clip_dur": CLIP_DUR_SEC, "peaks": _peaks_list}))
        print(f"  [{sf_idx}/{len(source_files)}] {sf.name}: {len(peak_idxs)} peaks")


# ── Phase: all — full scan → score → peaks → extract ─────────────────────────
if CLIP_SCAN_PHASE == "all":
    _ensure_model()
    min_gap_frames = max(1, int(MIN_GAP_SEC / INTERVAL_SEC))
    _raw_scores_dir.mkdir(parents=True, exist_ok=True)
    _peaks_dir.mkdir(parents=True, exist_ok=True)

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

            t0 = time.time()
            raw_scores = _score_frames(frame_paths)
            elapsed = time.time() - t0
            print(f"    Scored {len(raw_scores)} frames in {elapsed:.1f}s")

            # Save raw scores for future reselect
            _timestamps = [i * INTERVAL_SEC for i in range(len(raw_scores))]
            (_raw_scores_dir / f"{sf.stem}.json").write_text(
                _json.dumps({"interval": INTERVAL_SEC, "timestamps": _timestamps, "scores": raw_scores})
            )

            smoothed   = _smooth(raw_scores, SMOOTH_WIN)
            peak_idxs  = _find_peaks(smoothed, min_gap_frames, SCORE_FLOOR)
            if not peak_idxs:
                print(f"    No peaks found")
                continue

            print(f"    Peaks: {len(peak_idxs)}  (scores: {[round(smoothed[i],3) for i in peak_idxs[:8]]})")

            cam_clip_n  = sum(1 for c in all_clips if c["scene"].startswith(sf.stem + "-clip-"))
            _peaks_list = []
            for peak_i in peak_idxs:
                peak_ts    = peak_i * INTERVAL_SEC
                clip_start = max(0.0, peak_ts - CLIP_DUR_SEC / 2)
                cam_clip_n += 1
                scene_name  = f"{sf.stem}-clip-{cam_clip_n:03d}"

                clip_out = autocut_dir / f"{scene_name}.mp4"
                clip_newly_created = not clip_out.exists()
                if clip_newly_created:
                    _extract_clip(sf, clip_start, CLIP_DUR_SEC, clip_out)

                peak_frame_src = frame_paths[peak_i]
                peak_frame_dst = frames_dir / f"{scene_name}_f0.jpg"
                if clip_newly_created or not peak_frame_dst.exists():
                    import shutil
                    peak_frame_dst.unlink(missing_ok=True)
                    shutil.copy2(peak_frame_src, peak_frame_dst)

                all_clips.append({
                    "scene":      scene_name,
                    "score":      raw_scores[peak_i],
                    "pos_score":  0.0,
                    "neg_score":  0.0,
                    "is_main":    is_main,
                    "offset_sec": clip_start,
                })
                _peaks_list.append({"ts": peak_ts, "score": smoothed[peak_i], "clip_name": scene_name})

            # Save selected peaks for future reextract
            (_peaks_dir / f"{sf.stem}.json").write_text(
                _json.dumps({"min_gap": MIN_GAP_SEC, "clip_dur": CLIP_DUR_SEC, "peaks": _peaks_list})
            )


# ── Re-score peak frames for accurate pos/neg columns ─────────────────────────
if all_clips:
    peak_frame_paths = [frames_dir / f"{c['scene']}_f0.jpg" for c in all_clips]
    print(f"\nRe-scoring {len(peak_frame_paths)} peak frames for CSV...")

    _ensure_model()
    import platform as _plat
    _nw = 0 if _plat.system() == "Darwin" else NUM_WORKERS
    ds = _FrameDS(peak_frame_paths, _preprocess)
    loader = DataLoader(ds, batch_size=BATCH_SIZE, num_workers=_nw,
                        pin_memory=(DEVICE == "cuda"),
                        prefetch_factor=2 if _nw > 0 else None)
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
            f = _model.encode_image(t); f /= f.norm(dim=-1, keepdim=True)
            pf = _pos_feat.to(f.dtype); nf = _neg_feat.to(f.dtype)
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

    # Brightness (median Y-channel)
    try:
        import cv2 as _cv2
        for clip in all_clips:
            _fp = frames_dir / f"{clip['scene']}_f0.jpg"
            try:
                _img = _cv2.imread(str(_fp))
                if _img is not None:
                    _y = _cv2.cvtColor(_img, _cv2.COLOR_BGR2YUV)[:, :, 0]
                    clip["avg_brightness"] = round(float(np.median(_y)), 1)
                else:
                    clip["avg_brightness"] = float("nan")
            except Exception:
                clip["avg_brightness"] = float("nan")
    except ImportError:
        for clip in all_clips:
            clip.setdefault("avg_brightness", float("nan"))

    # Write CSVs
    fieldnames = ["scene", "score", "pos_score", "neg_score", "aesthetic_score", "offset_sec", "avg_brightness"]
    main_clips = [c for c in all_clips if c["is_main"]]
    all_clips_sorted  = sorted(all_clips, key=lambda c: c["score"], reverse=True)
    main_clips_sorted = sorted(main_clips, key=lambda c: c["score"], reverse=True)

    with open(OUTPUT_CSV_ALLCAM, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader(); w.writerows(all_clips_sorted)
    with open(OUTPUT_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader(); w.writerows(main_clips_sorted)

    # Write duration_cache.json
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
