#!/usr/bin/env python3
"""insta360_scan.py — 360° footage → auto-reframed clips for the pool.

Pipeline (per project with a 360/ subdir of Insta360 X-series recordings):
  A. scan     LRV (dual-fisheye preview) → equirect → N yaw views every
              --interval s → SigLIP2 scores with [clip_prompts]  (GPU)
  B. select   per file: top windows (score peaks, min time gap), best yaw
  C. stitch   MediaSDKTest: ONE invocation per VID _00_/_10_ pair exporting
              the frames of ALL selected windows as jpg (FlowState +
              DirectionLock, optflow, 3840x1920). The SDK seeks — cost
              scales with window count, not file length.
  D. assemble ffmpeg per window: jpg sequence → v360 flat(yaw) → 1920x1080
              NVENC clip + AAC audio from the source .insv; creation_time
              metadata = source start + window offset.
  E. merge    scene_scores_allcam.csv, camera_sources.csv (camera="360"),
              duration_cache.json, frames/{scene}.jpg.

The Insta360 MediaSDK is a licensed, user-supplied dependency (EULA forbids
redistribution): [paths] insta360_mediasdk in config.ini must point at the
MediaSDKTest binary. Nothing from the SDK ships with this repo.

Caches in _autoframe/insta360/: raw scores per LRV (interval/yaws/prompts
hash) and a done-marker per stitched window set — re-runs are incremental.

Usage:
  python3 insta360_scan.py <work_dir> [--interval 2] [--yaws 8]
      [--clip-dur 6] [--min-gap 30] [--per-file 8] [--out-size 1920x1080]
"""
import argparse
import configparser
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MODEL = "ViT-SO400M-16-SigLIP2-384"
PRETRAINED = "webli"
VIEW_W, VIEW_H = 480, 270
H_FOV, V_FOV = 100, 62
LRV_ROLL = 90            # sideways bike mount; SDK output is gyro-levelled,
                         # the roll only matters for the cheap LRV scan
STITCH_SIZE = "3840x1920"
BATCH = 64
EXTRA_NEG = [
    "close-up of a motorcycle helmet blocking most of the view",
    "back of a motorcyclist filling the frame, scenery obscured",
    "camera pointing at the rider, road barely visible",
]

REPO_ROOT = Path(__file__).resolve().parent.parent


def log(msg: str) -> None:
    print(msg, flush=True)


# ── config ───────────────────────────────────────────────────────────────────

def load_cfg(work_dir: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read([REPO_ROOT / "config.ini", work_dir / "config.ini"])
    return cp


def sdk_binary(cp: configparser.ConfigParser) -> Path | None:
    raw = cp.get("paths", "insta360_mediasdk", fallback="").strip()
    if not raw:
        return None
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = REPO_ROOT / p
    p = p.resolve()
    if p.is_file() and os.access(p, os.X_OK) and (p.parent / "models").is_dir():
        return p
    return None


def prompts(cp) -> tuple[list[str], list[str], float, str]:
    def _lines(key):
        raw = cp.get("clip_prompts", key, fallback="")
        return [l.strip() for l in raw.splitlines() if l.strip()]
    pos = _lines("positive")
    neg = _lines("negative") + EXTRA_NEG
    neg_w = cp.getfloat("clip_scan", "neg_weight", fallback=0.5)
    ph = hashlib.sha256(("\n".join(pos) + "\n---\n" + "\n".join(neg)).encode()).hexdigest()[:16]
    return pos, neg, neg_w, ph


# ── discovery ────────────────────────────────────────────────────────────────

_VID_RE = re.compile(r"^VID_(\d{8}_\d{6})_00_(\d+)\.insv$", re.IGNORECASE)


def discover(dir360: Path) -> list[dict]:
    """VID _00_/_10_ pairs + their LRV. Sorted by name (= chronological)."""
    pairs = []
    for f in sorted(dir360.iterdir()):
        m = _VID_RE.match(f.name)
        if not m:
            continue
        ts, seq = m.group(1), m.group(2)
        p10 = dir360 / f"VID_{ts}_10_{seq}.insv"
        lrv = dir360 / f"LRV_{ts}_11_{seq}.insv"
        if not p10.exists():
            log(f"  ! {f.name}: missing _10_ counterpart — skipped")
            continue
        pairs.append({"stem": f.stem, "vid00": f, "vid10": p10,
                      "lrv": lrv if lrv.exists() else None})
    return pairs


def probe_fps_dur_ct(ffprobe: str, path: Path) -> tuple[float, float, float | None]:
    """(fps, duration_sec, creation_epoch|None)."""
    r = subprocess.run(
        [ffprobe, "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,duration",
         "-show_entries", "format_tags=creation_time",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=15)
    j = json.loads(r.stdout or "{}")
    st = (j.get("streams") or [{}])[0]
    num, _, den = (st.get("r_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1)
    dur = float(st.get("duration") or 0)
    ct = None
    ts = (j.get("format", {}).get("tags") or {}).get("creation_time", "")
    if ts:
        try:
            ct = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fps, dur, ct


# ── phase A: scan ────────────────────────────────────────────────────────────

def scan_lrv(lrv: Path, cache: Path, interval: float, yaws: list[int],
             phash: str, ffmpeg: str, scorer) -> list[tuple]:
    """[(ts, yaw, score)] — cached per LRV."""
    if cache.exists():
        try:
            d = json.loads(cache.read_text())
            if (d.get("interval") == interval and d.get("yaws") == yaws
                    and d.get("prompts_hash") == phash):
                return [tuple(r) for r in d["rows"]]
        except Exception:
            pass
    with tempfile.TemporaryDirectory(prefix="i360scan_") as _td:
        td = Path(_td)
        parts = [f"[0:v]fps=1/{interval},"
                 f"v360=input=dfisheye:output=e:ih_fov=190:iv_fov=190:roll={LRV_ROLL},"
                 f"split={len(yaws)}" + "".join(f"[e{i}]" for i in range(len(yaws)))]
        maps = []
        for i, yaw in enumerate(yaws):
            d = td / f"y{yaw:03d}"
            d.mkdir()
            syaw = yaw - 360 if yaw > 180 else yaw
            parts.append(f"[e{i}]v360=e:flat:h_fov={H_FOV}:v_fov={V_FOV}:"
                         f"yaw={syaw}:w={VIEW_W}:h={VIEW_H}[v{i}]")
            maps += ["-map", f"[v{i}]", "-q:v", "4", str(d / "%05d.jpg")]
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", str(lrv), "-filter_complex", ";".join(parts),
                            *maps], capture_output=True)
        if r.returncode != 0:
            log(f"  ! scan extract failed for {lrv.name}: "
                f"{r.stderr.decode()[-200:]}")
            return []
        items = []
        for yaw in yaws:
            for p in sorted((td / f"y{yaw:03d}").glob("*.jpg")):
                items.append((p, (int(p.stem) - 1) * interval, yaw))
        rows = scorer(items)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"interval": interval, "yaws": yaws,
                                 "prompts_hash": phash, "rows": rows}))
    return [tuple(r) for r in rows]


def make_scorer(pos: list[str], neg: list[str], neg_w: float):
    import torch
    import open_clip
    from PIL import Image
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL, pretrained=PRETRAINED)
    model = model.to(dev).eval()
    tok = open_clip.get_tokenizer(MODEL)
    import torch as _t
    with _t.no_grad():
        pf = model.encode_text(tok(pos).to(dev)); pf /= pf.norm(dim=-1, keepdim=True)
        nf = model.encode_text(tok(neg).to(dev)); nf /= nf.norm(dim=-1, keepdim=True)

    def scorer(items):
        rows = []
        for i in range(0, len(items), BATCH):
            chunk = items[i:i + BATCH]
            imgs = _t.stack([preprocess(Image.open(p).convert("RGB"))
                             for p, _, _ in chunk]).to(dev)
            with _t.no_grad(), _t.amp.autocast(
                    device_type=dev, enabled=(dev == "cuda")):
                f = model.encode_image(imgs); f /= f.norm(dim=-1, keepdim=True)
                s = ((f @ pf.T.to(f.dtype)).mean(1)
                     - (f @ nf.T.to(f.dtype)).mean(1) * neg_w)
            for (_, ts, yaw), sc in zip(chunk, s.float().cpu().tolist()):
                rows.append([ts, yaw, sc])
        return rows

    return scorer


# ── phase B: select ──────────────────────────────────────────────────────────

def select_windows(rows, dur_limit: float, clip_dur: float,
                   min_gap: float, per_file: int) -> list[dict]:
    """Greedy peaks: [{ts, yaw, score}], ts = window START, clamped to file."""
    if dur_limit < clip_dur:
        return []
    picked: list[dict] = []
    for ts, yaw, sc in sorted(rows, key=lambda r: -r[2]):
        if any(abs(ts - p["_center"]) < min_gap for p in picked):
            continue
        start = max(0.0, min(ts - clip_dur / 2, dur_limit - clip_dur))
        picked.append({"ts": round(start, 3), "yaw": int(yaw),
                       "score": round(sc, 5), "_center": ts})
        if len(picked) >= per_file:
            break
    for p in picked:
        p.pop("_center")
    return sorted(picked, key=lambda p: p["ts"])


# ── phase C: stitch ──────────────────────────────────────────────────────────

def stitch_windows(sdk: Path, pair: dict, windows: list[dict], fps: float,
                   clip_dur: float, out_dir: Path) -> dict | None:
    """One MediaSDKTest run for all windows → jpgs in out_dir.
    Returns {window_idx: [frame indices]} or None on failure."""
    frames_per = max(1, round(clip_dur * fps))
    idx_map: dict[int, list[int]] = {}
    all_idx: list[int] = []
    for wi, w in enumerate(windows):
        first = round(w["ts"] * fps)
        idxs = list(range(first, first + frames_per))
        idx_map[wi] = idxs
        all_idx += idxs
    out_dir.mkdir(parents=True, exist_ok=True)
    _budget = int(120 * len(windows) + 600)
    _logf = out_dir.parent / f"{pair['stem']}.sdk.log"
    # Rendering accel: try auto first; a Vulkan-less environment (e.g. the
    # container without a GPU ICD) segfaults there — the SDK's documented
    # remedy is -image_processing_accel cpu, so retry with it.
    for _accel in ("auto", "cpu"):
        cmd = [str(sdk),
               "-inputs", str(pair["vid00"]), str(pair["vid10"]),
               "-image_sequence_dir", str(out_dir),
               "-image_type", "jpg",
               "-export_frame_index", "-".join(map(str, all_idx)),
               "-stitch_type", "optflow",
               "-output_size", STITCH_SIZE,
               # FlowState only, NO DirectionLock: direction lock keeps the
               # file's INITIAL heading, while the LRV scan picks yaw
               # relative to the camera's current heading — after a turn the
               # two frames would diverge. FlowState levels the horizon and
               # follows the camera, matching the scan's yaw space.
               "-enable_flowstate",
               "-image_processing_accel", _accel,
               "-model_root_dir", str(sdk.parent / "models") + "/"]
        # SDK stdout goes to a FILE, not a pipe buffer: a misbehaving run
        # can spam progress/errors for minutes and capture_output=True holds
        # all of it in RAM (this OOM-killed the module at 30 GB once).
        try:
            with open(_logf, "w") as _lf:
                r = subprocess.run(cmd, stdout=_lf, stderr=subprocess.STDOUT,
                                   text=True, timeout=_budget)
        except subprocess.TimeoutExpired:
            log(f"  ! stitch of {pair['stem']} timed out (> {_budget}s)")
            return None
        try:
            with open(_logf, errors="replace") as _lf:
                _lf.seek(0, os.SEEK_END)
                _sz = _lf.tell()
                _lf.seek(max(0, _sz - 4000))
                out = _lf.read()
        except OSError:
            out = ""
        # Argument errors DO return non-zero; runtime errors print to stdout
        # and still exit 0 — validate artifacts + feature status instead.
        produced = len(list(out_dir.glob("*.jpg")))
        if (r.returncode == 0 and produced >= len(all_idx) * 0.9
                and "error:" not in out.lower()):
            if _accel == "cpu":
                log("    (image processing on CPU — no usable Vulkan here)")
            if "flowstate: ON" not in out:
                log(f"  ! warning: FlowState not confirmed for {pair['stem']} "
                    f"(gyro missing?) — horizon may roll")
            return idx_map
        log(f"  ! stitch of {pair['stem']} incomplete (accel={_accel}, "
            f"rc={r.returncode}): {produced}/{len(all_idx)} frames; "
            f"tail: {out[-300:]}")
        for _p in out_dir.glob("*.jpg"):
            _p.unlink(missing_ok=True)
    return None


# ── phase D2: object-lock tracking ───────────────────────────────────────────
# Reproject–track–recenter: render a flat view at the current yaw/pitch,
# let a tracker measure the target's offset from centre, convert the offset
# to Δyaw/Δpitch, repeat. Distant landmarks (hills, castles) drift slowly
# across the sphere — the angular-velocity clamp naturally rejects close
# parallax objects the lock cannot follow anyway.

TRACK_W, TRACK_H = 960, 540
MAX_DEG_PER_SEC = 40.0
PITCH_LIMIT = 25.0
TRACK_OK_RATIO = 0.9


def _make_ray_grid(w: int, h: int, h_fov: float, v_fov: float):
    import numpy as np
    xs = np.linspace(-1, 1, w, dtype=np.float32) * np.tan(np.radians(h_fov / 2))
    ys = np.linspace(-1, 1, h, dtype=np.float32) * np.tan(np.radians(v_fov / 2))
    gx, gy = np.meshgrid(xs, ys)
    gz = np.ones_like(gx)
    n = np.sqrt(gx * gx + gy * gy + 1.0)
    return gx / n, gy / n, gz / n


def _remap_view(eq_bgr, rays, yaw_deg: float, pitch_deg: float):
    """Gnomonic view from an equirect frame. Centre pixel looks at lon=yaw
    (matches ffmpeg v360 yaw), y axis points down."""
    import numpy as np
    import cv2
    gx, gy, gz = rays
    p, y_ = np.radians(pitch_deg), np.radians(yaw_deg)
    cp, sp = np.cos(p), np.sin(p)
    ry = gy * cp - gz * sp
    rz1 = gy * sp + gz * cp
    cw, sw = np.cos(y_), np.sin(y_)
    rx = gx * cw + rz1 * sw
    rz = -gx * sw + rz1 * cw
    H, W = eq_bgr.shape[:2]
    lon = np.arctan2(rx, rz)
    lat = np.arcsin(np.clip(ry, -1.0, 1.0))
    mx = (((lon / (2 * np.pi)) + 0.5) % 1.0).astype(np.float32) * W
    my = ((lat / np.pi + 0.5) * H).astype(np.float32)
    return cv2.remap(eq_bgr, mx, my, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_WRAP)


def _make_tracker():
    import cv2
    for attr in ("TrackerCSRT_create", "TrackerMIL_create"):
        fn = getattr(cv2, attr, None) or getattr(
            getattr(cv2, "legacy", None) or object, attr, None)
        if fn:
            return fn()
    return None


def track_window(jpg_dir: Path, idxs: list, fps: float, yaw0: float):
    """(trajectory [(yaw,pitch)] aligned with idxs) or None when tracking
    is unavailable/unreliable — caller falls back to a fixed yaw."""
    try:
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
    except ImportError:
        return None
    if _make_tracker() is None:
        return None
    import cv2
    rays = _make_ray_grid(TRACK_W, TRACK_H, H_FOV, V_FOV)
    max_step = MAX_DEG_PER_SEC / fps

    def _run(seq):
        yawp, pitchp = float(yaw0), 0.0
        tracker, out, ok_ct = None, [], 0
        for i in seq:
            img = cv2.imread(str(jpg_dir / f"{i}.jpg"))
            if img is None:
                return None, 0
            view = _remap_view(img, rays, yawp, pitchp)
            if tracker is None:
                bw, bh = int(TRACK_W * 0.3), int(TRACK_H * 0.3)
                tracker = _make_tracker()
                tracker.init(view, (TRACK_W // 2 - bw // 2,
                                    TRACK_H // 2 - bh // 2, bw, bh))
                out.append((yawp, pitchp))
                ok_ct += 1
                continue
            ok, box = tracker.update(view)
            if ok and box[2] > 4 and box[3] > 4:
                cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
                dyaw = (cx - TRACK_W / 2) / TRACK_W * H_FOV
                dpitch = -(cy - TRACK_H / 2) / TRACK_H * V_FOV
                yawp += max(-max_step, min(max_step, dyaw))
                pitchp = max(-PITCH_LIMIT,
                             min(PITCH_LIMIT,
                                 pitchp + max(-max_step, min(max_step, dpitch))))
                ok_ct += 1
            out.append((yawp, pitchp))
        return out, ok_ct

    mid = len(idxs) // 2          # window is centred on the scan peak
    fwd, fwd_ok = _run(idxs[mid:])
    bwd, bwd_ok = _run(list(reversed(idxs[:mid + 1])))
    if fwd is None or bwd is None:
        return None
    if (fwd_ok + bwd_ok - 2) / max(1, len(idxs)) < TRACK_OK_RATIO:
        return None
    traj = list(reversed(bwd))[:-1] + fwd

    def _smooth(vals, w=7):
        return [sum(vals[max(0, i - w // 2):i + w // 2 + 1])
                / (min(len(vals), i + w // 2 + 1) - max(0, i - w // 2))
                for i in range(len(vals))]

    return list(zip(_smooth([t[0] for t in traj]),
                    _smooth([t[1] for t in traj])))


def assemble_tracked(ffmpeg: str, jpg_dir: Path, idxs: list, fps: float,
                     traj: list, out: Path, src: Path, ts: float, dur: float,
                     ct_epoch, out_w: int, out_h: int) -> bool:
    """Per-frame animated reframe: numpy remap → rawvideo pipe → NVENC."""
    import cv2
    meta = []
    if ct_epoch is not None:
        iso = datetime.fromtimestamp(ct_epoch + ts, tz=timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        meta = ["-metadata", f"creation_time={iso}"]
    rays = _make_ray_grid(out_w, out_h, H_FOV, V_FOV)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_w}x{out_h}",
           "-framerate", f"{fps:.6f}", "-i", "-",
           "-ss", f"{ts:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           "-map", "0:v", "-map", "1:a?",
           "-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "16M",
           "-c:a", "aac", "-b:a", "128k", "-shortest",
           *meta, str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        for i, (yw, pt) in zip(idxs, traj):
            img = cv2.imread(str(jpg_dir / f"{i}.jpg"))
            if img is None:
                raise RuntimeError(f"missing stitched frame {i}.jpg")
            proc.stdin.write(_remap_view(img, rays, yw, pt).tobytes())
        proc.stdin.close()
        proc.wait(timeout=300)
    except Exception as e:
        log(f"  ! tracked assemble failed for {out.name}: {e}")
        try:
            proc.kill()
        except Exception:
            pass
        return False
    if proc.returncode != 0 or not out.exists():
        err = b""
        try:
            err = proc.stderr.read()
        except Exception:
            pass
        log(f"  ! tracked assemble failed for {out.name}: "
            f"{err.decode(errors='replace')[-200:]}")
        return False
    return True


# ── phase D: assemble ────────────────────────────────────────────────────────

def assemble_clip(ffmpeg: str, jpg_dir: Path, idxs: list, fps: float,
                  yaw: int, out: Path, src: Path, ts: float, dur: float,
                  ct_epoch, out_w: int, out_h: int) -> bool:
    syaw = yaw - 360 if yaw > 180 else yaw
    meta = []
    if ct_epoch is not None:
        iso = datetime.fromtimestamp(ct_epoch + ts, tz=timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        meta = ["-metadata", f"creation_time={iso}"]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-framerate", f"{fps:.6f}", "-start_number", str(idxs[0]),
           "-i", str(jpg_dir / "%d.jpg"),
           "-ss", f"{ts:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           "-frames:v", str(len(idxs)),
           "-vf", f"v360=e:flat:h_fov={H_FOV}:v_fov={V_FOV}:yaw={syaw}:"
                  f"w={out_w}:h={out_h}",
           "-map", "0:v", "-map", "1:a?",
           "-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "16M",
           "-c:a", "aac", "-b:a", "128k", "-shortest",
           *meta, str(out)]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode != 0 or not out.exists():
        log(f"  ! assemble failed for {out.name}: {r.stderr.decode()[-200:]}")
        return False
    return True


def extract_pool_frame(ffmpeg: str, clip: Path, frames_dir: Path,
                       scene: str) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", "0.5", "-i", str(clip), "-frames:v", "1",
                    "-vf", "scale=640:-2", "-q:v", "4",
                    str(frames_dir / f"{scene}.jpg")],
                   capture_output=True, timeout=30)


# ── phase E: merge ───────────────────────────────────────────────────────────

CSV_COLS = ["scene", "score", "pos_score", "neg_score", "aesthetic_score",
            "offset_sec", "avg_brightness", "gps_speed_avg", "gps_speed_max",
            "gps_turn_max", "gps_altitude_avg", "gps_alt_change_max"]


def _manifest_path(auto_dir: Path) -> Path:
    return auto_dir / "insta360" / "own_scenes.json"


def save_manifest(auto_dir: Path, rows: list[dict], cam_sources: list[str],
                  durations: dict) -> None:
    """Ownership manifest: the normal pipeline rewrites the shared CSVs and
    prunes autocut/frames wholesale — this file lets it (a) protect our
    clips from cleanup and (b) re-merge our rows after its own writes."""
    p = _manifest_path(auto_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    if p.exists():
        try:
            old = json.loads(p.read_text())
        except Exception:
            old = {}
    by_scene = {r["scene"]: r for r in old.get("rows", [])}
    for r in rows:
        by_scene[r["scene"]] = r
    p.write_text(json.dumps({
        "rows": list(by_scene.values()),
        "cam_sources": sorted(set(old.get("cam_sources", []) + cam_sources)),
        "durations": {**old.get("durations", {}), **durations},
    }, indent=1))


def protected_scenes(auto_dir: Path) -> set:
    """Scene names owned by the 360 scan — pipeline cleanup must skip them."""
    p = _manifest_path(auto_dir)
    if not p.exists():
        return set()
    try:
        return {r["scene"] for r in json.loads(p.read_text()).get("rows", [])}
    except Exception:
        return set()


def reintegrate(auto_dir: Path) -> int:
    """Re-merge manifest rows into the shared CSVs/caches after the normal
    pipeline rebuilt them (it writes from scratch and drops foreign rows).
    Only scenes whose clip still exists are restored. Returns row count."""
    p = _manifest_path(auto_dir)
    if not p.exists():
        return 0
    try:
        m = json.loads(p.read_text())
    except Exception:
        return 0
    autocut = auto_dir / "autocut"
    rows = [r for r in m.get("rows", [])
            if (autocut / f"{r['scene']}.mp4").exists()]
    if not rows:
        return 0
    live = {r["scene"] for r in rows}
    merge_outputs(auto_dir, rows, m.get("cam_sources", []),
                  {k: v for k, v in m.get("durations", {}).items() if k in live})
    return len(rows)


def merge_outputs(auto_dir: Path, new_rows: list[dict],
                  cam_sources: list[str], durations: dict) -> None:
    csv_path = auto_dir / "scene_scores_allcam.csv"
    existing: list[dict] = []
    cols = list(CSV_COLS)
    if csv_path.exists():
        with open(csv_path) as fh:
            rd = csv.DictReader(fh)
            cols = rd.fieldnames or cols
            new_scenes = {r["scene"] for r in new_rows}
            existing = [r for r in rd if r.get("scene") not in new_scenes]
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in existing + new_rows:
            w.writerow({c: r.get(c, "") for c in cols})

    cs_path = auto_dir / "camera_sources.csv"
    seen: set[str] = set()
    lines: list[tuple[str, str]] = []
    if cs_path.exists():
        with open(cs_path) as fh:
            for row in csv.DictReader(fh):
                if row.get("source"):
                    lines.append((row["source"], row.get("camera", "")))
                    seen.add(row["source"])
    for s in cam_sources:
        if s not in seen:
            lines.append((s, "360"))
    with open(cs_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "camera"])
        w.writerows(lines)

    dc_path = auto_dir / "duration_cache.json"
    dc = {}
    if dc_path.exists():
        try:
            dc = json.loads(dc_path.read_text())
        except Exception:
            dc = {}
    dc.update(durations)
    dc_path.write_text(json.dumps(dc, indent=1))


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--yaws", type=int, default=8)
    ap.add_argument("--clip-dur", type=float, default=6.0)
    ap.add_argument("--min-gap", type=float, default=30.0)
    ap.add_argument("--per-file", type=int, default=8)
    ap.add_argument("--out-size", default="1920x1080")
    ap.add_argument("--dir360", default="360",
                    help="subdir of work_dir with the .insv recordings")
    ap.add_argument("--no-track", dest="track", action="store_false",
                    default=True,
                    help="disable object-lock tracking (fixed yaw per clip)")
    a = ap.parse_args()

    work_dir = Path(a.work_dir).resolve()
    dir360 = work_dir / a.dir360
    if not dir360.is_dir():
        sys.exit(f"no {a.dir360}/ directory in {work_dir}")
    out_w, out_h = (int(x) for x in a.out_size.lower().split("x"))

    cp = load_cfg(work_dir)
    sdk = sdk_binary(cp)
    if sdk is None:
        sys.exit("Insta360 MediaSDK not configured — set [paths] "
                 "insta360_mediasdk in config.ini (see README, 360 support)")
    ffmpeg = os.path.expanduser(cp.get("paths", "ffmpeg", fallback="ffmpeg"))
    ffprobe = str(Path(ffmpeg).parent / "ffprobe") if "/" in ffmpeg else "ffprobe"
    pos, neg, neg_w, phash = prompts(cp)
    if not pos:
        sys.exit("no [clip_prompts] positive in config.ini")

    pairs = discover(dir360)
    if not pairs:
        sys.exit(f"no VID_*_00_*.insv recordings in {dir360}")
    log(f"360 scan: {len(pairs)} recording pair(s) in {dir360}")

    auto_dir = work_dir / "_autoframe"
    i360_dir = auto_dir / "insta360"
    autocut = auto_dir / "autocut"
    autocut.mkdir(parents=True, exist_ok=True)

    scorer = None
    yaws = [round(i * 360 / a.yaws) for i in range(a.yaws)]
    new_rows: list[dict] = []
    cam_sources: list[str] = []
    durations: dict = {}
    n_clips = 0

    for pi, pair in enumerate(pairs):
        stem = pair["stem"]
        log(f"[{pi + 1:2d}/{len(pairs)}] {stem}")
        if pair["lrv"] is None:
            log("  ! no LRV preview — skipped (scan needs the LRV)")
            continue
        fps, dur, ct = probe_fps_dur_ct(ffprobe, pair["vid00"])
        if dur < a.clip_dur:
            log(f"  · too short ({dur:.1f}s) — skipped")
            continue

        # A: scan (lazy model load — cache hits need no GPU)
        cache = i360_dir / "raw" / f"{pair['lrv'].stem}.json"
        need_scan = True
        if cache.exists():
            try:
                d = json.loads(cache.read_text())
                need_scan = not (d.get("interval") == a.interval
                                 and d.get("yaws") == yaws
                                 and d.get("prompts_hash") == phash)
            except Exception:
                pass
        if need_scan and scorer is None:
            log("  loading SigLIP2 …")
            scorer = make_scorer(pos, neg, neg_w)
        rows = scan_lrv(pair["lrv"], cache, a.interval, yaws, phash,
                        ffmpeg, scorer or (lambda items: []))
        if not rows:
            continue

        # B: select
        wins = select_windows(rows, dur, a.clip_dur, a.min_gap, a.per_file)
        if not wins:
            log("  · no windows selected")
            continue
        log(f"  windows: {len(wins)}  "
            + " ".join(f"{w['ts']:.0f}s/y{w['yaw']}" for w in wins))

        # done-marker: skip already-assembled window sets
        wsig = hashlib.sha256(json.dumps(
            [wins, a.clip_dur, a.out_size, a.track], sort_keys=True).encode()
        ).hexdigest()[:16]
        marker = i360_dir / "done" / f"{stem}.{wsig}"
        if marker.exists():
            log("  · cached (clips already assembled)")
            for wi, w in enumerate(wins):
                scene = f"{stem}-clip-{wi + 1:03d}"
                if (autocut / f"{scene}.mp4").exists():
                    new_rows.append(_row(scene, w))
                    durations[scene] = a.clip_dur
            cam_sources.append(stem)
            continue

        # C: stitch
        stitch_dir = i360_dir / "tmp" / stem
        if stitch_dir.exists():
            shutil.rmtree(stitch_dir)
        log(f"  stitching {len(wins)} window(s) via MediaSDK …")
        idx_map = stitch_windows(sdk, pair, wins, fps, a.clip_dur, stitch_dir)
        if idx_map is None:
            shutil.rmtree(stitch_dir, ignore_errors=True)
            continue

        # D: assemble (D2: object-lock trajectory when it tracks reliably)
        ok = 0
        for wi, w in enumerate(wins):
            scene = f"{stem}-clip-{wi + 1:03d}"
            clip = autocut / f"{scene}.mp4"
            made = False
            if a.track:
                traj = track_window(stitch_dir, idx_map[wi], fps,
                                    float(w["yaw"]))
                if traj is not None:
                    made = assemble_tracked(
                        ffmpeg, stitch_dir, idx_map[wi], fps, traj, clip,
                        pair["vid00"], w["ts"], a.clip_dur, ct, out_w, out_h)
                    if made:
                        log(f"    clip {wi + 1}: tracked "
                            f"(yaw {traj[0][0]:.0f}→{traj[-1][0]:.0f}°, "
                            f"pitch {traj[0][1]:+.0f}→{traj[-1][1]:+.0f}°)")
            if not made:
                made = assemble_clip(ffmpeg, stitch_dir, idx_map[wi], fps,
                                     w["yaw"], clip, pair["vid00"], w["ts"],
                                     a.clip_dur, ct, out_w, out_h)
            if made:
                extract_pool_frame(ffmpeg, clip, auto_dir / "frames", scene)
                new_rows.append(_row(scene, w))
                durations[scene] = a.clip_dur
                ok += 1
        shutil.rmtree(stitch_dir, ignore_errors=True)
        if ok:
            cam_sources.append(stem)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            n_clips += ok
        log(f"  clips: {ok}/{len(wins)}")

    if new_rows:
        merge_outputs(auto_dir, new_rows, cam_sources, durations)
        save_manifest(auto_dir, new_rows, cam_sources, durations)
        log(f"Done: {n_clips} new clip(s), {len(new_rows)} pool entries "
            f"({len(cam_sources)} source file(s), camera=360)")
    else:
        log("Done: nothing new")
    return 0


def _row(scene: str, w: dict) -> dict:
    return {"scene": scene, "score": w["score"], "pos_score": "",
            "neg_score": "", "aesthetic_score": "", "offset_sec": w["ts"],
            "avg_brightness": ""}


if __name__ == "__main__":
    sys.exit(main())
