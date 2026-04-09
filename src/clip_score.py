#!/usr/bin/env python3
import os
import sys
import logging
import warnings
import configparser

os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

import numpy as np
import torch
import open_clip

warnings.filterwarnings("ignore", message="QuickGELU mismatch", category=UserWarning)
import pandas as pd
from PIL import Image
from pathlib import Path
from tqdm import tqdm
from torch.utils.data import Dataset, DataLoader

_cfg = configparser.ConfigParser()
_script_dir = Path(__file__).resolve().parent
_cfg.read([_script_dir / "config.ini", Path.cwd() / "config.ini"])

FRAMES_DIR        = os.environ.get("FRAMES_DIR", "frames/")
OUTPUT_CSV        = os.environ.get("OUTPUT_CSV", "scene_scores.csv")
OUTPUT_CSV_ALLCAM = os.environ.get("OUTPUT_CSV_ALLCAM", "")   # all-cam scores for shorts
SCORE_ALL_CAMS    = os.environ.get("SCORE_ALL_CAMS", "") == "1"
EMBEDDINGS_FILE   = os.environ.get("EMBEDDINGS_FILE",
                        str(Path(OUTPUT_CSV).parent / "scene_embeddings.npz"))
CAM_SOURCES = os.environ.get("CAM_SOURCES", "")
AUDIO_CAM   = os.environ.get("AUDIO_CAM",   "")
TOP_PERCENT = _cfg.getint("clip_scoring",   "top_percent",   fallback=25)
NEG_WEIGHT  = _cfg.getfloat("clip_scoring", "neg_weight",    fallback=0.5)
BATCH_SIZE  = int(os.environ.get("CLIP_BATCH_SIZE",  _cfg.get("clip_scoring", "batch_size",  fallback="64")))
NUM_WORKERS = int(os.environ.get("CLIP_NUM_WORKERS", _cfg.get("clip_scoring", "num_workers", fallback=str(min(4, os.cpu_count() or 1)))))
if torch.cuda.is_available():
    DEVICE = "cuda"
elif torch.backends.mps.is_available():
    DEVICE = "mps"
else:
    DEVICE = "cpu"

def _parse_prompts(raw: str) -> list:
    return [line.strip() for line in raw.strip().splitlines() if line.strip()]

POSITIVE_PROMPTS = _parse_prompts(_cfg.get("clip_prompts", "positive", fallback=""))
NEGATIVE_PROMPTS = _parse_prompts(_cfg.get("clip_prompts", "negative", fallback=""))

if not POSITIVE_PROMPTS:
    print("ERROR: No positive CLIP prompts configured.\n"
          "Set [clip_prompts] positive in config.ini or generate prompts in Settings → Describe this ride.",
          file=sys.stderr)
    sys.exit(1)
if not NEGATIVE_PROMPTS:
    print("ERROR: No negative CLIP prompts configured.\n"
          "Set [clip_prompts] negative in config.ini.",
          file=sys.stderr)
    sys.exit(1)

print(f"Device: {DEVICE}")
if DEVICE == "cuda":
    print(f"GPU: {torch.cuda.get_device_name(0)}")
elif DEVICE == "mps":
    print("GPU: Apple Silicon (MPS)")
print(f"Batch size: {BATCH_SIZE}")

model, _, preprocess = open_clip.create_model_and_transforms('ViT-L-14', pretrained='openai')
tokenizer = open_clip.get_tokenizer('ViT-L-14')
model = model.to(DEVICE).eval()

with torch.no_grad():
    pos_tokens = tokenizer(POSITIVE_PROMPTS).to(DEVICE)
    neg_tokens = tokenizer(NEGATIVE_PROMPTS).to(DEVICE)
    pos_features = model.encode_text(pos_tokens)
    neg_features = model.encode_text(neg_tokens)
    pos_features /= pos_features.norm(dim=-1, keepdim=True)
    neg_features /= neg_features.norm(dim=-1, keepdim=True)

class FrameDataset(Dataset):
    def __init__(self, paths, transform):
        self.paths = paths
        self.transform = transform

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, idx):
        path = self.paths[idx]
        try:
            img = self.transform(Image.open(path).convert('RGB'))
            ok  = True
        except Exception:
            img = torch.zeros(3, 224, 224)
            ok  = False
        return img, str(path), ok


# Build set of back-cam source prefixes to skip
import re as _re
_back_sources: set[str] = set()
if CAM_SOURCES and os.path.exists(CAM_SOURCES) and AUDIO_CAM:
    import csv as _csv
    with open(CAM_SOURCES) as _f:
        for row in _csv.DictReader(_f):
            if row.get("camera") != AUDIO_CAM:
                _back_sources.add(row["source"])

def _is_main_cam(path: Path) -> bool:
    if not _back_sources:
        return True
    src = _re.sub(r'-scene-\d+$', '', path.stem)
    return src not in _back_sources

all_frames_list = sorted(Path(FRAMES_DIR).glob("*.jpg"))
main_cam_stems  = set(f.stem for f in all_frames_list if _is_main_cam(f))
frames = all_frames_list if SCORE_ALL_CAMS else [f for f in all_frames_list if _is_main_cam(f)]
skipped = len(all_frames_list) - len(main_cam_stems)
if skipped and not SCORE_ALL_CAMS:
    print(f"Skipping {skipped} back-cam frames (scoring main cam only)")
elif skipped and SCORE_ALL_CAMS:
    print(f"Scoring all cams: {len(main_cam_stems)} main + {skipped} back-cam (for shorts)")
print(f"Scoring {len(frames)} frames (batch={BATCH_SIZE}, workers={NUM_WORKERS})...")
dataset = FrameDataset(frames, preprocess)
loader  = DataLoader(dataset, batch_size=BATCH_SIZE, num_workers=NUM_WORKERS,
                     pin_memory=(DEVICE == "cuda"), prefetch_factor=2 if NUM_WORKERS > 0 else None)
results = []
scene_embs: dict[str, np.ndarray] = {}

for batch_imgs, batch_paths, batch_ok in tqdm(loader, total=len(loader)):
    valid_mask = batch_ok.bool()
    if not valid_mask.any():
        continue

    batch_tensor = batch_imgs[valid_mask].to(DEVICE, non_blocking=True)
    valid_paths  = [p for p, ok in zip(batch_paths, batch_ok.tolist()) if ok]

    with torch.no_grad(), torch.amp.autocast(device_type=DEVICE if DEVICE != "mps" else "cpu", enabled=(DEVICE == "cuda")):
        img_features = model.encode_image(batch_tensor)
        img_features /= img_features.norm(dim=-1, keepdim=True)
        pf = pos_features.to(img_features.dtype)
        nf = neg_features.to(img_features.dtype)
        pos_scores   = (img_features @ pf.T).mean(dim=1)
        neg_scores   = (img_features @ nf.T).mean(dim=1)
        final_scores = pos_scores - neg_scores * NEG_WEIGHT

    embs_cpu = img_features.float().cpu().numpy()
    for path, pos, neg, final, emb in zip(
        valid_paths,
        pos_scores.float().cpu().tolist(),
        neg_scores.float().cpu().tolist(),
        final_scores.float().cpu().tolist(),
        embs_cpu,
    ):
        stem = Path(path).stem
        results.append({
            "scene":     stem,
            "score":     final,
            "pos_score": pos,
            "neg_score": neg,
        })
        scene_embs[stem] = emb

if not results:
    print("No frames scored — check that autocut/ has .mp4 files > 5MB.")
    sys.exit(1)

df_all = pd.DataFrame(results).sort_values("score", ascending=False)

# Main-cam only → OUTPUT_CSV (used by main pipeline — unchanged behaviour)
df_main = df_all[df_all["scene"].isin(main_cam_stems)] if SCORE_ALL_CAMS else df_all
df_main.to_csv(OUTPUT_CSV, index=False)

# All-cam → OUTPUT_CSV_ALLCAM (used by make_shorts.py for multicam shorts)
if SCORE_ALL_CAMS and OUTPUT_CSV_ALLCAM:
    df_all.to_csv(OUTPUT_CSV_ALLCAM, index=False)
    print(f"All-cam scores: {len(df_all)} scenes → {Path(OUTPUT_CSV_ALLCAM).name}")

# Main-cam embeddings → EMBEDDINGS_FILE (used by select_scenes dedup)
embs_main = {k: v for k, v in scene_embs.items() if k in main_cam_stems}
if embs_main:
    names = list(embs_main.keys())
    embs  = np.array([embs_main[n] for n in names], dtype=np.float32)
    np.savez_compressed(EMBEDDINGS_FILE, names=np.array(names), embeddings=embs)
    print(f"Embeddings: {len(names)} scenes → {Path(EMBEDDINGS_FILE).name}")

print(f"\nScored: {len(df_main)} scenes")
print(f"Score range: {df_main['score'].min():.3f} – {df_main['score'].max():.3f}")
cutoff = df_main["score"].quantile(1 - TOP_PERCENT / 100)
top = df_main[df_main["score"] >= cutoff]
print(f"Top {TOP_PERCENT}%: {len(top)} scenes (cutoff: {cutoff:.3f})")
print("\nTop 10:")
print(top.head(10)[["scene", "score"]].to_string(index=False))
