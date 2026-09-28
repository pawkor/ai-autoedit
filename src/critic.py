#!/usr/bin/env python3
"""
critic.py — VLM critic for a built timeline. Two tiers:

quick (default):  up to 40 sampled slots, TWO frames each (CLIP-peak thumb +
                  a late-in-clip extract) → judges pool composition + motion.
deep (--deep V):  frames sampled from the ACTUAL rendered draft video V at
                  even timestamps → judges the real edit: cuts, rhythm,
                  transitions, ending.

Both tiers include deterministic metrics (source repeats, chronology
inversions, slot-duration stats) so the model reasons from hard numbers.

stdout: STRICT JSON {score_0_10, verdict, issues[], suggestions{...}}.
Requires ANTHROPIC_API_KEY. Non-zero exit + stderr message on failure.
"""
import base64
import configparser
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_SLOTS_QUICK = 40      # ×2 images = ≤80 (API limit 100)
MAX_FRAMES_DEEP = 66
MODEL = os.environ.get("CRITIC_MODEL", "claude-sonnet-5")

PROMPT_COMMON = """You are a seasoned action-sports film editor reviewing a \
motorcycle highlight-reel edit. CONTEXT: this is a motovlog pipeline — the \
raw material is many hours of riding from one or two FIXED MOUNTED cameras \
(helmet POV + bike-mounted), so most shots are inherently riding footage \
with similar framing. Judge within that genre: never penalize the format \
itself, and never suggest shot types this footage cannot contain (drone, \
tripod B-roll, filmed close-ups). Score what the EDIT does with the \
material it has: camera alternation, scenery/lighting/time-of-day changes, \
pacing vs implied music energy, stops/photos used as breathers, narrative \
arc of the day, the ending. Score anchors: 5 = competent typical motovlog \
edit; deduct only for AVOIDABLE flaws (same camera or near-identical \
composition back-to-back, scenery variety left unused, weak ending when the \
pool had better). Chronology is a soft weight by design — mild backward \
jumps are intentional, flag only jarring ones. The METRICS camera counts \
and the slot-table cam column are ground truth for which camera each slot \
uses — do not claim single-camera coverage against them. Be concrete and \
terse.

Reply with STRICT JSON only (no prose outside JSON):
{"score_0_10": int,
 "verdict": "one sentence",
 "issues": [{"at": "m:ss", "problem": "..."}],
 "suggestions": {
   "adjacent_time_gap_sec": number or null,
   "chron_weight": number or null,
   "dedup_similarity": number or null,
   "comment": "one sentence rationale"
 }}
suggestions semantics: adjacent_time_gap_sec — raise to force bigger \
capture-time jumps between neighbouring shots; chron_weight 0..0.3 — lower = \
less chronological ordering; dedup_similarity 0.88..0.985 — lower = prune \
more near-duplicates. Only set a suggestion when confident it would visibly \
improve THIS edit; otherwise null. Issues: max 6, most important first, \
each problem ≤ 25 words."""

_safe = re.compile(r"[^\w\-\.]+")


def _ffmpeg_bin(work_dir: Path) -> str:
    cp = configparser.ConfigParser()
    cp.read([Path(__file__).resolve().parent.parent / "config.ini",
             work_dir / "config.ini"])
    return os.path.expanduser(cp.get("paths", "ffmpeg", fallback="ffmpeg"))


def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _metrics(slots: list[dict]) -> str:
    """Deterministic edit diagnostics — hard numbers beat squinting at JPEGs."""
    def _src(s):
        return re.sub(r"-(?:scene|clip|photo)-\d+$", "", str(s.get("scene", "")))
    durs = [float(s.get("duration", 0)) for s in slots]
    srcs = [_src(s) for s in slots]
    same_adj = sum(1 for a, b in zip(srcs, srcs[1:]) if a == b)
    from collections import Counter
    top_src = Counter(srcs).most_common(3)
    tnorms = [s.get("clip_time_norm") for s in slots]
    tn = [t for t in tnorms if t is not None]
    inversions = sum(1 for a, b in zip(tn, tn[1:]) if b < a - 1e-9)
    cams = [str(s.get("camera") or "?") for s in slots]
    same_cam_adj = sum(1 for a, b in zip(cams, cams[1:]) if a == b)
    lines = [
        f"slots={len(slots)}  total={sum(durs):.1f}s  "
        f"slot_dur min/med/max={min(durs):.1f}/{sorted(durs)[len(durs)//2]:.1f}/{max(durs):.1f}s",
        f"adjacent same-source pairs={same_adj}  "
        f"top sources={[(re.sub(_safe, '_', s)[-20:], c) for s, c in top_src]}",
        f"cameras={dict(Counter(cams))}  adjacent same-camera pairs={same_cam_adj}",
    ]
    if tn:
        lines.append(f"chronology: timed={len(tn)}/{len(slots)}  "
                     f"backward jumps={inversions}  "
                     f"day coverage={min(tn):.2f}..{max(tn):.2f}")
    return "\n".join(lines)


def _slot_table(slots: list[dict], idxs: list[int]) -> str:
    rows = []
    for i in idxs:
        s = slots[i]
        _day = s.get("clip_time_norm")
        try:
            _daytxt = f"{float(_day):.2f}" if _day is not None else "?"
        except (TypeError, ValueError):
            _daytxt = "?"
        rows.append(f"{i:3d}  {_fmt(float(s.get('music_start', 0)))}  "
                    f"{_safe.sub('_', str(s.get('scene', '?')))[:60]}  "
                    f"dur={float(s.get('duration', 0)):.1f}s  "
                    f"cam={_safe.sub('_', str(s.get('camera', '') or '-'))[:12]}  "
                    f"day={_daytxt}")
    return "\n".join(rows)


def _img_block(path: Path) -> dict | None:
    try:
        if not path.exists() or path.stat().st_size > 2_000_000:
            return None
        return {"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.standard_b64encode(path.read_bytes()).decode()}}
    except OSError:
        return None


def _shrink(p: Path, td: Path, ffmpeg: str) -> Path:
    """Re-encode oversized stills to 640px — peak thumbs can be full-res
    frame grabs; 80 of those would blow the API request size limit."""
    try:
        if p.stat().st_size <= 400_000:
            return p
    except OSError:
        return p
    out = td / ("s_" + _safe.sub("_", p.name))
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(p), "-vf", "scale=640:-2", "-q:v", "5",
                        str(out)], capture_output=True, timeout=15)
    return out if (r.returncode == 0 and out.exists()) else p


def _even_idxs(n_total: int, n_want: int) -> list[int]:
    if n_total <= 0:
        return []
    n = min(n_want, n_total)
    if n == 1:
        return [0]
    return sorted({round(i * (n_total - 1) / (n - 1)) for i in range(n)})


def main() -> int:
    args = [a for a in sys.argv[1:]]
    deep_video: Path | None = None
    if "--deep" in args:
        i = args.index("--deep")
        deep_video = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    if not args:
        print("usage: critic.py <work_dir> [sequence_json] [--deep video]", file=sys.stderr)
        return 2
    work_dir = Path(args[0]).resolve()
    seq_path = (Path(args[1]) if len(args) > 1
                else work_dir / "_autoframe" / "preview_sequence.json")
    if not seq_path.exists():
        print("preview_sequence.json not found — run Build Timeline first", file=sys.stderr)
        return 3
    data = json.loads(seq_path.read_text())
    slots = [s for s in data.get("sequence", []) if s.get("type") != "photo"]
    if not slots:
        print("empty sequence", file=sys.stderr)
        return 3

    ffmpeg = _ffmpeg_bin(work_dir)
    images: list[dict] = []
    mode_note = ""

    with tempfile.TemporaryDirectory(prefix="critic_") as _td:
        td = Path(_td)
        if deep_video is not None:
            # DEEP: frames from the actual rendered edit — real cuts & rhythm.
            try:
                deep_video.relative_to(work_dir)
            except ValueError:
                print("deep video must live inside the project", file=sys.stderr)
                return 3
            if not deep_video.exists():
                print(f"draft video missing: {deep_video}", file=sys.stderr)
                return 3
            total = sum(float(s.get("duration", 0)) for s in data.get("sequence", []))
            fps = MAX_FRAMES_DEEP / max(total, 1.0)
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-i", str(deep_video),
                 "-vf", f"fps={fps:.6f},scale=640:-2",
                 "-q:v", "5", str(td / "f%03d.jpg")],
                capture_output=True, timeout=180)
            if r.returncode != 0:
                print(f"frame extraction failed: {r.stderr.decode()[-300:]}", file=sys.stderr)
                return 4
            for p in sorted(td.glob("f*.jpg"))[:MAX_FRAMES_DEEP]:
                b = _img_block(p)
                if b:
                    images.append(b)
            idxs = _even_idxs(len(slots), 60)
            mode_note = (f"The {len(images)} frames are EVEN TIME SAMPLES OF THE "
                         f"RENDERED EDIT (~{1/fps:.1f}s apart, in order) — you are "
                         f"watching the actual cut, including transitions.")
        else:
            # QUICK: sampled slots, 2 frames each (CLIP-peak + late-in-clip).
            idxs = _even_idxs(len(slots), MAX_SLOTS_QUICK)
            for i in idxs:
                s = slots[i]
                # Label each image with its slot — a missing/failed frame
                # must not silently shift an implied pairing.
                _tag = f"slot {i} @{_fmt(float(s.get('music_start', 0)))}"
                fp = s.get("frame_path")
                if fp:
                    fpp = Path(fp).resolve()
                    try:
                        fpp.relative_to(work_dir)
                        b = _img_block(_shrink(fpp, td, ffmpeg))
                        if b:
                            images.append({"type": "text",
                                           "text": f"{_tag} — CLIP peak:"})
                            images.append(b)
                    except ValueError:
                        pass
                # Second frame: late in the clip → shows in-shot motion.
                _clip = s.get("clip_path") or ""
                _cpp = Path(_clip) if _clip else (
                    work_dir / "_autoframe" / "autocut" / f"{s.get('scene', '')}.mp4")
                try:
                    _cpp = _cpp.resolve()
                    _cpp.relative_to(work_dir)
                except ValueError:
                    continue
                if not _cpp.exists():
                    continue
                _t = float(s.get("clip_ss", 0)) + 0.8 * float(s.get("duration", 1))
                _out = td / f"m{i:03d}.jpg"
                subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error",
                     "-ss", f"{_t:.2f}", "-i", str(_cpp),
                     "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5",
                     str(_out)], capture_output=True, timeout=15)
                b = _img_block(_out)
                if b:
                    images.append({"type": "text",
                                   "text": f"{_tag} — later moment:"})
                    images.append(b)
            mode_note = ("Each image is preceded by a text label naming its "
                         "slot: 'CLIP peak' = the scored moment, 'later "
                         "moment' = the same clip further in — together they "
                         "show in-shot motion. Some slots may have only one "
                         "image.")

        if not images:
            print("no readable frames", file=sys.stderr)
            return 3

        try:
            import anthropic
        except ImportError:
            print("anthropic package not installed", file=sys.stderr)
            return 4

        client = anthropic.Anthropic()
        text = (PROMPT_COMMON + "\n\n" + mode_note +
                "\n\nMETRICS:\n" + _metrics(slots) +
                "\n\nSlot table (sampled):\n" + _slot_table(slots, idxs))
        msg = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": images + [{"type": "text", "text": text}]}],
        )
    out_text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    if getattr(msg, "stop_reason", "") == "max_tokens":
        print(f"model output truncated at max_tokens — verdict unusable:\n{out_text[:400]}",
              file=sys.stderr)
        return 5
    m = re.search(r"\{.*\}", out_text, re.DOTALL)
    if not m:
        print(f"model returned no JSON: {out_text[:400]}", file=sys.stderr)
        return 5
    try:
        verdict = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"bad JSON from model: {e}\n{out_text[:400]}", file=sys.stderr)
        return 5
    print(json.dumps(verdict, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
