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
import threading
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
    # User (2026-10-01): dropped "camera pointing at the rider, road barely
    # visible" — unlike the two above, it didn't require blocking/obscuring,
    # just the rider being IN frame + road not visible, which also penalized
    # good "rider + scenery" shots with no road in view (e.g. a side/rear
    # angle showing mountains, not the road). Merely being visible is fine;
    # only blocking/filling-the-frame framing should be penalized.
    #
    # A rock wall/cliff filling the frame is worse than the helmet-blocking
    # case above, not just an equal nuisance — "extreme close-up ... filling
    # the entire frame" on purpose, so this doesn't penalize a genuinely
    # scenic rock/mountain shot with sky in frame.
    "extreme close-up of a rock wall or cliff face filling the entire frame, no sky or horizon visible",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
_FORCE_CPU_ACCEL = threading.Event()   # set after the first auto→cpu fallback

# Hub policy (src/hf_policy.py) is applied in main(), NOT at import time:
# pipeline.py imports this module merely for the ownership manifest, and an
# import-time apply() consumed the online window / pinned offline env in the
# long-lived webapp process (audit finding).
import hf_policy


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
    log(f"    extracting {len(yaws)} yaw views every {interval:g}s …")
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
    from device_policy import select_torch_device
    dev = select_torch_device("insta360_scan")
    try:
        model, _, preprocess = open_clip.create_model_and_transforms(
            MODEL, pretrained=PRETRAINED)
        tok = open_clip.get_tokenizer(MODEL)
    except Exception as _e:
        # Re-audit #2: the tokenizer call was OUTSIDE this try — weights
        # cached but tokenizer files missing crashed the scanner with no
        # retry, same class of gap as yesterday's mood_score crash.
        hf_policy.retry_online_or_return(f"{MODEL}/{PRETRAINED}: {_e}")
        raise SystemExit(
            f"model load failed while online ({_e})") from _e
    model = model.to(dev).eval()
    import torch as _t
    with _t.no_grad():
        pf = model.encode_text(tok(pos).to(dev)); pf /= pf.norm(dim=-1, keepdim=True)
        nf = model.encode_text(tok(neg).to(dev)); nf /= nf.norm(dim=-1, keepdim=True)

    def scorer(items):
        rows = []
        for i in range(0, len(items), BATCH):
            if i and (i // BATCH) % 20 == 0:
                log(f"    scored {i}/{len(items)} views")
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

# 360's value over the helmet/handlebar cams is precisely the angles they
# CAN'T cover. User policy (2026-10-01): don't just discount forward
# (direction of travel) — ignore it exactly like the helmet cam already
# does. Hard-exclude the whole front hemisphere, keep only right/left/back.
# Replaces an earlier soft multiplicative tie-break (FORWARD_BIAS) that
# could still let a high-scoring forward shot win; that tie-break is moot
# now anyway since every surviving yaw after this filter is already ≥90°
# from forward.
#
# Raw yaw=0 is the camera BODY's own reference axis (the dual-fisheye
# seam), fixed by the hardware — it only happens to equal "forward" if the
# camera was physically mounted with that axis pointed along the direction
# of travel. A different mount angle needs a calibration offset, same idea
# as the existing LRV_ROLL constant for a sideways-mounted roll — except
# yaw varies per rig/trip, so it's a config value, not a constant:
# [insta360] forward_yaw_deg in config.ini (see load_cfg/main — read once
# per run, same layered global→project override as auto_scan). One-time
# calibration per physical mount, not per file/clip.
FORWARD_EXCLUDE_HALF_DEG = 90.0


def _is_forward(yaw: float, forward_yaw: float = 0.0) -> bool:
    """True if yaw is within FORWARD_EXCLUDE_HALF_DEG of the forward
    reference (forward_yaw — the raw yaw value that is dead-ahead for
    THIS camera's physical mount, default 0°)."""
    d = abs(((float(yaw) - forward_yaw + 180) % 360) - 180)  # signed distance, in [0,180]
    return d < FORWARD_EXCLUDE_HALF_DEG


def select_windows(rows, dur_limit: float, clip_dur: float,
                   min_gap: float, per_file: int,
                   forward_yaw: float = 0.0) -> list[dict]:
    """Greedy peaks: [{ts, yaw, score}], ts = window START, clamped to file.
    Forward-hemisphere candidates are dropped before ranking (see
    FORWARD_EXCLUDE_HALF_DEG / forward_yaw) — right/left/back only, same
    coverage the helmet cam doesn't already give us."""
    if dur_limit < clip_dur:
        return []
    total = len(rows)
    rows = [r for r in rows if not _is_forward(r[1], forward_yaw)]
    dropped = total - len(rows)
    if dropped:
        log(f"    {dropped}/{total} candidate view(s) excluded (forward hemisphere)")
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
                   clip_dur: float, out_dir: Path,
                   stitch_size: str = STITCH_SIZE,
                   stitch_type: str = "optflow") -> dict | None:
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
    _logf = out_dir / "sdk.log"   # inside the per-invocation dir
    # Rendering accel: try auto first; broken GPU paths either segfault or
    # render black frames — the SDK's documented remedy is
    # -image_processing_accel cpu. Once one file needed the cpu retry, the
    # rest of the run skips the doomed auto attempt (same environment).
    _accels = ("cpu",) if _FORCE_CPU_ACCEL.is_set() else ("auto", "cpu")
    for _accel in _accels:
        cmd = [str(sdk),
               "-inputs", str(pair["vid00"]), str(pair["vid10"]),
               "-image_sequence_dir", str(out_dir),
               "-image_type", "jpg",
               "-export_frame_index", "-".join(map(str, all_idx)),
               "-stitch_type", stitch_type,
               "-output_size", stitch_size,
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
        produced_files = sorted(out_dir.glob("*.jpg"))
        produced = len(produced_files)
        # The SDK's error callback prints lines starting with "error:"; a
        # plain substring match also caught benign "glGetError:" GL spam.
        _err_line = any(l.strip().lower().startswith("error:")
                        for l in out.splitlines())
        # Headless-GL trap: a broken GPU path renders BLACK frames while
        # reporting success — sample a few frames and reject an all-black
        # export so the cpu retry kicks in.
        _black = False
        if produced:
            try:
                from PIL import Image
                import numpy as _np
                _samples = [produced_files[0],
                            produced_files[produced // 2],
                            produced_files[-1]]
                _black = all(
                    _np.asarray(Image.open(p).convert("L")).mean() < 2.0
                    for p in _samples)
            except Exception:
                _black = False
        if _black:
            log(f"  ! stitch of {pair['stem']} produced black frames "
                f"(accel={_accel}) — broken GPU render path")
        if (r.returncode == 0 and produced >= len(all_idx) * 0.98
                and not _err_line and not _black):
            if _accel == "cpu":
                log("    (image processing on CPU — no usable GPU render here)")
                _FORCE_CPU_ACCEL.set()
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
    # Runaway guard: a tracker sliding off the subject reports "ok" while
    # panning 100°+ across a 6s window (seen in production logs). A genuine
    # landmark pass stays well under this; reject and fall back to fixed yaw.
    _yaws_all = [t[0] for t in traj]
    if max(_yaws_all) - min(_yaws_all) > 90.0:
        return None

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
    # stderr to a FILE: an undrained pipe can fill while we write stdin and
    # deadlock both processes — with the NVENC semaphore held (audit TS#4).
    _errf = out.with_suffix(".enc.log")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=open(_errf, "w"))
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
        err = ""
        try:
            err = _errf.read_text(errors="replace")[-200:]
        except OSError:
            pass
        log(f"  ! tracked assemble failed for {out.name}: {err}")
        return False
    _errf.unlink(missing_ok=True)
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
                       scene: str) -> bool:
    """Single-frame pool thumbnail. Production (2026-10-01): 32/120 clips
    silently had no thumbnail (grey square in the gallery) after a run under
    heavy GPU/CPU contention (concurrent Vulkan-fallback stitching) — this
    had no returncode check, no retry, no log line, so the failure was
    invisible. Now retried once and reported; still non-fatal (the clip
    itself is fine, only its pool preview is missing)."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    out = frames_dir / f"{scene}.jpg"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", "0.5", "-i", str(clip), "-frames:v", "1",
           "-vf", "scale=640:-2", "-q:v", "4", str(out)]
    for _attempt in range(2):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            # Re-audit #1(b): only TimeoutExpired was caught — a missing
            # ffmpeg binary or any OSError escaped uncaught, past this
            # function's "never fatal" contract, up into _process_pair's
            # blanket except — dropping the WHOLE pair's 8 good clips over
            # one thumbnail. Must never raise.
            continue
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return True
    log(f"  ! pool thumbnail failed for {scene} (clip itself is fine)")
    return False


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
    # Sources covered by THIS run replace their scene set wholesale —
    # unioning kept clips 005-008 alive forever after --per-file shrank
    # (audit #12).
    _run_stems = set(cam_sources)
    by_scene = {r["scene"]: r for r in old.get("rows", [])
                if not any(r["scene"].startswith(f"{s}-clip-")
                           for s in _run_stems)}
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
    _seed = csv_path if csv_path.exists() else auto_dir / "scene_scores.csv"
    # Seeding from the main CSV matters: creating allcam from ONLY the 360
    # rows made every allcam-preferring consumer lose the normal footage
    # (audit #17).
    if _seed.exists():
        with open(_seed) as fh:
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
    ap.add_argument("--jobs", type=int, default=1,
                    help="parallel MediaSDK stitch jobs across files "
                         "(measure RAM/VRAM before going above 1)")
    ap.add_argument("--stitch-size", default=STITCH_SIZE,
                    help="equirect stitch resolution WxH (3840x1920 default; "
                         "4800x2400 / 5760x2880 for sharper reframes)")
    ap.add_argument("--stitch-type", default="optflow",
                    choices=["optflow", "dynamicstitch"],
                    help="SDK stitch algorithm (dynamicstitch = faster)")
    a = ap.parse_args()
    if not re.fullmatch(r"\d{3,5}x\d{3,5}", a.stitch_size):
        sys.exit(f"bad --stitch-size: {a.stitch_size}")
    a.jobs = max(1, min(8, a.jobs))
    hf_policy.apply()   # before the lazy torch/open_clip imports

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

    _lock = acquire_lock(auto_dir)
    if _lock is None:
        sys.exit("Another 360 scan is already running for this project")
    try:
        return _main_locked(a, work_dir, dir360, out_w, out_h, cp, sdk,
                            ffmpeg, ffprobe, pos, neg, neg_w, phash,
                            pairs, auto_dir, i360_dir, autocut)
    finally:
        release_lock(_lock)


def _main_locked(a, work_dir, dir360, out_w, out_h, cp, sdk, ffmpeg, ffprobe,
                 pos, neg, neg_w, phash, pairs, auto_dir, i360_dir, autocut) -> int:
    # Reclaim tmp dirs from crashed prior runs (re-audit Medium #4): only
    # safe now that the lock proves no OTHER process is mid-run.
    _tmp_root = i360_dir / "tmp"
    if _tmp_root.is_dir():
        for _leftover in _tmp_root.iterdir():
            if _leftover.is_dir():
                shutil.rmtree(_leftover, ignore_errors=True)

    yaws = [round(i * 360 / a.yaws) for i in range(a.yaws)]
    forward_yaw = cp.getfloat("insta360", "forward_yaw_deg", fallback=0.0) % 360
    if forward_yaw:
        log(f"  forward reference: raw yaw {forward_yaw:g}° "
            f"([insta360] forward_yaw_deg)")

    # ── Pass 1: scan + select every pair (holds the CUDA model) ─────────────
    scorer = None
    plans: list[dict] = []
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
        wins = select_windows(rows, dur, a.clip_dur, a.min_gap, a.per_file,
                              forward_yaw)
        if not wins:
            log("  · no windows selected")
            continue
        log(f"  windows: {len(wins)}  "
            + " ".join(f"{w['ts']:.0f}s/y{w['yaw']}" for w in wins))
        wsig = hashlib.sha256(json.dumps(
            [wins, a.clip_dur, a.out_size, a.track,
             a.stitch_size, a.stitch_type], sort_keys=True).encode()
        ).hexdigest()[:16]
        plans.append({"pair": pair, "wins": wins, "fps": fps, "ct": ct,
                      "wsig": wsig})

    # Free the SigLIP model BEFORE stitching — it would otherwise hold VRAM
    # while the SDK (Vulkan) and NVENC need the same GPU.
    if scorer is not None:
        scorer = None
        try:
            import gc
            import torch
            gc.collect()
            torch.cuda.empty_cache()
            log("Scan model released")
        except Exception:
            pass

    # ── Pass 2: stitch + assemble (optionally N pairs in parallel) ──────────
    # NVENC sessions are the scarce resource (consumer GPUs allow ~5):
    # bound them separately from SDK job count.
    enc_sem = threading.Semaphore(2)
    ctx = {"a": a, "ffmpeg": ffmpeg, "sdk": sdk, "auto_dir": auto_dir,
           "autocut": autocut, "i360_dir": i360_dir, "enc_sem": enc_sem,
           "out_w": out_w, "out_h": out_h}
    if a.jobs > 1 and len(plans) > 1:
        log(f"Stitching {len(plans)} file(s) with {a.jobs} parallel job(s)")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            results = list(ex.map(lambda p: _process_pair(p, ctx), plans))
    else:
        results = [_process_pair(p, ctx) for p in plans]

    new_rows: list[dict] = []
    cam_sources: list[str] = []
    durations: dict = {}
    n_clips = 0
    for rows_r, stem_r, durs_r, n_r in results:
        new_rows += rows_r
        durations.update(durs_r)
        n_clips += n_r
        if stem_r:
            cam_sources.append(stem_r)

    if new_rows:
        merge_outputs(auto_dir, new_rows, cam_sources, durations)
        save_manifest(auto_dir, new_rows, cam_sources, durations)
        log(f"Done: {n_clips} new clip(s), {len(new_rows)} pool entries "
            f"({len(cam_sources)} source file(s), camera=360)")
    else:
        log("Done: nothing new")
    return 0


def _process_pair(plan: dict, ctx: dict):
    """Phases C+D for one pair (thread-safe: writes only its own stitch dir,
    marker and clip files; shared CSV/manifest merging stays in main).
    Returns (rows, source_stem_or_None, durations, n_new_clips).
    Never raises: one worker's crash must not abort the whole ex.map — the
    other workers' clips would then miss the manifest and later be deleted
    by pipeline cleanup (audit TS#1)."""
    try:
        return _process_pair_inner(plan, ctx)
    except Exception as _e:
        log(f"[{plan['pair']['stem']}] worker failed: {_e}")
        return [], None, {}, 0


def _drop_stale_markers(marker_dir: Path, stem: str, keep: Path) -> None:
    """Remove every done-marker for `stem` other than `keep`. Must run as
    soon as we're committed to overwriting this stem's clip files — not
    only after the new attempt fully succeeds — or a stale marker can go on
    validating files a failed newer attempt partially overwrote
    (2026-10-06 audit)."""
    marker_dir.mkdir(parents=True, exist_ok=True)
    for _old in marker_dir.glob(f"{stem}.*"):
        if _old != keep:
            _old.unlink(missing_ok=True)


def _process_pair_inner(plan: dict, ctx: dict):
    a, ffmpeg, sdk = ctx["a"], ctx["ffmpeg"], ctx["sdk"]
    auto_dir, autocut, i360_dir = ctx["auto_dir"], ctx["autocut"], ctx["i360_dir"]
    enc_sem, out_w, out_h = ctx["enc_sem"], ctx["out_w"], ctx["out_h"]
    pair, wins, fps, ct = plan["pair"], plan["wins"], plan["fps"], plan["ct"]
    stem = pair["stem"]
    marker = i360_dir / "done" / f"{stem}.{plan['wsig']}"

    rows: list[dict] = []
    durs: dict = {}
    # Marker hit counts only when EVERY clip of the set exists — a partial
    # set (corrupt-clip pruning, manual deletion) must regenerate, not be
    # silently accepted with holes (audit #4).
    if marker.exists():
        if all((autocut / f"{stem}-clip-{wi + 1:03d}.mp4").exists()
               for wi in range(len(wins))):
            log(f"[{stem}] cached (clips already assembled)")
            _repaired = 0
            for wi, w in enumerate(wins):
                scene = f"{stem}-clip-{wi + 1:03d}"
                # Re-audit #2: a thumbnail-extraction failure was discarded
                # on the FRESH-stitch path too, but there it could at least
                # self-heal on the next analyze — the cached-hit path only
                # ever checked clip existence, so a missing preview (seen
                # in production: 32/120) stayed a permanent grey square
                # until someone noticed and repaired it by hand. Repair here.
                if not (auto_dir / "frames" / f"{scene}.jpg").exists():
                    if extract_pool_frame(ffmpeg, autocut / f"{scene}.mp4",
                                          auto_dir / "frames", scene):
                        _repaired += 1
                rows.append(_row(scene, w))
                durs[scene] = a.clip_dur
            if _repaired:
                log(f"[{stem}]   repaired {_repaired} missing thumbnail(s)")
            return rows, stem, durs, 0
        log(f"[{stem}] cache marker present but clips missing — regenerating")
        marker.unlink(missing_ok=True)

    # Unique per invocation: overlapping scans of one project must not
    # delete each other's frames or interleave SDK logs (audit TS#2).
    stitch_dir = i360_dir / "tmp" / f"{stem}.{os.getpid()}"
    if stitch_dir.exists():
        shutil.rmtree(stitch_dir)
    log(f"[{stem}] stitching {len(wins)} window(s) via MediaSDK …")
    idx_map = stitch_windows(sdk, pair, wins, fps, a.clip_dur, stitch_dir,
                             a.stitch_size, a.stitch_type)
    if idx_map is None:
        # Nothing on disk was touched — an old marker, if any, is still
        # accurate. Invalidating it here (rather than only below) would
        # punish a transient stitch failure for no reason.
        shutil.rmtree(stitch_dir, ignore_errors=True)
        return [], None, {}, 0

    # From here on we're about to overwrite autocut/{stem}-clip-NNN.mp4.
    _drop_stale_markers(marker.parent, stem, marker)

    ok = 0
    for wi, w in enumerate(wins):
        scene = f"{stem}-clip-{wi + 1:03d}"
        clip = autocut / f"{scene}.mp4"
        made = False
        if a.track:
            traj = track_window(stitch_dir, idx_map[wi], fps, float(w["yaw"]))
            if traj is not None:
                with enc_sem:
                    made = assemble_tracked(
                        ffmpeg, stitch_dir, idx_map[wi], fps, traj, clip,
                        pair["vid00"], w["ts"], a.clip_dur, ct, out_w, out_h)
                if made:
                    log(f"[{stem}] clip {wi + 1}: tracked "
                        f"(yaw {traj[0][0]:.0f}→{traj[-1][0]:.0f}°, "
                        f"pitch {traj[0][1]:+.0f}→{traj[-1][1]:+.0f}°)")
        if not made:
            with enc_sem:
                made = assemble_clip(ffmpeg, stitch_dir, idx_map[wi], fps,
                                     w["yaw"], clip, pair["vid00"], w["ts"],
                                     a.clip_dur, ct, out_w, out_h)
        if made:
            extract_pool_frame(ffmpeg, clip, auto_dir / "frames", scene)
            rows.append(_row(scene, w))
            durs[scene] = a.clip_dur
            ok += 1
    shutil.rmtree(stitch_dir, ignore_errors=True)
    # All-or-nothing: a partial window set must NOT be marked done, or the
    # cache would forever hide the failed clips. Older markers for this stem
    # were already dropped upfront, before any clip file was touched (see
    # above) — nothing more to clean up here, just record this attempt.
    if ok == len(wins):
        marker.touch()
    log(f"[{stem}] clips: {ok}/{len(wins)}")
    return rows, (stem if ok else None), durs, ok


def acquire_lock(auto_dir: Path):
    """Cross-process lock keyed by the resolved project dir (re-audit High
    #2): the manual endpoint's job-based guard never saw the automatic
    analyze-time scan, since that one runs in-process from pipeline.py with
    no Job object. A lock file works for both entry points and the CLI.
    Returns the lock Path on success, None if another live process holds it.

    PID-reuse note: execve (used by hf_policy.retry_online_or_return for an
    offline-cache-miss retry) replaces the program image but KEEPS the PID —
    main() runs from scratch and calls this again. Without recognizing our
    own PID as already-held, the re-exec'd process would read its own lock
    file and refuse to proceed, permanently defeating the retry (re-audit
    High #1, reproduced in production). Self-PID is therefore always a hit.
    """
    lock = auto_dir / "insta360" / ".scan.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                _pid = int(lock.read_text().strip())
            except (ValueError, OSError):
                return None   # unreadable — can't tell, don't guess
            if _pid == os.getpid():
                return lock   # our own lock, surviving an execve re-exec
            try:
                os.kill(_pid, 0)
                return None   # a live process holds it
            except ProcessLookupError:
                pass   # looks dead — fall through to a verified reclaim
            except (PermissionError, OSError):
                return None   # can't tell — treat as held, don't guess
            # TOCTOU guard (re-audit Medium #3): re-read right before
            # unlinking. If the PID changed since the check above, another
            # process already reclaimed or renewed it — don't blindly
            # unlink what might now be someone else's live lock. This
            # narrows, but does not fully eliminate, the race; a kernel
            # advisory lock (fcntl.flock) would close it completely and is
            # a reasonable future upgrade if this ever bites in practice.
            try:
                _pid2 = int(lock.read_text().strip())
            except (ValueError, OSError):
                return None
            if _pid2 != _pid:
                return None
            lock.unlink(missing_ok=True)
            continue
    return None


def release_lock(lock: Path | None) -> None:
    if lock is not None:
        lock.unlink(missing_ok=True)


def _row(scene: str, w: dict) -> dict:
    return {"scene": scene, "score": w["score"], "pos_score": "",
            "neg_score": "", "aesthetic_score": "", "offset_sec": w["ts"],
            "avg_brightness": ""}


if __name__ == "__main__":
    sys.exit(main())
