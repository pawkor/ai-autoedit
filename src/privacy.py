#!/usr/bin/env python3
"""
Privacy filter: blur motorcycle speedometer TFT display and license plates.

Speedometer: EasyOCR finds the largest 2-3 digit number in the lower 60% of frame
License plates: YOLOv8 (ultralytics) or EasyOCR fallback.

Implements "Temporal Keyframed Blurring":
Instead of one static box covering all detections, we animate the blur box 
between detections across SAMPLE_COUNT frames, ensuring a much tighter and 
cleaner look.
"""

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

# ── Suppress ultralytics GitHub rate-limit check (once per day max) ───────────
_YOLO_STAMP = Path.home() / '.cache' / 'ultralytics_online_check'
try:
    _last_check = _YOLO_STAMP.stat().st_mtime if _YOLO_STAMP.exists() else 0
    if time.time() - _last_check < 86400:
        os.environ.setdefault('YOLO_OFFLINE', '1')
    else:
        _YOLO_STAMP.parent.mkdir(parents=True, exist_ok=True)
        _YOLO_STAMP.touch()
except Exception:
    os.environ.setdefault('YOLO_OFFLINE', '1')

# ── Tuning knobs ──────────────────────────────────────────────────────────────
# Defaults — can be overridden in config.ini [privacy]
DETECT_SCALE   = 2      # downsample factor (4K → 1920×1080) — better OCR accuracy
SAMPLE_COUNT   = 5      # frames to sample per clip
LOWER_FRAC     = 0.50   # speedometer lives in lower half of frame
SPEED_MIN_H    = 35     # min digit bbox height in detection-scale pixels (35 @ 1920×1080)
SPEED_MIN_CONF = 0.7    # min OCR confidence for speedometer reading
LP_MIN_W       = 50     # min LP width in detection-scale pixels
LP_MIN_CONF    = 0.85   # min YOLO confidence for plate detection (high: model over-detects logos)
LP_MIN_ASPECT  = 2.0    # min width/height ratio (EU plates ~4.7:1, US ~2:1; logos/gloves fail)
LP_MAX_W_FRAC  = 0.22   # max plate width as fraction of frame (plates small vs logos on helmet)
PAD_SPEED      = 30     # padding around speedometer region (detect-scale px)
PAD_LP         = 15     # padding around LP region (detect-scale px)
PIX_BLOCK      = 20     # pixelation block size (original-scale px)
CONSENSUS_MIN  = 3      # min detections across SAMPLE_COUNT frames to accept a region

# ── Model singletons ─────────────────────────────────────────────────────────
# None = not yet tried; False = tried and unavailable; YOLO instance = loaded
_ocr   = None
_yolo  = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        import easyocr
        _ocr = easyocr.Reader(['en'], gpu=True, verbose=False)
    return _ocr


def _get_yolo():
    global _yolo
    if _yolo is None:
        from ultralytics import YOLO
        # Only use a specialized license plate model — generic COCO models
        # (yolov8n, yolo11n) have class 0 = person, not plate, and produce
        # false positives without a reliable class filter.
        try:
            _yolo = YOLO("yolo11n-license-plate.pt")
        except Exception:
            _yolo = False  # sentinel: tried, unavailable — do not retry
    return _yolo if _yolo else None


# ── Config loading ────────────────────────────────────────────────────────────

def _load_privacy_cfg():
    import configparser
    from pathlib import Path
    cp = configparser.ConfigParser()
    root = Path(__file__).resolve().parent.parent
    cp.read([str(root / "config.ini"), str(root / "webapp" / "config.ini")])
    
    global DETECT_SCALE, SAMPLE_COUNT, LOWER_FRAC, SPEED_MIN_H, SPEED_MIN_CONF
    global LP_MIN_W, PAD_SPEED, PAD_LP, PIX_BLOCK, CONSENSUS_MIN
    
    if cp.has_section("privacy"):
        s = "privacy"
        DETECT_SCALE   = cp.getint(s, "detect_scale", fallback=DETECT_SCALE)
        SAMPLE_COUNT   = cp.getint(s, "sample_count", fallback=SAMPLE_COUNT)
        LOWER_FRAC     = cp.getfloat(s, "lower_frac", fallback=LOWER_FRAC)
        SPEED_MIN_H    = cp.getint(s, "speed_min_h", fallback=SPEED_MIN_H)
        SPEED_MIN_CONF = cp.getfloat(s, "speed_min_conf", fallback=SPEED_MIN_CONF)
        LP_MIN_W       = cp.getint(s, "lp_min_w", fallback=LP_MIN_W)
        PAD_SPEED      = cp.getint(s, "pad_speed", fallback=PAD_SPEED)
        PAD_LP         = cp.getint(s, "pad_lp", fallback=PAD_LP)
        PIX_BLOCK      = cp.getint(s, "pix_block", fallback=PIX_BLOCK)
        CONSENSUS_MIN  = cp.getint(s, "consensus_min", fallback=CONSENSUS_MIN)


# ── Frame extraction ──────────────────────────────────────────────────────────

def _sample_frames(clip_path: Path, clip_ss: float, duration: float,
                   n: int, ffmpeg: str = "ffmpeg") -> list[np.ndarray]:
    """Return N BGR frames (downscaled for detection) from [clip_ss, clip_ss+dur]."""
    frames = []
    n = max(1, n)
    with tempfile.TemporaryDirectory() as td:
        for i in range(n):
            t = clip_ss + duration * (i + 0.5) / n
            out = Path(td) / f"f{i}.jpg"
            cmd = [
                ffmpeg, "-y",
                "-ss", f"{t:.3f}",
                "-i", str(clip_path),
                "-vf", f"scale=iw/{DETECT_SCALE}:ih/{DETECT_SCALE}",
                "-frames:v", "1", "-q:v", "3",
                str(out),
            ]
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0 and out.exists():
                img = cv2.imread(str(out))
                if img is not None:
                    frames.append(img)
    return frames


# ── Speedometer detection ─────────────────────────────────────────────────────

def _detect_speedometer(frame_bgr: np.ndarray) -> tuple[int, int, int, int] | None:
    """Find the speedometer reading. Returns (x, y, w, h) in detection-scale coords."""
    h, w = frame_bgr.shape[:2]
    y_start = int(h * LOWER_FRAC)
    roi = frame_bgr[y_start:, :]

    ocr = _get_ocr()
    results = ocr.readtext(roi, detail=1, paragraph=False)

    best = None
    best_h = 0
    for (bbox, text, conf) in results:
        text = text.strip()
        if not re.fullmatch(r'\d{2,3}', text):
            continue
        speed = int(text)
        if speed < 5 or speed > 350:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        bx, by = int(min(xs)), int(min(ys)) + y_start
        bw, bh = int(max(xs) - min(xs)), int(max(ys) - min(ys))
        if bh < SPEED_MIN_H: continue
        if conf < SPEED_MIN_CONF: continue
        if bh > best_h:
            best_h = bh
            best = (bx, by, bw, bh)
    return best


# ── License plate detection ───────────────────────────────────────────────────

_LP_PATTERN = re.compile(r'^[A-Z0-9]{4,8}$')


def _detect_plates(frame_bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Detect license plates via specialized YOLO model or EasyOCR fallback."""
    yolo = _get_yolo()
    if yolo:
        results = yolo(frame_bgr, verbose=False)
        boxes = []
        for r in results:
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf < LP_MIN_CONF:
                    continue
                b = box.xyxy[0].cpu().numpy()
                bx, by, bx2, by2 = map(int, b)
                bw, bh = bx2 - bx, by2 - by
                if bw < LP_MIN_W:
                    continue
                if bh > 0 and bw / bh < LP_MIN_ASPECT:
                    continue
                if bw > frame_bgr.shape[1] * LP_MAX_W_FRAC:
                    continue
                boxes.append((bx, by, bw, bh))
        if boxes:
            return boxes

    ocr = _get_ocr()
    results = ocr.readtext(frame_bgr, detail=1, paragraph=False)
    boxes = []
    for (bbox, text, conf) in results:
        text_clean = re.sub(r'[^A-Z0-9]', '', text.upper())
        if not _LP_PATTERN.match(text_clean): continue
        if not re.search(r'\d', text_clean): continue  # brand names (SHOEI, AIROH, ARAI) have no digits
        if conf < 0.4: continue
        xs, ys = [p[0] for p in bbox], [p[1] for p in bbox]
        bx, by = int(min(xs)), int(min(ys))
        bw, bh = int(max(xs) - min(xs)), int(max(ys) - min(ys))
        if bw < LP_MIN_W or (bh > 0 and bw / bh < LP_MIN_ASPECT): continue
        fw = frame_bgr.shape[1]
        if bw > fw * LP_MAX_W_FRAC: continue
        boxes.append((bx, by, bw, bh))
    return boxes


# ── Region helpers ────────────────────────────────────────────────────────────

def _pad(x, y, w, h, p, fw, fh):
    nx, ny = max(0, x - p), max(0, y - p)
    return nx, ny, min(fw - nx, w + 2 * p), min(fh - ny, h + 2 * p)

def _union(regions):
    if not regions: return None
    x1, y1 = min(r[0] for r in regions), min(r[1] for r in regions)
    x2, y2 = max(r[0] + r[2] for r in regions), max(r[1] + r[3] for r in regions)
    return x1, y1, x2 - x1, y2 - y1

def _scale_to_orig(region, scale):
    return tuple(v * scale for v in region)


# ── Public API ────────────────────────────────────────────────────────────────

def detect_clip_regions(
    clip_path: Path,
    clip_ss: float,
    duration: float,
    ffmpeg: str = "ffmpeg",
    detect_speed: bool = True,
    detect_plates: bool = True,
) -> list[dict]:
    """
    Returns list of 'clusters' (speedo, or individual vehicles).
    Each cluster has 'detections': { frame_idx: (x,y,w,h) } in ORIGINAL resolution.
    """
    _load_privacy_cfg()
    frames = _sample_frames(clip_path, clip_ss, duration, SAMPLE_COUNT, ffmpeg)
    if not frames: return []
    fh, fw = frames[0].shape[:2]

    speed_cluster = {"type": "speed", "detections": {}}
    plate_clusters = []

    for i, frame in enumerate(frames):
        if detect_speed:
            r = _detect_speedometer(frame)
            if r:
                speed_cluster["detections"][i] = _scale_to_orig(_pad(*r, PAD_SPEED, fw, fh), DETECT_SCALE)

        if detect_plates:
            for r in _detect_plates(frame):
                orig_r = _scale_to_orig(_pad(*r, PAD_LP, fw, fh), DETECT_SCALE)
                placed = False
                cx, cy = orig_r[0] + orig_r[2]/2, orig_r[1] + orig_r[3]/2
                for pc in plate_clusters:
                    # Find a detection in THIS cluster from ANY previous frame to compare
                    for prev_r in pc["detections"].values():
                        pcx, pcy = prev_r[0] + prev_r[2]/2, prev_r[1] + prev_r[3]/2
                        if abs(cx - pcx) < 150 and abs(cy - pcy) < 150:
                            pc["detections"][i] = orig_r
                            placed = True
                            break
                    if placed: break
                if not placed:
                    plate_clusters.append({"type": "plate", "detections": {i: orig_r}})

    final_clusters = []
    if len(speed_cluster["detections"]) >= CONSENSUS_MIN:
        final_clusters.append(speed_cluster)

    for pc in plate_clusters:
        if len(pc["detections"]) >= max(2, CONSENSUS_MIN - 1):
            final_clusters.append(pc)

    # Store original-resolution frame dimensions so blur_vf can compute
    # numeric crop bounds (ffmpeg's crop filter doesn't support main_w/main_h).
    orig_w, orig_h = fw * DETECT_SCALE, fh * DETECT_SCALE
    for c in final_clusters:
        c["frame_w"] = orig_w
        c["frame_h"] = orig_h

    return final_clusters


def blur_vf(clusters: list[dict], duration: float, pix_block: int = PIX_BLOCK) -> str:
    """
    Builds a filter graph that interpolates the blur box position across duration.
    """
    if not clusters: return ""
    parts = []
    prev = "0:v"

    for i, cluster in enumerate(clusters):
        # 1. Calculate a global "max size" for this cluster's union (crop needs static size)
        all_det = list(cluster["detections"].values())
        u = _union(all_det)
        if not u: continue
        
        # Use union box size + 20% to allow for some movement outside detection points
        bw, bh = int(u[2] * 1.1), int(u[3] * 1.1)
        bw, bh = bw + (bw % 2), bh + (bh % 2) # ensure even
        
        # 2. Build interpolation expressions for X and Y
        # Sample points are at T = dur * (i + 0.5) / SAMPLE_COUNT
        pts = []
        for idx, det in sorted(cluster["detections"].items()):
            t = duration * (idx + 0.5) / SAMPLE_COUNT
            # Center of the box
            cx, cy = det[0] + det[2]/2, det[1] + det[3]/2
            pts.append((t, cx, cy))
        
        # Expression builders
        def lerp(p1, p2, val_idx):
            t1, v1 = p1[0], p1[val_idx]
            t2, v2 = p2[0], p2[val_idx]
            return f"({v1}+({v2}-{v1})*(t-{t1:.3f})/{t2-t1:.3f})"

        if len(pts) == 1:
            expr_x, expr_y = f"{pts[0][1]:.3f}", f"{pts[0][2]:.3f}"
        else:
            expr_x, expr_y = "", ""
            for j in range(len(pts)-1):
                cond = f"between(t,{pts[j][0]:.3f},{pts[j+1][0]:.3f})"
                lx, ly = lerp(pts[j], pts[j+1], 1), lerp(pts[j], pts[j+1], 2)
                expr_x = f"if({cond},{lx},{expr_x if expr_x else pts[j][1]})"
                expr_y = f"if({cond},{ly},{expr_y if expr_y else pts[j][2]})"
            
        # Final offset: center minus half-width, clamped to frame boundaries.
        # ffmpeg crop filter doesn't have main_w/main_h (overlay-only variables),
        # so use numeric bounds from stored frame dimensions when available.
        _fw = cluster.get("frame_w", 0)
        _fh = cluster.get("frame_h", 0)
        if _fw > 0 and _fh > 0:
            final_x = f"clip({expr_x}-{bw/2:.1f},0,{max(0, _fw - bw)})"
            final_y = f"clip({expr_y}-{bh/2:.1f},0,{max(0, _fh - bh)})"
        else:
            final_x = f"clip({expr_x}-{bw/2:.1f},0,iw-{bw})"
            final_y = f"clip({expr_y}-{bh/2:.1f},0,ih-{bh})"

        pix_w, pix_h = max(1, bw // pix_block), max(1, bh // pix_block)
        is_last = i == len(clusters) - 1
        out_label = "privacy_out" if is_last else f"prv{i}"
        
        parts.append(
            f"[{prev}]split[base{i}][tmp{i}];"
            f"[tmp{i}]crop={bw}:{bh}:x='{final_x}':y='{final_y}',"
            f"scale={pix_w}:{pix_h},scale={bw}:{bh}:flags=neighbor[pix{i}];"
            f"[base{i}][pix{i}]overlay=x='{final_x}':y='{final_y}':shortest=1"
            f"[{out_label}]"
        )
        prev = out_label

    return ";".join(parts)
