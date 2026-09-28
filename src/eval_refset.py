#!/usr/bin/env python3
"""
eval_refset.py — reference-set tooling for comparing CLIP backbones / weights.

Two modes:

1. Sample frames for labelling (stratified over source files):
     python3 src/eval_refset.py sample /data/2026/07/Toskania/23.07 --n 100
   Copies peak frames into <work_dir>/_autoframe/refset/frames/ and writes
   refset/labels.csv with empty label column. Fill the label column with one
   of: good, ok, bad (anything else is ignored). Run for several projects —
   labels accumulate per project and evaluation can take many work_dir args.

2. Evaluate a scene_scores.csv against the labels:
     python3 src/eval_refset.py eval /data/.../23.07 [/data/.../24.07 ...]
   Prints precision@K for "good", mean score per label, and Spearman rank
   correlation between the score column and the label ordering
   (good=2, ok=1, bad=0). Use --column to test other columns
   (e.g. aesthetic_score) and compare model runs on identical labels.
"""
import argparse
import csv
import random
import re
import shutil
import sys
from pathlib import Path


def _read_labels(refset_dir: Path) -> dict[str, str]:
    labels: dict[str, str] = {}
    f = refset_dir / "labels.csv"
    if not f.exists():
        return labels
    with open(f) as fh:
        for row in csv.DictReader(fh):
            lab = (row.get("label") or "").strip().lower()
            if lab in ("good", "ok", "bad"):
                labels[row["scene"]] = lab
    return labels


def cmd_sample(work_dir: Path, n: int) -> int:
    auto = work_dir / "_autoframe"
    frames = sorted((auto / "frames").glob("*_f0.jpg"))
    if not frames:
        print(f"No peak frames in {auto / 'frames'} — run Analyze first")
        return 1
    refset = auto / "refset"
    (refset / "frames").mkdir(parents=True, exist_ok=True)
    existing = _read_labels(refset)

    # Stratify: round-robin over source files so one long recording cannot
    # dominate the sample.
    by_src: dict[str, list[Path]] = {}
    for p in frames:
        stem = re.sub(r"_f\d+$", "", p.stem)
        if stem in existing:
            continue
        src = re.sub(r"-(?:scene|clip|photo)-\d+$", "", stem)
        by_src.setdefault(src, []).append(p)
    for lst in by_src.values():
        random.shuffle(lst)
    picked: list[Path] = []
    while len(picked) < n and any(by_src.values()):
        for src in list(by_src):
            if by_src[src]:
                picked.append(by_src[src].pop())
                if len(picked) >= n:
                    break
            else:
                by_src.pop(src)

    rows = [{"scene": re.sub(r"_f\d+$", "", p.stem), "label": ""} for p in picked]
    for p in picked:
        shutil.copy2(p, refset / "frames" / p.name)
    # Rewrite labels.csv, keeping already-labelled rows.
    all_rows = ([{"scene": s, "label": l} for s, l in existing.items()] + rows)
    with open(refset / "labels.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["scene", "label"])
        w.writeheader()
        w.writerows(all_rows)
    print(f"Sampled {len(picked)} new frames → {refset / 'frames'}")
    print(f"Label them in {refset / 'labels.csv'} (good / ok / bad)")
    return 0


def cmd_eval(work_dirs: list[Path], column: str) -> int:
    import pandas as pd
    from scipy.stats import spearmanr

    parts = []
    for wd in work_dirs:
        auto = wd / "_autoframe"
        labels = _read_labels(auto / "refset")
        if not labels:
            print(f"  (no labels in {wd})")
            continue
        csv_path = auto / "scene_scores_allcam.csv"
        if not csv_path.exists():
            csv_path = auto / "scene_scores.csv"
        if not csv_path.exists():
            print(f"  (no scene_scores CSV in {wd})")
            continue
        df = pd.read_csv(csv_path)
        df = df[df["scene"].isin(labels)].copy()
        df["label"] = df["scene"].map(labels)
        parts.append(df)
    if not parts:
        print("Nothing to evaluate — run `sample` and label frames first.")
        return 1
    df = pd.concat(parts, ignore_index=True)
    if column not in df.columns:
        print(f"Column {column} not present in the CSVs.")
        return 1
    df = df.dropna(subset=[column])
    if df.empty:
        print(f"Column {column} has no values for the labelled scenes.")
        return 1

    order = {"good": 2, "ok": 1, "bad": 0}
    df["_rank"] = df["label"].map(order)
    print(f"Labelled clips with {column}: {len(df)} "
          f"(good={int((df['label']=='good').sum())}, "
          f"ok={int((df['label']=='ok').sum())}, "
          f"bad={int((df['label']=='bad').sum())})")
    print(f"\nMean {column} per label:")
    for lab in ("good", "ok", "bad"):
        sub = df[df["label"] == lab][column]
        if len(sub):
            print(f"  {lab:5s}: {sub.mean():.4f}  (±{sub.std():.4f}, n={len(sub)})")
    top = df.sort_values(column, ascending=False)
    print(f"\nPrecision@K (share of 'good' among top-K by {column}):")
    for k in (10, 25, 50):
        if len(top) >= k:
            pk = (top.head(k)["label"] == "good").mean()
            print(f"  P@{k}: {pk:.2f}")
    rho, p = spearmanr(df[column], df["_rank"])
    print(f"\nSpearman(score, label): rho={rho:.3f}  p={p:.3g}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample", help="copy N peak frames for labelling")
    sp.add_argument("work_dir", type=Path)
    sp.add_argument("--n", type=int, default=100)
    ev = sub.add_parser("eval", help="score a CSV column against the labels")
    ev.add_argument("work_dirs", type=Path, nargs="+")
    ev.add_argument("--column", default="score",
                    help="CSV column to evaluate (score, aesthetic_score, ...)")
    args = ap.parse_args()
    if args.cmd == "sample":
        return cmd_sample(args.work_dir, args.n)
    return cmd_eval(args.work_dirs, args.column)


if __name__ == "__main__":
    sys.exit(main())
