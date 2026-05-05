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
LP_MIN_CONF    = 0.28   # min YOLO confidence — low for max coverage; LP_MIN_Y_FRAC handles FPs
LP_MIN_ASPECT  = 0.8    # min width/height ratio (model bbox ~1:1; EU plates ~4.7:1 theoretical)
LP_MAX_W_FRAC  = 0.22   # max plate width as fraction of frame (plates small vs logos on helmet)
LP_MIN_Y_FRAC  = 0.30   # plate center must be below top 30% of frame — rejects signs/billboards/scaffolding
DENSE_MIN_YOLO = 1      # min YOLO keyframe hits to accept a dense cluster (1 = max coverage)
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
            _model_path = Path(__file__).parent / "yolo11n-license-plate.pt"
            _yolo = YOLO(str(_model_path))
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
                # Reject detections in upper part of frame — shop signs, billboards
                if (by + bh / 2) < frame_bgr.shape[0] * LP_MIN_Y_FRAC:
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
        if len(re.findall(r'\d', text_clean)) < 2: continue  # brand names (SHOEI→SH0EI=1 digit) filtered; real plates have ≥2 digits
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


def _iou(a, b):
    """IoU between two (x, y, w, h) boxes."""
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
    ix2 = min(ax2, bx2);   iy2 = min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _lk_track(cap, start_frame: int, end_frame: int, fps: float,
              det_w: int, det_h: int, orig_w: int, orig_h: int,
              seed_r: tuple, seed_frame: int, step: int,
              forward: bool) -> dict:
    """Track a plate box from seed_frame using Lucas-Kanade optical flow.

    Returns {t_offset: (x,y,w,h)} for successfully tracked frames.
    """
    scale_x = det_w / orig_w
    scale_y = det_h / orig_h

    cap.set(cv2.CAP_PROP_POS_FRAMES, seed_frame)
    ret, frame = cap.read()
    if not ret:
        return {}
    prev_gray = cv2.cvtColor(cv2.resize(frame, (det_w, det_h)), cv2.COLOR_BGR2GRAY)

    cx_det = (seed_r[0] + seed_r[2] / 2) * scale_x
    cy_det = (seed_r[1] + seed_r[3] / 2) * scale_y
    pts = np.array([[cx_det, cy_det]], dtype=np.float32).reshape(-1, 1, 2)

    lk_params = dict(winSize=(31, 31), maxLevel=3,
                     criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.01))

    tracked: dict = {}
    curr_r = seed_r

    frame_seq = range(seed_frame + step, end_frame, step) if forward \
        else range(seed_frame - step, start_frame - 1, -step)

    for fnum in frame_seq:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fnum)
        ret, frame = cap.read()
        if not ret:
            break
        curr_gray = cv2.cvtColor(cv2.resize(frame, (det_w, det_h)), cv2.COLOR_BGR2GRAY)

        new_pts, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, curr_gray, pts, None, **lk_params)
        if status is None or status[0][0] != 1:
            break  # tracking lost

        nx, ny = new_pts[0][0]
        px, py = pts[0][0]
        # Stop if point jumps too far between frames — indicates LK drifted to background
        if abs(nx - px) > 80 or abs(ny - py) > 80:
            break

        new_r = (
            int(nx / scale_x - curr_r[2] / 2),
            int(ny / scale_y - curr_r[3] / 2),
            curr_r[2],
            curr_r[3],
        )
        t_off = (fnum - start_frame) / fps
        if t_off >= 0.0:
            tracked[t_off] = new_r

        curr_r = new_r
        pts = new_pts.copy()
        prev_gray = curr_gray

    return tracked


def _dense_plate_clusters(clip_path: Path, clip_ss: float, duration: float) -> list[dict]:
    """Per-frame plate tracking: YOLO on up to 60 frames + LK optical flow extension.

    Phase 1: YOLO detections with IoU-based cluster matching.
    Phase 2: LK flow extends each cluster forward and backward beyond YOLO keyframes.
    Returns clusters with float time-offset keys (seconds from clip_ss).
    """
    cap = cv2.VideoCapture(str(clip_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    det_w  = max(1, orig_w // DETECT_SCALE)
    det_h  = max(1, orig_h // DETECT_SCALE)

    start_f = int(clip_ss * fps)
    end_f   = int((clip_ss + duration) * fps)
    total   = max(1, end_f - start_f)
    step    = max(1, total // 60)  # at most 60 YOLO calls
    recency = 2 * step / fps + 0.1

    plate_clusters: list[dict] = []

    # ── Phase 1: YOLO detection ───────────────────────────────────────────────
    frame_num = start_f
    while frame_num < end_f:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = cap.read()
        if not ret:
            break
        t_off = (frame_num - start_f) / fps
        small = cv2.resize(frame, (det_w, det_h))

        for r in _detect_plates(small):
            orig_r = _scale_to_orig(_pad(*r, PAD_LP, det_w, det_h), DETECT_SCALE)
            cx = orig_r[0] + orig_r[2] / 2
            cy = orig_r[1] + orig_r[3] / 2

            best_pc, best_score = None, 0.0
            for pc in plate_clusters:
                if not pc["detections"]:
                    continue
                last_t = max(pc["detections"])
                if t_off - last_t > recency:
                    continue
                last_r = pc["detections"][last_t]
                iou = _iou(orig_r, last_r)
                lcx = last_r[0] + last_r[2] / 2
                lcy = last_r[1] + last_r[3] / 2
                close = abs(cx - lcx) < 200 and abs(cy - lcy) < 200
                # Allow centroid-only join (iou=0) when plate rotates: same plate, no overlap
                score = iou if iou >= 0.15 else (0.05 if close else 0.0)
                if score > best_score:
                    best_score, best_pc = score, pc

            if best_pc is not None:
                best_pc["detections"][t_off] = orig_r
            else:
                plate_clusters.append({
                    "type": "plate",
                    "detections": {t_off: orig_r},
                    "frame_w": orig_w,
                    "frame_h": orig_h,
                })
        frame_num += step

    # ── Filter: require ≥ DENSE_MIN_YOLO keyframe hits before LK extension ──
    # Single-frame detections are usually shop signs / road signs (noise).
    # Real plates visible for even 0.3 s get 2+ YOLO hits at step ~4 frames.
    plate_clusters = [pc for pc in plate_clusters if len(pc["detections"]) >= DENSE_MIN_YOLO]

    # ── Phase 2: LK optical flow extension ───────────────────────────────────
    for pc in plate_clusters:
        if not pc["detections"]:
            continue
        times = sorted(pc["detections"])

        # Forward: extend from last YOLO keyframe to end of clip
        last_t = times[-1]
        if last_t < duration - 0.1:
            last_fnum = start_f + int(last_t * fps)
            fwd = _lk_track(cap, start_f, end_f, fps, det_w, det_h, orig_w, orig_h,
                            pc["detections"][last_t], last_fnum, step, forward=True)
            for t, r in fwd.items():
                if t not in pc["detections"]:
                    pc["detections"][t] = r

        # Backward: extend from first YOLO keyframe to start of clip
        first_t = times[0]
        if first_t > 0.1:
            first_fnum = start_f + int(first_t * fps)
            bwd = _lk_track(cap, start_f, end_f, fps, det_w, det_h, orig_w, orig_h,
                            pc["detections"][first_t], first_fnum, step, forward=False)
            for t, r in bwd.items():
                if t not in pc["detections"]:
                    pc["detections"][t] = r

    cap.release()

    result = [pc for pc in plate_clusters if pc["detections"]]

    # Clamp first/last keyframe to clip boundaries
    for pc in result:
        times = sorted(pc["detections"])
        if times[0] > 0.05:
            pc["detections"][0.0] = pc["detections"][times[0]]
        if times[-1] < duration - 0.05:
            pc["detections"][duration] = pc["detections"][times[-1]]

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def detect_clip_regions(
    clip_path: Path,
    clip_ss: float,
    duration: float,
    ffmpeg: str = "ffmpeg",
    detect_speed: bool = True,
    detect_plates: bool = True,
    dense: bool = False,
) -> list[dict]:
    """
    Returns list of 'clusters' (speedo, or individual vehicles).
    Each cluster has 'detections': { t_offset: (x,y,w,h) } in ORIGINAL resolution,
    where t_offset is seconds from clip_ss (float).

    dense=True: use per-frame cv2 tracking for plates (IoU-based, up to 60 keyframes).
    """
    _load_privacy_cfg()
    frames = _sample_frames(clip_path, clip_ss, duration, SAMPLE_COUNT, ffmpeg)
    if not frames: return []
    fh, fw = frames[0].shape[:2]
    orig_w, orig_h = fw * DETECT_SCALE, fh * DETECT_SCALE
    n = len(frames)

    speed_cluster = {"type": "speed", "detections": {}}
    plate_clusters: list[dict] = []

    for i, frame in enumerate(frames):
        t_off = duration * (i + 0.5) / n

        if detect_speed:
            r = _detect_speedometer(frame)
            if r:
                speed_cluster["detections"][t_off] = _scale_to_orig(_pad(*r, PAD_SPEED, fw, fh), DETECT_SCALE)

        if detect_plates and not dense:
            for r in _detect_plates(frame):
                orig_r = _scale_to_orig(_pad(*r, PAD_LP, fw, fh), DETECT_SCALE)
                placed = False
                cx, cy = orig_r[0] + orig_r[2]/2, orig_r[1] + orig_r[3]/2
                for pc in plate_clusters:
                    for prev_r in pc["detections"].values():
                        pcx, pcy = prev_r[0] + prev_r[2]/2, prev_r[1] + prev_r[3]/2
                        if abs(cx - pcx) < 150 and abs(cy - pcy) < 150:
                            pc["detections"][t_off] = orig_r
                            placed = True
                            break
                    if placed: break
                if not placed:
                    plate_clusters.append({"type": "plate", "detections": {t_off: orig_r}})

    if detect_plates and dense:
        plate_clusters = _dense_plate_clusters(clip_path, clip_ss, duration)

    final_clusters = []
    if len(speed_cluster["detections"]) >= CONSENSUS_MIN:
        speed_cluster["frame_w"] = orig_w
        speed_cluster["frame_h"] = orig_h
        final_clusters.append(speed_cluster)

    for pc in plate_clusters:
        if len(pc["detections"]) >= 1:
            if "frame_w" not in pc:
                pc["frame_w"] = orig_w
                pc["frame_h"] = orig_h
            final_clusters.append(pc)

    return final_clusters


def blur_vf(clusters: list[dict], duration: float, pix_block: int = PIX_BLOCK) -> str:
    """
    Builds a filter graph that interpolates the blur box position across duration.
    """
    if not clusters: return ""
    parts = []
    prev = "0:v"

    for i, cluster in enumerate(clusters):
        all_det = list(cluster["detections"].values())
        if not all_det: continue

        # Box size = median individual detection size (NOT union of positions).
        # Union spans the entire plate travel path → huge width → crop filter error.
        widths  = sorted(d[2] for d in all_det)
        heights = sorted(d[3] for d in all_det)
        bw = int(widths[len(widths) // 2] * 1.3)
        bh = int(heights[len(heights) // 2] * 1.3)
        bw = bw + (bw % 2)
        bh = bh + (bh % 2)
        
        # 2. Build interpolation expressions for X and Y
        # detections keys are float time offsets (seconds from clip start)
        pts = []
        for t, det in sorted(cluster["detections"].items()):
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
        if _fw > 0: bw = min(bw, _fw - (_fw % 2))
        if _fh > 0: bh = min(bh, _fh - (_fh % 2))
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
