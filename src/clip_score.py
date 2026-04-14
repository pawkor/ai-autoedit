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
TOP_PERCENT     = _cfg.getint("clip_scoring",   "top_percent",   fallback=25)
NEG_WEIGHT      = _cfg.getfloat("clip_scoring", "neg_weight",    fallback=0.5)
CLIP_MODEL      = _cfg.get("clip_scoring",      "model",         fallback="ViT-H-14")
CLIP_PRETRAINED = _cfg.get("clip_scoring",      "pretrained",    fallback="dfn5b")
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
print(f"Model: {CLIP_MODEL} / {CLIP_PRETRAINED}")
print(f"Batch size: {BATCH_SIZE}")

model, _, preprocess = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=CLIP_PRETRAINED)
tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
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
    stem = _re.sub(r'_f\d+$', '', path.stem)          # strip _f0/_f1/_f2
    src  = _re.sub(r'-scene-\d+$', '', stem)
    return src not in _back_sources

def _scene_stem(path: Path) -> str:
    """Return scene stem without _fN suffix."""
    return _re.sub(r'_f\d+$', '', path.stem)

all_frames_list = sorted(Path(FRAMES_DIR).glob("*.jpg"))
main_cam_stems  = set(_scene_stem(f) for f in all_frames_list if _is_main_cam(f))
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
frame_results: list[dict] = []
frame_embs:    dict[str, np.ndarray] = {}

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
        frame_stem = Path(path).stem
        frame_results.append({
            "frame":     frame_stem,
            "scene":     _scene_stem(Path(path)),
            "score":     final,
            "pos_score": pos,
            "neg_score": neg,
        })
        frame_embs[frame_stem] = emb

# Aggregate: per scene take the frame with max score
scene_best: dict[str, dict] = {}
scene_embs: dict[str, np.ndarray] = {}
for r in frame_results:
    sc = r["scene"]
    if sc not in scene_best or r["score"] > scene_best[sc]["score"]:
        scene_best[sc] = r
        scene_embs[sc] = frame_embs[r["frame"]]

results = [{"scene": r["scene"], "score": r["score"],
            "pos_score": r["pos_score"], "neg_score": r["neg_score"]}
           for r in scene_best.values()]

if not results:
    print("No frames scored — check that autocut/ has .mp4 files > 5MB.")
    sys.exit(1)

# ── Aesthetic scoring (LAION improved predictor, requires ViT-L-14 768-dim) ──
_AES_BACKBONE   = "ViT-L-14"
_AES_PRETRAINED = "openai"

def _aesthetic_mlp():
    import torch.nn as nn
    class _MLP(nn.Module):
        def __init__(self):
            super().__init__()
            self.layers = nn.Sequential(
                nn.Linear(768, 1024), nn.Dropout(0.2),
                nn.Linear(1024, 128), nn.Dropout(0.2),
                nn.Linear(128, 64),  nn.Dropout(0.1),
                nn.Linear(64, 16),
                nn.Linear(16, 1),
            )
        def forward(self, x):
            return self.layers(x)
    return _MLP()

def _load_aesthetic_predictor():
    import urllib.request
    cache = Path.home() / ".cache" / "aesthetic_predictor" / "sac+logos+ava1-l14-linearMSE.pth"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        url = ("https://github.com/christophschuhmann/improved-aesthetic-predictor"
               "/raw/main/sac%2Blogos%2Bava1-l14-linearMSE.pth")
        print("  Downloading aesthetic predictor weights (~5MB)...")
        urllib.request.urlretrieve(url, str(cache))
    m = _aesthetic_mlp()
    state = torch.load(str(cache), map_location="cpu", weights_only=True)
    if any(k.startswith("model.") for k in state):
        state = {k[6:]: v for k, v in state.items()}
    m.load_state_dict(state)
    return m.eval().to(DEVICE)

aes_scores: dict[str, float] = {}
try:
    _aes_model = _load_aesthetic_predictor()
    _stems = list(scene_best.keys())

    if CLIP_MODEL == _AES_BACKBONE:
        # Main model is ViT-L-14 — reuse existing 768-dim embeddings directly
        _aes_emb_t = torch.tensor(
            np.array([scene_embs[s] for s in _stems]), dtype=torch.float32
        ).to(DEVICE)
    else:
        # Main model uses different embedding space — run a lightweight ViT-L-14 pass
        # on the per-scene best frames only (much smaller set than all frames).
        print(f"  Aesthetic: loading {_AES_BACKBONE} for 768-dim embeddings ({len(_stems)} scenes)...")
        _aes_clip, _, _aes_prep = open_clip.create_model_and_transforms(
            _AES_BACKBONE, pretrained=_AES_PRETRAINED
        )
        _aes_clip = _aes_clip.to(DEVICE).eval()
        _aes_imgs = []
        for stem in _stems:
            img_path = Path(FRAMES_DIR) / f"{scene_best[stem]['frame']}.jpg"
            try:
                _aes_imgs.append(_aes_prep(Image.open(img_path).convert("RGB")))
            except Exception:
                _aes_imgs.append(torch.zeros(3, 224, 224))
        _aes_emb_parts: list[torch.Tensor] = []
        for i in range(0, len(_aes_imgs), BATCH_SIZE):
            batch = torch.stack(_aes_imgs[i:i + BATCH_SIZE]).to(DEVICE)
            with torch.no_grad():
                _aes_emb_parts.append(_aes_clip.encode_image(batch).float().cpu())
        del _aes_clip  # free VRAM before MLP pass
        _aes_emb_t = torch.cat(_aes_emb_parts).to(DEVICE)

    _aes_emb_t = _aes_emb_t / _aes_emb_t.norm(dim=-1, keepdim=True)
    with torch.no_grad():
        _aes_raw = _aes_model(_aes_emb_t).squeeze(-1).cpu().tolist()
    aes_scores = dict(zip(_stems, _aes_raw))
    print(f"Aesthetic: {min(_aes_raw):.2f}–{max(_aes_raw):.2f}  mean={sum(_aes_raw)/len(_aes_raw):.2f}")
except Exception as _e:
    print(f"Aesthetic scoring skipped: {_e}")

for r in results:
    r["aesthetic_score"] = round(aes_scores.get(r["scene"], float("nan")), 4)

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
