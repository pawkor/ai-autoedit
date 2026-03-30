#!/usr/bin/env python3
import os
import sys
import logging
import warnings
import configparser

os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

import torch
import open_clip

warnings.filterwarnings("ignore", message="QuickGELU mismatch", category=UserWarning)
import pandas as pd
from PIL import Image
from pathlib import Path
from tqdm import tqdm

_cfg = configparser.ConfigParser()
_script_dir = Path(__file__).resolve().parent
_cfg.read([_script_dir / "config.ini", Path.cwd() / "config.ini"])

FRAMES_DIR  = os.environ.get("FRAMES_DIR", "frames/")
OUTPUT_CSV  = os.environ.get("OUTPUT_CSV", "scene_scores.csv")
TOP_PERCENT = _cfg.getint("clip_scoring",   "top_percent", fallback=25)
NEG_WEIGHT  = _cfg.getfloat("clip_scoring", "neg_weight",  fallback=0.5)
BATCH_SIZE  = _cfg.getint("clip_scoring",   "batch_size",  fallback=64)
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

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

frames = sorted(Path(FRAMES_DIR).glob("*.jpg"))
print(f"Scoring {len(frames)} frames (batch={BATCH_SIZE})...")
results = []

for i in tqdm(range(0, len(frames), BATCH_SIZE)):
    batch_paths = frames[i:i + BATCH_SIZE]
    imgs, valid_paths = [], []

    for frame_path in batch_paths:
        try:
            imgs.append(preprocess(Image.open(frame_path).convert('RGB')))
            valid_paths.append(frame_path)
        except Exception as e:
            print(f"Error loading {frame_path}: {e}", file=sys.stderr)

    if not imgs:
        continue

    batch_tensor = torch.stack(imgs).to(DEVICE)

    with torch.no_grad(), torch.amp.autocast(device_type=DEVICE, enabled=(DEVICE == "cuda")):
        img_features = model.encode_image(batch_tensor)
        img_features /= img_features.norm(dim=-1, keepdim=True)
        pf = pos_features.to(img_features.dtype)
        nf = neg_features.to(img_features.dtype)
        pos_scores  = (img_features @ pf.T).mean(dim=1)
        neg_scores  = (img_features @ nf.T).mean(dim=1)
        final_scores = pos_scores - neg_scores * NEG_WEIGHT

    for path, pos, neg, final in zip(
        valid_paths,
        pos_scores.float().cpu().tolist(),
        neg_scores.float().cpu().tolist(),
        final_scores.float().cpu().tolist(),
    ):
        results.append({
            "scene":     Path(path).stem,
            "score":     final,
            "pos_score": pos,
            "neg_score": neg,
        })

if not results:
    print("No frames scored — check that autocut/ has .mp4 files > 5MB.")
    sys.exit(1)

df = pd.DataFrame(results).sort_values("score", ascending=False)
df.to_csv(OUTPUT_CSV, index=False)

print(f"\nScored: {len(df)} scenes")
print(f"Score range: {df['score'].min():.3f} – {df['score'].max():.3f}")
cutoff = df["score"].quantile(1 - TOP_PERCENT / 100)
top = df[df["score"] >= cutoff]
print(f"Top {TOP_PERCENT}%: {len(top)} scenes (cutoff: {cutoff:.3f})")
print("\nTop 10:")
print(top.head(10)[["scene", "score"]].to_string(index=False))
