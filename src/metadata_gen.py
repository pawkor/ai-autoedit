#!/usr/bin/env python3
"""
metadata_gen.py — AI metadata generator for highlight videos.

Generates:
  - YouTube Chapters (timestamps grouped every 20-30s, named by top CLIP zero-shot label)
  - "Na tym filmie zobaczysz:" detected scene types (top 5)
  - Ready-to-paste description block

Uses the existing ViT-L-14 CLIP model — no extra models, no external API.

Usage:
    python metadata_gen.py <work_dir> [--chapter-window 25] [--top-n 5]
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ── Zero-shot label vocabulary ────────────────────────────────────────────────

LABELS: list[str] = [
    # Road & riding
    "motorcycle riding on winding mountain road",
    "motorcycle on tight hairpin curve",
    "motorcycle on straight open highway",
    "riding through tunnel",
    "motorcycle on narrow mountain pass",
    "riding in group convoy",
    "motorcycle parked at scenic viewpoint",
    "slow traffic in village or town",
    "gravel or unpaved mountain road",
    # Landscape
    "mountain peak and rocky terrain",
    "dense forest road",
    "alpine meadow and green hills",
    "coastal road above sea",
    "river valley and gorge",
    "arid rocky landscape",
    "vineyard and farmland scenery",
    "lake or reservoir view",
    "waterfall near road",
    "snowy mountain ridge",
    "dramatic canyon road",
    # Infrastructure & stops
    "small village or mountain town",
    "bridge over canyon or river",
    "fuel station stop",
    "roadside restaurant or cafe",
    "mountain checkpoint or toll gate",
    "ancient ruins or historic site",
    "road construction zone",
    "switchback road from above",
    # Conditions & light
    "clear sunny day riding",
    "overcast or cloudy weather",
    "mountain fog or low clouds",
    "wet road after rain",
    "golden hour sunset riding",
    "early morning misty ride",
    "shadows and light through trees",
    # Region-specific
    "balkans mountain scenery",
    "greek coastal road",
    "romanian mountain road",
    "adriatic coastline",
    "border crossing checkpoint",
    # Camera angles
    "helmet camera following shot",
    "rear camera following view",
    "dramatic sky with road into distance",
]

# Human-readable chapter name (title case, shortened for YT chapters ≤ 100 chars)
_LABEL_TO_CHAPTER: dict[str, str] = {
    "motorcycle riding on winding mountain road": "Winding Mountain Road",
    "motorcycle on tight hairpin curve": "Hairpin Curves",
    "motorcycle on straight open highway": "Open Highway",
    "riding through tunnel": "Tunnel",
    "motorcycle on narrow mountain pass": "Mountain Pass",
    "riding in group convoy": "Group Ride",
    "motorcycle parked at scenic viewpoint": "Scenic Viewpoint",
    "slow traffic in village or town": "Village",
    "gravel or unpaved mountain road": "Gravel Road",
    "mountain peak and rocky terrain": "Rocky Terrain",
    "dense forest road": "Forest Road",
    "alpine meadow and green hills": "Alpine Meadow",
    "coastal road above sea": "Coastal Road",
    "river valley and gorge": "River Gorge",
    "arid rocky landscape": "Arid Landscape",
    "vineyard and farmland scenery": "Farmland",
    "lake or reservoir view": "Lake View",
    "waterfall near road": "Waterfall",
    "snowy mountain ridge": "Snow & Ice",
    "dramatic canyon road": "Canyon Road",
    "small village or mountain town": "Mountain Village",
    "bridge over canyon or river": "Bridge",
    "fuel station stop": "Fuel Stop",
    "roadside restaurant or cafe": "Rest Stop",
    "mountain checkpoint or toll gate": "Checkpoint",
    "ancient ruins or historic site": "Historic Site",
    "road construction zone": "Road Works",
    "switchback road from above": "Switchbacks",
    "clear sunny day riding": "Sunny Riding",
    "overcast or cloudy weather": "Cloudy Ride",
    "mountain fog or low clouds": "Mountain Fog",
    "wet road after rain": "Wet Road",
    "golden hour sunset riding": "Golden Hour",
    "early morning misty ride": "Morning Mist",
    "shadows and light through trees": "Forest Light",
    "balkans mountain scenery": "Balkans Mountains",
    "greek coastal road": "Greek Coast",
    "romanian mountain road": "Romanian Mountains",
    "adriatic coastline": "Adriatic Coast",
    "border crossing checkpoint": "Border Crossing",
    "helmet camera following shot": "Helmet Cam",
    "rear camera following view": "Rear View",
    "dramatic sky with road into distance": "Open Road",
}


# ── Duration helpers ──────────────────────────────────────────────────────────

def _ffprobe_duration(path: Path, ffprobe: str = "ffprobe") -> float:
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _fmt_timestamp(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ── Parse selected_scenes.txt (ffconcat format) ───────────────────────────────

def _parse_selected(path: Path) -> list[Path]:
    """Return ordered list of clip paths from ffconcat file."""
    clips: list[Path] = []
    for line in path.read_text().splitlines():
        m = re.match(r"^file\s+'(.+)'$", line.strip())
        if m:
            clips.append(Path(m.group(1)))
    return clips


# ── CLIP zero-shot ────────────────────────────────────────────────────────────

def _run_zero_shot(
    frame_paths: list[Path],
    device: str = "cuda",
) -> list[int]:
    """
    For each frame path return the index of the best matching label.
    Returns list of ints (label indices), same length as frame_paths.
    Skips missing frames (returns -1).
    """
    import torch
    import open_clip
    from PIL import Image

    print(f"  Zero-shot: {len(frame_paths)} frames × {len(LABELS)} labels …", flush=True)

    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-L-14", pretrained="openai"
    )
    model = model.to(device).eval()
    tokenizer = open_clip.get_tokenizer("ViT-L-14")

    # Encode labels once (batch)
    with torch.no_grad():
        label_tokens = tokenizer(LABELS).to(device)
        label_feats = model.encode_text(label_tokens)
        label_feats /= label_feats.norm(dim=-1, keepdim=True)

    results: list[int] = []
    batch_size = 32

    valid_paths = [(i, p) for i, p in enumerate(frame_paths) if p.exists()]
    best_indices = [-1] * len(frame_paths)

    for start in range(0, len(valid_paths), batch_size):
        batch = valid_paths[start:start + batch_size]
        imgs = []
        idxs = []
        for orig_i, fp in batch:
            try:
                imgs.append(preprocess(Image.open(fp).convert("RGB")))
                idxs.append(orig_i)
            except Exception:
                pass
        if not imgs:
            continue
        tensor = torch.stack(imgs).to(device)
        with torch.no_grad():
            img_feats = model.encode_image(tensor)
            img_feats /= img_feats.norm(dim=-1, keepdim=True)
            sims = (img_feats @ label_feats.T)          # (B, L)
            top = sims.argmax(dim=1).cpu().tolist()
        for orig_i, lbl_i in zip(idxs, top):
            best_indices[orig_i] = lbl_i

    del model
    if device == "cuda":
        torch.cuda.empty_cache()

    print(f"  Zero-shot done.")
    return best_indices


# ── Core analysis ─────────────────────────────────────────────────────────────

def generate(
    work_dir: Path,
    chapter_window: float = 25.0,
    top_n: int = 5,
    ffprobe: str = "ffprobe",
) -> dict:
    """
    Returns:
        {
          "chapters": [{"time": "0:00", "seconds": 0, "label": "..."}, ...],
          "detected": ["Label A", "Label B", ...],   # top_n unique
          "description_block": "...",                 # ready to paste
        }
    """
    auto_dir = work_dir / "_autoframe"
    selected_txt = auto_dir / "selected_scenes.txt"
    scores_csv   = auto_dir / "scene_scores.csv"
    frames_dir   = auto_dir / "frames"

    if not selected_txt.exists():
        raise FileNotFoundError(f"selected_scenes.txt not found — run pipeline first")
    if not scores_csv.exists():
        raise FileNotFoundError(f"scene_scores.csv not found — run Analyze first")

    # 1. Load selected clips in order
    clip_paths = _parse_selected(selected_txt)
    if not clip_paths:
        raise ValueError("selected_scenes.txt has no clips")

    print(f"\n[metadata-gen] {len(clip_paths)} selected clips")

    # 2. Load CLIP scores (for weighting)
    scores: dict[str, float] = {}
    with open(scores_csv) as f:
        for row in csv.DictReader(f):
            try:
                scores[row["scene"]] = float(row["score"])
            except (KeyError, ValueError):
                pass

    # 3. Resolve frame paths + durations
    scene_names: list[str] = []
    scene_durs:  list[float] = []
    frame_paths: list[Path] = []

    for clip_path in clip_paths:
        stem = clip_path.stem
        # trimmed clips may have _tXXs suffix — strip to get base scene name
        scene = re.sub(r"_t\d+(\.\d+)?s$", "", stem)
        scene_names.append(scene)
        scene_durs.append(_ffprobe_duration(clip_path, ffprobe))

        # Best frame: prefer _f1 (50%), fallback _f0, then plain
        for suffix in (f"{scene}_f1.jpg", f"{scene}_f0.jpg", f"{scene}.jpg"):
            fp = frames_dir / suffix
            if fp.exists():
                frame_paths.append(fp)
                break
        else:
            frame_paths.append(frames_dir / f"{scene}_f1.jpg")  # missing → zero-shot skips

    print(f"  Total duration: {_fmt_timestamp(sum(scene_durs))}")

    # 4. CLIP zero-shot classification
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    label_indices = _run_zero_shot(frame_paths, device=device)

    # 5. Build per-scene records
    records = []
    t = 0.0
    for name, dur, lbl_i in zip(scene_names, scene_durs, label_indices):
        records.append({
            "scene":    name,
            "t_start":  t,
            "duration": dur,
            "score":    scores.get(name, 0.0),
            "label_i":  lbl_i,
            "label":    LABELS[lbl_i] if lbl_i >= 0 else "",
        })
        t += dur

    # 6. Group into chapter windows
    chapters: list[dict] = []
    window_records: list[dict] = []
    window_start = 0.0

    def _flush_window(recs: list[dict], t_start: float) -> None:
        if not recs:
            return
        # Pick label with highest average CLIP score among frames in this window
        label_scores: dict[int, list[float]] = {}
        for r in recs:
            if r["label_i"] >= 0:
                label_scores.setdefault(r["label_i"], []).append(r["score"])
        if not label_scores:
            return
        best_lbl_i = max(label_scores, key=lambda k: sum(label_scores[k]) / len(label_scores[k]))
        raw_label  = LABELS[best_lbl_i]
        ch_name    = _LABEL_TO_CHAPTER.get(raw_label, raw_label.title())
        # Don't add duplicate consecutive chapter names
        if chapters and chapters[-1]["label"] == ch_name:
            return
        chapters.append({
            "time":    _fmt_timestamp(t_start),
            "seconds": round(t_start),
            "label":   ch_name,
        })

    for r in records:
        window_records.append(r)
        window_dur = sum(x["duration"] for x in window_records)
        if window_dur >= chapter_window:
            _flush_window(window_records, window_start)
            window_start += window_dur
            window_records = []

    _flush_window(window_records, window_start)  # flush remainder

    # Ensure first chapter is always 0:00
    if chapters and chapters[0]["seconds"] != 0:
        chapters[0]["time"] = "0:00"
        chapters[0]["seconds"] = 0

    # 7. Top-N detected scene types (global, weighted by CLIP score)
    label_weight: dict[int, float] = {}
    for r in records:
        if r["label_i"] >= 0:
            label_weight[r["label_i"]] = label_weight.get(r["label_i"], 0.0) + r["score"]

    top_labels = sorted(label_weight, key=lambda k: label_weight[k], reverse=True)[:top_n]
    detected   = [_LABEL_TO_CHAPTER.get(LABELS[i], LABELS[i].title()) for i in top_labels]

    # 8. Format description block
    chapters_block = "\n".join(f"{c['time']} {c['label']}" for c in chapters)
    detected_block = ", ".join(detected)
    description_block = (
        f"Na tym filmie zobaczysz: {detected_block}\n\n"
        f"{chapters_block}"
    )

    print(f"  Chapters: {len(chapters)}  Detected: {detected}")

    return {
        "chapters":          chapters,
        "detected":          detected,
        "description_block": description_block,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, configparser

    ap = argparse.ArgumentParser(description="AI metadata generator")
    ap.add_argument("work_dir")
    ap.add_argument("--chapter-window", type=float, default=25.0,
                    help="Target seconds per chapter group (default 25)")
    ap.add_argument("--top-n", type=int, default=5,
                    help="Number of scene types in 'zobaczysz' block (default 5)")
    ap.add_argument("--out", default="", help="Write JSON result to file")
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).parent.parent / "config.ini")
    _ffprobe = cfg.get("paths", "ffprobe", fallback="ffprobe")

    result = generate(
        Path(args.work_dir),
        chapter_window=args.chapter_window,
        top_n=args.top_n,
        ffprobe=_ffprobe,
    )

    print("\n── Description block ──────────────────────────")
    print(result["description_block"])

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"\nSaved → {args.out}")
