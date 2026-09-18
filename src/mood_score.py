#!/usr/bin/env python3
"""
Mood-only scoring pass.
Reads saved scene_embeddings.npz (written by clip_scan.py or clip_score.py) and encodes
action/scenic text prompts using the same CLIP model — no image GPU pass needed.
Writes action_score / scenic_score columns to the existing scene_scores.csv.

Called by pipeline.py when CLIP scores are cached but mood columns are missing.
"""
import os, sys, configparser
import numpy as np
import torch
import open_clip
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HUGGINGFACE_HUB_VERBOSITY", "error")
import logging; logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

import pandas as pd

_script_dir = Path(__file__).resolve().parent
_cfg = configparser.ConfigParser()
_cfg.read([_script_dir.parent / "config.ini", _script_dir / "config.ini", Path.cwd() / "config.ini"])

OUTPUT_CSV        = os.environ.get("OUTPUT_CSV", "scene_scores.csv")
OUTPUT_CSV_ALLCAM = os.environ.get("OUTPUT_CSV_ALLCAM", "")
EMBEDDINGS_FILE   = os.environ.get(
    "EMBEDDINGS_FILE",
    str(Path(OUTPUT_CSV).parent / "scene_embeddings.npz")
)
CLIP_MODEL      = _cfg.get("clip_scoring", "model",      fallback="ViT-L-14")
CLIP_PRETRAINED = _cfg.get("clip_scoring", "pretrained", fallback="openai")

def _parse(raw): return [l.strip() for l in raw.strip().splitlines() if l.strip()]

if not _cfg.getboolean("mood_scoring", "enabled", fallback=False):
    print("Mood scoring: disabled")
    sys.exit(0)

_action_prompts = _parse(_cfg.get("mood_scoring", "action_prompts", fallback=""))
_scenic_prompts = _parse(_cfg.get("mood_scoring", "scenic_prompts", fallback=""))
if not _action_prompts or not _scenic_prompts:
    print("Mood scoring: no prompts configured in [mood_scoring]")
    sys.exit(0)

emb_path = Path(EMBEDDINGS_FILE)
csv_path = Path(OUTPUT_CSV)
if not emb_path.exists():
    print(f"Mood scoring: embeddings not found ({emb_path.name}) — re-run Analyze")
    sys.exit(1)
if not csv_path.exists():
    print(f"Mood scoring: CSV not found: {csv_path}")
    sys.exit(1)

# Load embeddings first — detect actual dim to pick correct CLIP model
data     = np.load(str(emb_path))
emb_dim  = int(data["embeddings"].shape[1])

# Newer embedding caches carry the exact backbone identity.  Prefer it over
# dimension-based inference: different CLIP families can share a feature
# width while living in incompatible embedding spaces (e.g. SigLIP2 B-16 and
# OpenAI ViT-L-14 are both 768-dimensional).
_saved_model = data["model"].item() if "model" in data.files else ""
_saved_pretrained = data["pretrained"].item() if "pretrained" in data.files else ""
if _saved_model and _saved_pretrained:
    CLIP_MODEL, CLIP_PRETRAINED = str(_saved_model), str(_saved_pretrained)
    print(f"  Embedding metadata → {CLIP_MODEL}/{CLIP_PRETRAINED}")

# Map embedding dim → (model, pretrained). Fallback covers legacy ViT-H-14 projects.
_DIM_TO_MODEL = {
    768:  ("ViT-L-14", "openai"),
    1024: ("ViT-H-14", "dfn5b"),
    512:  ("ViT-B-32", "openai"),
    1280: ("ViT-G-14", "laion2b_s12b_b42k"),
    # CLIP-first's default backbone.  Its image and text features are
    # 1152-dimensional; using the configured ViT-L text encoder here gives a
    # misleading matmul error (or, worse, silently mismatched scores).
    1152: ("ViT-SO400M-16-SigLIP2-384", "webli"),
}
_model_dim = {
    "ViT-L-14": 768, "ViT-H-14": 1024, "ViT-B-32": 512,
    "ViT-G-14": 1280, "ViT-SO400M-16-SigLIP2-384": 1152,
}.get(CLIP_MODEL, 0)
if not (_saved_model and _saved_pretrained) and _model_dim != emb_dim and emb_dim in _DIM_TO_MODEL:
    CLIP_MODEL, CLIP_PRETRAINED = _DIM_TO_MODEL[emb_dim]
    print(f"  Embedding dim {emb_dim} → auto-selected {CLIP_MODEL}/{CLIP_PRETRAINED}")
if emb_dim not in _DIM_TO_MODEL and not (_saved_model and _saved_pretrained):
    raise RuntimeError(f"Mood scoring: unsupported embedding dimension {emb_dim}; "
                       "the saved embeddings need a matching CLIP backbone")

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Mood scoring: {len(_action_prompts)} action + {len(_scenic_prompts)} scenic prompts  [{CLIP_MODEL}]")

import warnings; warnings.filterwarnings("ignore", message="QuickGELU mismatch", category=UserWarning)
if CLIP_PRETRAINED.startswith("hf-hub:"):
    model, _ = open_clip.create_model_from_pretrained(CLIP_PRETRAINED)
    tokenizer = open_clip.get_tokenizer(CLIP_PRETRAINED)
else:
    model, _, _ = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=CLIP_PRETRAINED)
    tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
model = model.to(device).eval()

with torch.no_grad():
    _at = tokenizer(_action_prompts).to(device)
    _st = tokenizer(_scenic_prompts).to(device)
    af = model.encode_text(_at).float(); af /= af.norm(dim=-1, keepdim=True)
    sf = model.encode_text(_st).float(); sf /= sf.norm(dim=-1, keepdim=True)
    act_mean = af.mean(dim=0); act_mean /= act_mean.norm()
    sce_mean = sf.mean(dim=0); sce_mean /= sce_mean.norm()

names = data["names"].tolist()
embs  = torch.tensor(data["embeddings"], dtype=torch.float32).to(device)
with torch.no_grad():
    act_s = (embs @ act_mean).cpu().tolist()
    sce_s = (embs @ sce_mean).cpu().tolist()

mood_map = {n: (round(float(a), 4), round(float(s), 4))
            for n, a, s in zip(names, act_s, sce_s)}
print(f"  action {min(act_s):.3f}–{max(act_s):.3f}  scenic {min(sce_s):.3f}–{max(sce_s):.3f}")

def _apply(df):
    df["action_score"] = df["scene"].map(lambda sc: mood_map.get(sc, (float("nan"), float("nan")))[0])
    df["scenic_score"] = df["scene"].map(lambda sc: mood_map.get(sc, (float("nan"), float("nan")))[1])
    return df

df = _apply(pd.read_csv(csv_path))
df.to_csv(csv_path, index=False)
print(f"  {csv_path.name}: {len(df)} scenes updated")

if OUTPUT_CSV_ALLCAM and Path(OUTPUT_CSV_ALLCAM).exists():
    dfa = _apply(pd.read_csv(OUTPUT_CSV_ALLCAM))
    dfa.to_csv(OUTPUT_CSV_ALLCAM, index=False)
    print(f"  {Path(OUTPUT_CSV_ALLCAM).name}: {len(dfa)} scenes updated")
