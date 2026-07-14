#!/usr/bin/env python3
"""
music_driven.py — Music-to-Motion Alignment highlight assembler.

Instead of selecting scenes and dropping music on top, this module:
  1. Analyses the music track → beats + energy envelope
  2. Builds a cut schedule: high-energy sections → fast cuts, low → slow
  3. Computes motion profiles for top CLIP-scored clips (OpenCV frame diff)
  4. Matches clips to slots: high-energy slot → high CLIP + high motion clip
  5. Aligns each clip's motion peak to the beat hit (Motion Anchor)
  6. Renders directly — chronological order ignored

Inputs:
  _autoframe/autocut/*.mp4     — clips from clip_scan
  _autoframe/scene_scores.csv  — CLIP scores
  <music_file>                 — track to drive the edit

Output:
  _autoframe/highlight_music_driven.mp4
"""
from __future__ import annotations

import csv
from datetime import datetime
import json as _json
import os
import random
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

_WORKERS = min(os.cpu_count() or 4, 12)

def _ts() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


# ── Music analysis ────────────────────────────────────────────────────────────

def _get_vocals_demucs(music_path: Path, sr: int) -> "np.ndarray | None":
    """
    Separate vocals via Meta Demucs (htdemucs) Python API.
    Uses soundfile for output (avoids torchaudio.save → torchcodec dependency).
    Returns mono waveform at `sr` Hz, or None if Demucs unavailable/fails.
    Cache: .vocals_{stem}.wav next to the music file.
    """
    cache = music_path.parent / f".vocals_{music_path.stem}.wav"
    if cache.exists():
        try:
            import soundfile as _sf
            y_voc, _vsr = _sf.read(str(cache), always_2d=False)
            return y_voc if _vsr == sr else None
        except Exception:
            pass

    try:
        import torch
        import soundfile as _sf
        import librosa as _lib
        from demucs.pretrained import get_model as _get_model
        from demucs.apply import apply_model as _apply_model
    except ImportError:
        return None

    try:
        # Redirect model cache to writable /data/.cache/torch
        _cache_root = Path(os.environ.get("DATA_DIR", "/data")) / ".cache"
        _cache_root.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("TORCH_HOME", str(_cache_root / "torch"))

        # Decode music → stereo 44100 Hz WAV via ffmpeg (avoids torchaudio.load)
        _tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        _tmp.close()
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(music_path),
                 "-ar", "44100", "-ac", "2", _tmp.name],
                check=True,
            )
            wav_np, wav_sr = _sf.read(_tmp.name, always_2d=True)  # [samples, channels]
        finally:
            Path(_tmp.name).unlink(missing_ok=True)

        # [batch=1, channels, samples]
        wav_t = torch.tensor(wav_np.T, dtype=torch.float32).unsqueeze(0)

        _device = "cuda" if torch.cuda.is_available() else "cpu"
        model = _get_model("htdemucs")
        model.to(_device).eval()

        # Resample to model's native sr if needed
        if wav_sr != model.samplerate:
            import torchaudio as _ta
            wav_t = _ta.functional.resample(wav_t, wav_sr, model.samplerate)

        with torch.no_grad():
            sources = _apply_model(model, wav_t.to(_device), overlap=0.25, shifts=0)

        vocals_idx = model.sources.index("vocals")
        vocals_mono = sources[0, vocals_idx].mean(0).cpu().numpy()  # [samples]

        # Resample to analysis sr and save
        vocals_rs = _lib.resample(vocals_mono, orig_sr=model.samplerate, target_sr=sr)
        _sf.write(str(cache), vocals_rs, sr)
        del model, sources, wav_t
        return vocals_rs

    except Exception as _e:
        print(f"  [Demucs] failed: {_e}")
        return None

def _get_chorus_whisperx(music_path: Path) -> list[tuple[float, float]]:
    """
    Use WhisperX forced alignment to find chorus regions via repeated lyrics.
    Returns list of (start, end) timestamp pairs.
    Returns [] if WhisperX unavailable, transcription fails, or music is instrumental.
    Cache: .chorus_{stem}.json next to music file.
    """
    cache = music_path.parent / f".chorus_{music_path.stem}.json"
    if cache.exists():
        try:
            return [tuple(x) for x in _json.loads(cache.read_text())]
        except Exception:
            pass

    try:
        # torchaudio ≥2.1 removed AudioMetaData; patch before importing whisperx
        import torchaudio as _ta
        if not hasattr(_ta, "AudioMetaData"):
            _ta.AudioMetaData = type("AudioMetaData", (), {})
        import whisperx as _wx
    except (ImportError, Exception):
        return []

    try:
        _device = "cuda"
        _model = _wx.load_model("large-v3", _device, compute_type="float16")
        _audio = _wx.load_audio(str(music_path))
        _res   = _model.transcribe(_audio, batch_size=16)
        del _model  # free VRAM before alignment model loads

        _lang = _res.get("language", "en") or "en"
        _ma, _meta = _wx.load_align_model(language_code=_lang, device=_device)
        _res = _wx.align(_res["segments"], _ma, _meta, _audio, _device,
                          return_char_alignments=False)
        del _ma

        words = [
            {"word": w["word"].lower().strip("'\".,!? "), "start": w["start"]}
            for w in _res.get("word_segments", [])
            if w.get("word") and w.get("start") is not None
        ]
        if len(words) < 20:
            return []  # instrumental / too sparse to detect structure

        # Sliding 4-second windows; signature = first 5 non-empty words
        WIN, STEP = 4.0, 2.0
        duration = float(_audio.shape[-1]) / 16000
        fingerprints: dict[str, list[float]] = {}
        t = 0.0
        while t < duration:
            bucket = [w["word"] for w in words if t <= w["start"] < t + WIN][:5]
            sig = " ".join(w for w in bucket if w)
            if len(sig.split()) >= 3:
                fingerprints.setdefault(sig, []).append(t)
            t += STEP

        # Repeated fingerprints (≥3 occurrences) mark chorus
        chorus_ts: list[float] = sorted({
            ts for sig, times in fingerprints.items()
            if len(times) >= 3
            for ts in times
        })
        if not chorus_ts:
            return []

        # Merge overlapping windows into contiguous regions
        regions: list[tuple[float, float]] = []
        r_start = chorus_ts[0]
        r_end   = chorus_ts[0] + WIN
        for t in chorus_ts[1:]:
            if t <= r_end + STEP:
                r_end = t + WIN
            else:
                regions.append((r_start, r_end))
                r_start, r_end = t, t + WIN
        regions.append((r_start, r_end))

        cache.write_text(_json.dumps(regions))
        print(f"  [WhisperX] {len(regions)} chorus region(s) detected  lang={_lang}")
        return regions

    except Exception as _e:
        print(f"  [WhisperX] skipped: {_e}")
        return []


def analyze_music(music_path: Path) -> dict:
    """
    Returns: duration, tempo, beat_times[], beat_energy[] (normalised 0-1).
    """
    import warnings
    warnings.filterwarnings("ignore", message=".*PySoundFile.*")
    warnings.filterwarnings("ignore", message=".*audioread.*")
    import librosa

    print(f"  Music: {music_path.name} …", end="", flush=True)
    # Decode via ffmpeg → temp WAV so PySoundFile handles it (avoids audioread deprecation)
    ffmpeg = "ffmpeg"
    _tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    _tmp.close()
    _beatnet_beats: list[float] | None = None
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(music_path),
             "-ar", "22050", "-ac", "1", _tmp.name],
            check=True,
        )
        y, sr = librosa.load(_tmp.name, sr=None, mono=True)
        try:
            from beatnet import BeatNet as _BeatNet
            _bn_out = np.array(
                _BeatNet(1, mode="offline", inference_model="DBN",
                         plot=[], thread=False).process(_tmp.name)
            )
            if _bn_out.ndim == 2 and len(_bn_out) > 4:
                _beatnet_beats = _bn_out[:, 0].tolist()
        except Exception:
            pass
    finally:
        Path(_tmp.name).unlink(missing_ok=True)
    duration = len(y) / sr

    hop = 512
    # Onset strength as input to beat_track → more accurate beat timestamps
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=hop)
    tempo = float(np.squeeze(tempo))  # librosa ≥0.10 returns 0-dim array
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop).tolist()

    if _beatnet_beats:
        beat_times = _beatnet_beats
        _ivals = np.diff(beat_times)
        tempo = 60.0 / float(np.median(_ivals)) if len(_ivals) > 0 else tempo
        print(f"  [BeatNet] {len(beat_times)} beats  {tempo:.0f} BPM")

    # PLP (Predominant Local Pulse) — rhythmic intensity per beat.
    pulse = librosa.beat.plp(onset_envelope=onset_env, sr=sr, hop_length=hop)
    pulse_times = librosa.times_like(pulse, sr=sr, hop_length=hop)

    beat_energy = np.interp(beat_times, pulse_times, pulse)
    e_min, e_max = beat_energy.min(), beat_energy.max()
    if e_max > e_min:
        beat_energy = (beat_energy - e_min) / (e_max - e_min)
    else:
        beat_energy = np.ones(len(beat_times)) * 0.5

    # Onset strength on percussive and harmonic/vocal components.
    # Percussion (kick/snare): HPSS isolation is sufficient and fast.
    # Vocals (melodic attacks, "the best"): Demucs separation preferred — 10-30 ms
    # accuracy vs HPSS 20-60 ms. Falls back to HPSS harmonic if Demucs unavailable.
    try:
        y_harm, y_perc = librosa.effects.hpss(y, margin=4.0)
        onset_env_perc = librosa.onset.onset_strength(y=y_perc, sr=sr, hop_length=hop)
        onset_env_harm = librosa.onset.onset_strength(y=y_harm, sr=sr, hop_length=hop)
        _y_voc = _get_vocals_demucs(music_path, sr)
        if _y_voc is not None:
            onset_env_harm = librosa.onset.onset_strength(y=_y_voc, sr=sr, hop_length=hop)
            print("  [Demucs] vocal onset active")
    except Exception:
        onset_env_perc = onset_env
        onset_env_harm = onset_env
    onset_times = librosa.times_like(onset_env_perc, sr=sr, hop_length=hop)
    onset_at_beats = np.interp(beat_times, onset_times, onset_env_perc)
    o_min, o_max = onset_at_beats.min(), onset_at_beats.max()
    onset_at_beats = (onset_at_beats - o_min) / (o_max - o_min) if o_max > o_min else np.ones(len(beat_times)) * 0.5

    harm_at_beats = np.interp(beat_times, onset_times, onset_env_harm)
    h_min, h_max = harm_at_beats.min(), harm_at_beats.max()
    harm_at_beats = (harm_at_beats - h_min) / (h_max - h_min) if h_max > h_min else np.ones(len(beat_times)) * 0.5

    # Section-level energy: RMS smoothed over ~4s windows.
    # This captures intro/verse/chorus/outro dynamics rather than per-beat pulse.
    # Used in auto mode to vary shot duration by musical section, not individual beats.
    _sec_hop = int(sr * 0.1)   # 100ms hop for fine resolution
    _sec_win = int(sr * 4.0)   # 4-second window — section granularity
    rms = librosa.feature.rms(y=y, frame_length=_sec_win, hop_length=_sec_hop)[0]
    rms_times = librosa.times_like(rms, sr=sr, hop_length=_sec_hop)
    # Additional smoothing: running average of 2s to remove transients
    _smooth_win = max(1, int(2.0 / 0.1))
    try:
        from scipy.ndimage import uniform_filter1d as _uf1d
        rms_smooth = _uf1d(rms.astype(float), size=_smooth_win)
    except ImportError:
        rms_smooth = rms.astype(float)
    section_energy = np.interp(beat_times, rms_times, rms_smooth)
    s_min, s_max = section_energy.min(), section_energy.max()
    if s_max > s_min:
        section_energy = (section_energy - s_min) / (s_max - s_min)
    else:
        section_energy = np.ones(len(beat_times)) * 0.5

    # Normalize raw percussive envelope for peak detection (stored for build_schedule_peaks)
    oep_max = onset_env_perc.max()
    onset_env_perc_norm = (onset_env_perc / oep_max).tolist() if oep_max > 0 else onset_env_perc.tolist()

    # Structural segmentation: agglomerative clustering on chroma+MFCC.
    # Identifies verse/chorus/bridge boundaries; each segment gets an RMS energy level.
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
        mfcc   = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop, n_mfcc=13)
        feat   = np.vstack([chroma, mfcc])
        k      = max(4, min(12, int(duration / 30)))  # ~1 boundary per 30s
        bound_frames = librosa.segment.agglomerative(feat, k=k)
        bound_times  = librosa.frames_to_time(bound_frames, sr=sr, hop_length=hop)
        seg_bounds   = np.concatenate([[0.0], bound_times, [float(duration)]])
        _segs: list[dict] = []
        for _si in range(len(seg_bounds) - 1):
            _ts, _te = float(seg_bounds[_si]), float(seg_bounds[_si + 1])
            _mask    = (rms_times >= _ts) & (rms_times < _te)
            _segs.append({"start": _ts, "end": _te,
                          "rms": float(rms_smooth[_mask].mean()) if _mask.any() else 0.0})
        _rms_v = np.array([s["rms"] for s in _segs])
        if _rms_v.max() > _rms_v.min():
            _rms_v = (_rms_v - _rms_v.min()) / (_rms_v.max() - _rms_v.min())
        for _si, _seg in enumerate(_segs):
            _seg["energy"] = float(_rms_v[_si])
        segments = _segs
    except Exception as _seg_err:
        segments = []

    # Boost segment energy at chorus regions detected via WhisperX repeated lyrics.
    # Additive+multiplicative so even low-RMS choruses (e.g. soft intro chorus) pull up.
    _chorus_regions = _get_chorus_whisperx(music_path)
    if _chorus_regions and segments:
        _boosted = 0
        for seg in segments:
            seg_mid = (seg["start"] + seg["end"]) / 2
            for c_start, c_end in _chorus_regions:
                if c_start <= seg_mid < c_end:
                    seg["energy"] = min(1.0, seg["energy"] * 1.3 + 0.25)
                    _boosted += 1
                    break
        if _boosted:
            print(f"  [WhisperX] boosted {_boosted}/{len(segments)} segments")

    print(f" {duration:.1f}s  {tempo:.0f} BPM  {len(beat_times)} beats  "
          f"section_e=[{section_energy.min():.2f}..{section_energy.max():.2f}]"
          f"  segs={len(segments)}")
    return {
        "duration":         duration,
        "tempo":            tempo,
        "beat_times":       beat_times,
        "beat_energy":      beat_energy.tolist(),
        "onset_energy":     onset_at_beats.tolist(),
        "harm_energy":      harm_at_beats.tolist(),
        "section_energy":   section_energy.tolist(),
        "onset_env_perc":   onset_env_perc_norm,
        "segments":         segments,
        "sr":               sr,
        "hop":              hop,
    }


# ── Cut schedule ──────────────────────────────────────────────────────────────

def _build_schedule_peaks(
    onset_env_perc: list[float],
    beat_times: list[float],
    section_energy: list[float] | None,
    min_shot_sec: float,
    max_shot_sec: float,
    sr: int,
    hop: int,
) -> list[dict]:
    """
    Peak-based auto schedule: cut points driven by percussive onset peaks
    (HPSS-isolated), not a beat grid. Catches sparse "dum dum" events at exact
    timestamps regardless of beat alignment.
    """
    import librosa

    oep = np.array(onset_env_perc, dtype=float)
    # onset_detect with backtrack=True: snaps detected onset BACK to the local energy
    # minimum just before the peak — i.e. the exact moment the transient starts.
    # This is what the ear hears as "the beat", not the peak maximum.
    min_wait = max(1, int(min_shot_sec * sr / hop))
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=oep,
        sr=sr,
        hop_length=hop,
        backtrack=True,
        delta=0.06,        # sensitivity: 6% above baseline to qualify as onset
        wait=min_wait,
        units="frames",
    )
    peak_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop)
    # Fake props for energy (onset strength at each detected frame)
    props = {"peak_heights": oep[onset_frames].tolist() if len(onset_frames) else []}

    track_dur = beat_times[-1] + (beat_times[-1] - beat_times[-2]) if len(beat_times) > 1 else beat_times[-1] + 1.0
    cut_times = np.concatenate([[0.0], peak_times, [track_dur]])

    _bt = np.array(beat_times)
    _se = np.array(section_energy) if section_energy else None

    schedule: list[dict] = []
    for j in range(len(cut_times) - 1):
        t_start = float(cut_times[j])
        t_end   = float(cut_times[j + 1])
        dur = t_end - t_start
        if dur < 0.4:
            continue
        # Section energy at slot start for clip matching (high energy → dynamic clip)
        if _se is not None and len(_bt) > 0:
            idx = int(np.searchsorted(_bt, t_start))
            idx = min(idx, len(_se) - 1)
            sec = float(_se[idx])
        else:
            sec = 0.5
        # Peak strength at cut point (j>0 → peaks[j-1])
        peak_strength = float(props["peak_heights"][j - 1]) if j > 0 and j - 1 < len(props.get("peak_heights", [])) else sec
        schedule.append({
            "start":    t_start,
            "end":      t_end,
            "duration": dur,
            "energy":   max(sec, peak_strength),
            "n_beats":  max(1, round(dur * 60.0 / (60.0 / max(1.0, float(_bt[1] - _bt[0]) * 60.0)) if len(_bt) > 1 else dur)),
        })

    if schedule:
        durs = [s["duration"] for s in schedule]
        print(f"  Schedule (peaks): {len(schedule)} slots  {len(onset_frames)} cut pts  "
              f"min={min(durs):.1f}s  max={max(durs):.1f}s  avg={sum(durs)/len(durs):.1f}s  "
              f"total={schedule[-1]['end']:.1f}s")
    return schedule


def _build_schedule_segments(
    beat_times: list[float],
    segments: list[dict],
    beats_ultra_fast: int,
    beats_fast: int,
    beats_mid: int,
    beats_slow: int,
    section_energy: list[float] | None = None,
    perc_energy: list[float] | None = None,
    harm_energy: list[float] | None = None,
) -> list[dict]:
    """
    Segment-aware beat-grid schedule.
    Blend: 35% segment context (verse/chorus) + 15% section RMS + 50% event energy.
    Event energy = max(percussive onset, harmonic onset) — catches both kick drums
    and vocal attacks (e.g. "the best") so cuts land on musically meaningful moments.
    """
    import numpy as _np

    def _seg_energy(t: float) -> float:
        for seg in segments:
            if seg["start"] <= t < seg["end"]:
                return seg["energy"]
        return segments[-1]["energy"] if segments else 0.5

    n = len(beat_times)
    seg_e_arr = _np.array([_seg_energy(beat_times[i]) for i in range(n)])

    def _norm(lst):
        a = _np.array(lst[:n], dtype=float)
        lo, hi = a.min(), a.max()
        return (a - lo) / (hi - lo) if hi > lo else _np.full(n, 0.5)

    se_norm   = _norm(section_energy) if section_energy and len(section_energy) >= n else _np.full(n, 0.5)
    perc_norm = _norm(perc_energy)    if perc_energy    and len(perc_energy)    >= n else _np.full(n, 0.5)
    harm_norm = _norm(harm_energy)    if harm_energy    and len(harm_energy)    >= n else _np.full(n, 0.5)

    # event_energy: max of percussive (kick/snare) and harmonic (vocal/guitar attack)
    event = _np.maximum(perc_norm, harm_norm)
    blended = 0.35 * seg_e_arr + 0.15 * se_norm + 0.50 * event

    # Thresholds tuned for screen-time balance at high BPM:
    # 35% beats→ultra, 25%→fast, 25%→mid, 15%→slow
    # (proportional timeline: ultra slots are narrow so use more of them)
    _p_ultra = float(_np.percentile(blended, 65))
    _p_fast  = float(_np.percentile(blended, 40))
    _p_slow  = float(_np.percentile(blended, 15))

    schedule: list[dict] = []
    i = 0
    while i < n - 1:
        energy = float(blended[i])
        if energy > _p_ultra:
            n_beats = beats_ultra_fast
        elif energy > _p_fast:
            n_beats = beats_fast
        elif energy < _p_slow:
            n_beats = beats_slow
        else:
            n_beats = beats_mid
        end_i = min(i + n_beats, n - 1)
        dur   = beat_times[end_i] - beat_times[i]
        if dur >= 0.4:
            schedule.append({
                "start":    beat_times[i],
                "end":      beat_times[end_i],
                "duration": dur,
                "energy":   energy,
                "n_beats":  n_beats,
            })
        i = end_i

    if schedule:
        durs = [s["duration"] for s in schedule]
        n_u = sum(1 for s in schedule if s["n_beats"] == beats_ultra_fast)
        n_f = sum(1 for s in schedule if s["n_beats"] == beats_fast)
        n_m = sum(1 for s in schedule if s["n_beats"] == beats_mid)
        n_s = sum(1 for s in schedule if s["n_beats"] == beats_slow)
        print(f"  Schedule (segments): {len(schedule)} slots  "
              f"min={min(durs):.1f}s  max={max(durs):.1f}s  avg={sum(durs)/len(durs):.1f}s  "
              f"total={schedule[-1]['end']:.1f}s  "
              f"ultra={n_u}  fast={n_f}  mid={n_m}  slow={n_s}")
    return schedule


def build_schedule(
    beat_times: list[float],
    beat_energy: list[float],
    beats_ultra_fast: int = 2,
    beats_fast: int = 3,
    beats_mid: int = 4,
    beats_slow: int = 6,
    auto: bool = False,
    min_shot_sec: float = 1.5,
    max_shot_sec: float = 8.0,
    section_energy: list[float] | None = None,
    onset_energy: list[float] | None = None,
    onset_env_perc: list[float] | None = None,
    sr: int = 22050,
    hop: int = 512,
) -> list[dict]:
    """
    Group consecutive beats into shot slots.
    Auto mode: peak-based cutting from percussive HPSS envelope (onset_env_perc).
    Manual mode: per-beat PLP energy thresholds → fixed beats/shot counts.
    """
    import math as _m

    # Auto mode → peak-based cutting (ignores beat grid)
    if auto and onset_env_perc:
        return _build_schedule_peaks(onset_env_perc, beat_times, section_energy,
                                     min_shot_sec, max_shot_sec, sr, hop)

    _sec_e   = section_energy or beat_energy
    _onset_e = onset_energy   or beat_energy

    # Percentile-based thresholds so energy tiers distribute across the actual
    # dynamic range of the track (flat-energy rock gets variety too).
    import numpy as _np
    _be = _np.array(beat_energy)
    _p85 = float(_np.percentile(_be, 85))
    _p65 = float(_np.percentile(_be, 65))
    _p35 = float(_np.percentile(_be, 35))

    schedule: list[dict] = []
    n = len(beat_times)
    i = 0
    while i < n - 1:
        energy = _sec_e[i]  # section_energy (4s RMS) reflects musical structure better than PLP
        if energy > _p85:
            n_beats = beats_ultra_fast
        elif energy > _p65:
            n_beats = beats_fast
        elif energy < _p35:
            n_beats = beats_slow
        else:
            n_beats = beats_mid
        end_i = min(i + n_beats, n - 1)
        dur = beat_times[end_i] - beat_times[i]
        if dur >= 0.4:
            schedule.append({
                "start":    beat_times[i],
                "end":      beat_times[end_i],
                "duration": dur,
                "energy":   float(energy),
                "n_beats":  n_beats,
            })
        i = end_i

    if schedule:
        n_ultra = sum(1 for s in schedule if s["n_beats"] == beats_ultra_fast)
        n_fast  = sum(1 for s in schedule if s["n_beats"] == beats_fast)
        n_mid   = sum(1 for s in schedule if s["n_beats"] == beats_mid)
        n_slow  = sum(1 for s in schedule if s["n_beats"] == beats_slow)
        print(f"  Schedule: {len(schedule)} slots  "
              f"ultra={n_ultra}({beats_ultra_fast}b)  fast={n_fast}({beats_fast}b)  "
              f"mid={n_mid}({beats_mid}b)  slow={n_slow}({beats_slow}b)  "
              f"total={schedule[-1]['end']:.1f}s")
    return schedule


# ── Motion analysis ───────────────────────────────────────────────────────────

def motion_profile(clip_path: Path, duration: float, ffmpeg_bin: str = "ffmpeg",
                   n_samples: int = 24) -> tuple[float, float]:
    """
    (motion_peak_time, mean_motion_level) for a clip via ffmpeg frame diff.
    No cv2 / libGL dependency.
    """
    if duration < 0.5:
        return 0.0, 0.0
    W, H = 160, 90
    target_fps = n_samples / duration
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-loglevel", "error",
             "-i", str(clip_path),
             "-vf", f"fps={target_fps:.6f},scale={W}:{H},format=gray",
             "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=30,
        )
    except Exception:
        return duration * 0.3, 0.0

    raw = r.stdout
    frame_size = W * H
    n_frames = len(raw) // frame_size
    if n_frames < 2:
        return duration * 0.3, 0.0

    frames = [
        np.frombuffer(raw[i * frame_size:(i + 1) * frame_size], dtype=np.uint8).astype(np.float32)
        for i in range(n_frames)
    ]
    diffs = [
        ((i / n_frames) * duration, float(np.mean(np.abs(frames[i] - frames[i - 1]))))
        for i in range(1, n_frames)
    ]
    diff_vals = [d for _, d in diffs]
    peak_t = diffs[int(np.argmax(diff_vals))][0]
    return peak_t, float(np.mean(diff_vals))


def analyse_clips(autocut_dir: Path, scene_scores: dict,
                  top_percent: float, ffprobe: str,
                  stem_to_camera: dict | None = None,
                  stem_to_time: dict | None = None) -> list[dict]:
    """
    Compute motion profiles for the top_percent% of CLIP-scored clips.
    Returns list sorted by CLIP score descending.
    """
    sorted_scenes = sorted(scene_scores.items(), key=lambda x: x[1], reverse=True)
    cutoff     = max(1, int(len(sorted_scenes) * top_percent))
    candidates = list(sorted_scenes[:cutoff])
    print(f"  Motion pass: top {top_percent*100:.0f}% → {len(candidates)}/{len(sorted_scenes)} clips")

    # Guarantee each camera source has at least MIN_PER_SOURCE candidates
    # (prevents low-scoring cameras like drone from being completely excluded)
    _MIN_PER_SOURCE = 3
    _in_candidates = {s for s, _ in candidates}
    _by_source: dict[str, list] = {}
    for scene, score in sorted_scenes:
        _by_source.setdefault(_clip_source(scene), []).append((scene, score))
    _rescued = 0
    for src, scenes in _by_source.items():
        _count = sum(1 for s, _ in scenes if s in _in_candidates)
        for scene, score in scenes:
            if _count >= _MIN_PER_SOURCE:
                break
            if scene not in _in_candidates:
                candidates.append((scene, score))
                _in_candidates.add(scene)
                _count += 1
                _rescued += 1
    if _rescued:
        print(f"  Per-source rescue: +{_rescued} clips to ensure {_MIN_PER_SOURCE}/source minimum")

    def _analyse_one(item):
        i, (scene, score) = item
        clip_path = autocut_dir / f"{scene}.mp4"
        if not clip_path.exists():
            return None
        try:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(clip_path)],
                capture_output=True, text=True, timeout=5
            )
            clip_dur = float(r.stdout.strip())
        except Exception:
            clip_dur = 0.0
        if clip_dur < 0.5:
            return None
        ffmpeg_bin = str(Path(ffprobe).parent / "ffmpeg")
        peak_t, motion_lvl = motion_profile(clip_path, duration=clip_dur, ffmpeg_bin=ffmpeg_bin)
        src = _clip_source(scene)
        return {
            "scene":          scene,
            "score":          score,
            "path":           clip_path,
            "duration":       clip_dur,
            "motion_peak":    peak_t,
            "motion_level":   motion_lvl,
            "camera":         stem_to_camera.get(src, "unknown") if stem_to_camera else "unknown",
            "clip_time_norm": stem_to_time.get(src) if stem_to_time else None,
        }

    clips: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {pool.submit(_analyse_one, item): item for item in enumerate(candidates)}
        for fut in as_completed(futures):
            done += 1
            result = fut.result()
            if result:
                clips.append(result)
            if done % 25 == 0:
                print(f"    {done}/{len(candidates)}…")
    # Restore original score order (as_completed is unordered)
    clips.sort(key=lambda c: c["score"], reverse=True)

    # Normalise motion_level to [0, 1]
    if clips:
        ml = [c["motion_level"] for c in clips]
        lo, hi = min(ml), max(ml)
        for c in clips:
            c["motion_norm"] = (c["motion_level"] - lo) / (hi - lo + 1e-6)

    print(f"  Clips ready: {len(clips)}")
    return clips


# ── Matching ──────────────────────────────────────────────────────────────────

import re as _re

def _clip_source(scene: str) -> str:
    """Base source file stem: strip trailing -(clip|scene)-NNN."""
    return _re.sub(r'-(clip|scene)-\d+$', '', scene)


def _parse_cam_pattern(pattern: str, cameras: list[str]) -> list[str] | None:
    """
    Parse a camera pattern string like "aabaab" into a list of camera names.
    Letters mapped in order of first appearance to cameras list (as provided — respects cam_a/cam_b order).
    e.g. pattern="aabaab", cameras=["helmet","back"] → ["helmet","helmet","back","helmet","helmet","back"]
         (a=helmet=cam_a, b=back=cam_b)
    Returns None if pattern is empty or fewer than 2 cameras available.
    """
    pattern = pattern.strip().lower()
    if not pattern or len(cameras) < 2:
        return None
    # Build letter→camera mapping: first unique letter = cameras[0], second = cameras[1], etc.
    letter_order: list[str] = []
    for ch in pattern:
        if ch not in letter_order:
            letter_order.append(ch)
    letter_to_cam = {letter_order[i]: cameras[i]
                     for i in range(min(len(letter_order), len(cameras)))}
    resolved = [letter_to_cam.get(ch) for ch in pattern]
    if any(r is None for r in resolved):
        return None
    return resolved  # type: ignore[return-value]


def match_clips(schedule: list[dict], clips: list[dict],
                chron_weight: float = 0.0,
                cam_pattern: str = "",
                cam_order: list[str] | None = None,
                max_consecutive_cam: int = 3,
                gps_weight: float = 0.0,
                mood_weight: float = 0.0) -> list[dict]:
    """
    Assign best clip to each slot.
    Scoring per candidate (when chron_weight=0):
        CLIP_score × 0.60  +  energy_match × 0.40
    With chronological arc (chron_weight=0.20):
        CLIP_score × 0.50  +  energy_match × 0.30  +  chron_match × 0.20
    Camera diversity:
        cam_pattern set → cyclic pattern (e.g. "aabaab" → back/back/helmet repeating)
        cam_pattern empty → group-based (2-3 shots per camera, then switch)
    Source diversity: avoid repeating same source file within rolling window.
    """
    import collections
    used: set[str] = set()
    reuse_used: set[str] = set()   # rotates through clips when pool exhausted
    edit: list[dict] = []

    num_sources  = len({_clip_source(c["scene"]) for c in clips})
    # Use cam_order from config (cam_a first) — fall back to alphabetical
    _cam_set = {c.get("camera", "unknown") for c in clips}
    if cam_order:
        cameras = [c for c in cam_order if c in _cam_set] + \
                  sorted(c for c in _cam_set if c not in cam_order)
    else:
        cameras = sorted(_cam_set)
    num_cameras  = len(cameras)
    total_music_dur = schedule[-1]["end"] if schedule else 1.0
    # Only use chronological arc if clips actually have time info
    _has_time = any(c.get("clip_time_norm") is not None for c in clips)
    _chron_w  = chron_weight if _has_time else 0.0
    if _chron_w > 0:
        _timed = sum(1 for c in clips if c.get("clip_time_norm") is not None)
        print(f"  Chronological arc active: weight={_chron_w:.2f}  "
              f"timed clips={_timed}/{len(clips)}")

    # Rolling window: at least 2 sources worth of slots, minimum 4
    _src_window = max(4, num_sources * 2)
    recent_sources: collections.deque = collections.deque(maxlen=_src_window)

    # Camera selection: explicit pattern (user override) or diversity-cap (default).
    _resolved_pattern = _parse_cam_pattern(cam_pattern, cameras)
    _use_diversity_cap = False
    if _resolved_pattern:
        print(f"  Camera pattern: '{cam_pattern}' → {_resolved_pattern[:8]}… "
              f"(repeating every {len(_resolved_pattern)} slots)")
    elif num_cameras >= 2:
        # Diversity-cap: score-driven selection, block same camera after max_consecutive_cam hits.
        # Proportions emerge naturally from clip counts — no hard alternation.
        _use_diversity_cap = True
        print(f"  Camera mode: diversity-cap (max {max_consecutive_cam} consecutive, "
              f"cameras: {cameras})")
    else:
        print(f"  Camera pattern: none (score-driven, {num_cameras} camera(s))")
    _slot_idx = 0   # counts placed slots (for pattern indexing)
    _last_cam: str | None = None
    _consecutive_cam: int = 0

    for slot in schedule:
        dur    = slot["duration"]
        energy = slot["energy"]

        _desired_cam = (_resolved_pattern[_slot_idx % len(_resolved_pattern)]
                        if _resolved_pattern else None)
        # Diversity-cap: when consecutive limit hit, exclude that camera until a switch occurs.
        _cap_cam = (_last_cam
                    if _use_diversity_cap and _consecutive_cam >= max_consecutive_cam
                    else None)

        def _pool(relax_dur: bool = False,
                  camera_filter: bool = True,
                  source_filter: bool = True) -> list[dict]:
            min_dur = dur if relax_dur else dur + 0.2
            return [
                c for c in clips
                if c["duration"] >= min_dur
                and c["scene"] not in used
                and (not source_filter or _clip_source(c["scene"]) not in recent_sources)
                and (not camera_filter or _desired_cam is None
                     or c.get("camera", "unknown") == _desired_cam)
                and (not camera_filter or _cap_cam is None
                     or c.get("camera", "unknown") != _cap_cam)
            ]

        pool = _pool()
        if not pool: pool = _pool(relax_dur=True)
        if not pool: pool = _pool(source_filter=False)
        if not pool: pool = _pool(relax_dur=True, source_filter=False)
        if not pool: pool = _pool(camera_filter=False)
        if not pool: pool = _pool(relax_dur=True, camera_filter=False)
        if not pool:
            pool = [c for c in clips if c["duration"] >= dur and c["scene"] not in used]
        # Pool exhausted — allow reuse rather than leaving slots empty,
        # but rotate through clips so the same clip isn't repeated consecutively.
        # IMPORTANT: only clear reuse_used when clips that MEET the duration
        # requirement have all been used.  When NO clip meets the duration
        # (e.g. CLIP_DUR_SEC shorter than the music slot), do NOT clear here —
        # fall through to the duration-relaxed second block which tracks its own
        # rotation via the same reuse_used set.
        _reusing = False
        if not pool:
            reuse_pool = [c for c in clips if c["duration"] >= dur and c["scene"] not in reuse_used]
            if not reuse_pool:
                _dur_eligible = [c for c in clips if c["duration"] >= dur]
                if _dur_eligible:
                    # There are clips with enough duration — rotation completed, reset.
                    reuse_used.clear()
                    reuse_pool = _dur_eligible
                # else: no clip meets duration constraint — leave reuse_used intact,
                # fall through to the relaxed-duration block below.
            pool = reuse_pool
            _reusing = bool(pool)
        if not pool:
            reuse_pool = [c for c in clips if c["scene"] not in reuse_used]
            if not reuse_pool:
                reuse_used.clear()
                reuse_pool = list(clips)
            pool = reuse_pool
            _reusing = bool(pool)
        if not pool:
            continue

        def rank(c: dict) -> float:
            motion_match = 1.0 - abs(energy - c.get("motion_norm", 0.5))
            # GPS bonus: high speed/turn clips score higher; max contribution = gps_weight * 0.3
            _gps = c.get("gps_norm", 0.0) * gps_weight * 0.3
            # Mood score: interpolate action↔scenic by slot energy
            # energy=1.0 (chorus/ultra) → action clip; energy=0.0 (verse/slow) → scenic clip
            _act = c.get("action_score", float("nan"))
            _sce = c.get("scenic_score", float("nan"))
            _has_mood = mood_weight > 0 and _act == _act and _sce == _sce  # nan-safe
            if _chron_w > 0 and c.get("clip_time_norm") is not None:
                music_pos   = slot["start"] / total_music_dur
                chron_match = 1.0 - abs(music_pos - c["clip_time_norm"])
                if _has_mood:
                    _mood = energy * _act + (1.0 - energy) * _sce
                    return (_mood * 0.35 + motion_match * 0.25
                            + chron_match * _chron_w + c["score"] * 0.20 + _gps)
                return (c["score"] * 0.50 + motion_match * 0.30 + chron_match * _chron_w + _gps)
            if _has_mood:
                _mood = energy * _act + (1.0 - energy) * _sce
                return _mood * 0.45 + motion_match * 0.30 + c["score"] * 0.25 + _gps
            # Fallback (no mood scores): original formula
            if energy > 0.65:
                return c["score"] * 0.45 + motion_match * 0.55 + _gps
            return c["score"] * 0.60 + motion_match * 0.40 + _gps

        best = max(pool, key=rank)
        if not _reusing:
            used.add(best["scene"])
        else:
            reuse_used.add(best["scene"])
        recent_sources.append(_clip_source(best["scene"]))
        _best_cam = best.get("camera", "unknown")
        if _best_cam == _last_cam:
            _consecutive_cam += 1
        else:
            _last_cam  = _best_cam
            _consecutive_cam = 1
        _slot_idx += 1

        # Motion anchor: place peak_motion at ~30% into the slot so the
        # "climax" of the action lands just after the beat hit
        anchor = dur * 0.3
        ideal_ss = best["motion_peak"] - anchor
        ss = max(0.0, min(ideal_ss, best["duration"] - dur))

        edit.append({
            "music_start":    slot["start"],
            "duration":       dur,
            "energy":         energy,
            "n_beats":        slot["n_beats"],
            "scene":          best["scene"],
            "clip_path":      str(best["path"]),
            "clip_ss":        round(ss, 3),
            "clip_total_dur": round(best["duration"], 3),
            "clip_score":     round(best["score"], 4),
            "motion_peak":    round(best["motion_peak"], 3),
            "camera":         best.get("camera", "unknown"),
        })

    covered = sum(e["duration"] for e in edit)
    unique  = len({e["scene"] for e in edit})
    # Camera distribution summary
    cam_counts: dict[str, int] = {}
    for c in clips:
        cam = c.get("camera", "unknown")
        if c["scene"] in {e["scene"] for e in edit}:
            cam_counts[cam] = cam_counts.get(cam, 0) + 1
    cam_str = "  ".join(f"{k}={v}" for k, v in sorted(cam_counts.items()))
    print(f"{_ts()}  Matched: {len(edit)} slots  {covered:.1f}s  unique={unique}  [{cam_str}]")
    # Log selected scenes for exclusion debugging
    scene_list = [e["scene"] for e in edit]
    print(f"  Scenes: {scene_list[:8]}{'…' if len(scene_list) > 8 else ''}")
    return edit


# ── Rendering ────────────────────────────────────────────────────────────────

def render(edit: list[dict], music_path: Path, music_ss: float,
           output: Path, ffmpeg: str, nvenc: bool = True,
           resolution: str = "", framerate: str = "60",
           color_correct: str = "",
           cam_crop: dict | None = None) -> None:
    """
    Trim each clip to its slot duration, concat, overlay music.
    Uses NVENC if available (detected by nvenc flag).
    resolution: e.g. "3840:2160" — scale all clips to this; empty = preserve source.
    color_correct: optional ffmpeg filter chain appended after scale/fps (per-project grading).
    """
    enc_v = (
        ["-c:v", "h264_nvenc", "-rc", "constqp", "-qp", "18", "-preset", "p4",
         "-profile:v", "high", "-pix_fmt", "yuv420p", "-bf", "0"]
        if nvenc else
        ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p", "-bf", "0"]
    )

    # Build -vf filter: normalise resolution + fps so all clips are compatible for concat
    _cam_crop_map = cam_crop or {}

    def _build_vf(camera: str = "") -> tuple[str, list[str]]:
        """Return (_vf string, vf_args list) for a given camera name."""
        if resolution:
            w, h = resolution.split(":")
            _use_crop = _cam_crop_map.get(camera)
            if not _use_crop and camera in ("", "unknown") and len(_cam_crop_map) == 1:
                _use_crop = next(iter(_cam_crop_map.values()))
            if _use_crop:
                _scale = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                          f"crop={w}:{h}")
            else:
                _scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
            vf = f"{_scale},setsar=1,fps={framerate}"
            if color_correct:
                vf += "," + color_correct
            return vf, ["-vf", vf]
        elif color_correct:
            return color_correct, ["-vf", color_correct]
        return "", []

    # Default (no camera info) — used for photos and privacy filtergraph prefix
    _vf, vf_args = _build_vf("")

    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(_tmp)

        _fps_int = int(framerate) if framerate else 60

        # Pre-compute frame-snapped durations using cumulative correction.
        # Independent per-clip rounding (round(dur*fps)/fps) accumulates ±0.5-frame
        # errors across 50+ clips → up to 400ms drift. Cumulative approach keeps
        # total frame count exact: each clip gets exactly the frames needed so the
        # running total matches round(cumulative_target * fps).
        _accum_frames = 0
        _cumul_target = 0.0
        for _e in edit:
            _cumul_target += _e["duration"]
            _ideal_total = round(_cumul_target * _fps_int)
            _this_frames = max(1, _ideal_total - _accum_frames)
            _e["_snapped_dur"] = _this_frames / _fps_int
            _accum_frames = _ideal_total

        # Parallel encode slots — NVENC session cap ~3-5; shorts may hold 1-3 slots concurrently
        trim_workers = 2 if nvenc else _WORKERS
        total_clips = len(edit)
        import queue as _trim_q, threading as _thr
        _slot_pool = _trim_q.Queue()
        for _s in range(trim_workers):
            _slot_pool.put(_s)
        _done_lock = _thr.Lock()
        _done_count = [0]

        def _trim_one(args):
            i, entry = args
            out = tmp / f"s{i:04d}.mp4"
            dur = entry.get("_snapped_dur", round(entry["duration"] * _fps_int) / _fps_int)

            # Photo slot: still image → video with fade in/out
            if entry.get("type") == "photo":
                fade_dur = min(0.4, dur * 0.2)
                if resolution:
                    w, h = resolution.split(":")
                    _photo_vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                                 f"fps={framerate},"
                                 f"fade=t=in:st=0:d={fade_dur},"
                                 f"fade=t=out:st={dur - fade_dur:.3f}:d={fade_dur}")
                else:
                    _photo_vf = (f"fps={framerate},"
                                 f"fade=t=in:st=0:d={fade_dur},"
                                 f"fade=t=out:st={dur - fade_dur:.3f}:d={fade_dur}")
                cmd = [
                    ffmpeg, "-y",
                    "-loop", "1", "-t", str(dur),
                    "-i", entry["path"],
                    "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
                    "-t", str(dur),
                    "-vf", _photo_vf,
                    *enc_v,
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                    "-map", "0:v", "-map", "1:a",
                    "-map_metadata", "-1",
                    str(out)
                ]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0 or not out.exists():
                    print(f"  WARN: photo encode failed for {entry['path']}\n{r.stderr}", flush=True)
                    return (i, None, 0.0)
                return (i, out, dur)

            _entry_vf, _entry_vf_args = _build_vf(entry.get("camera", ""))
            vf_final = _entry_vf_args

            cmd = [
                ffmpeg, "-y",
                "-ss", str(entry["clip_ss"]),
                "-t",  str(dur),
                "-avoid_negative_ts", "make_zero",
                "-i",  entry["clip_path"],
                *enc_v, *vf_final,
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                "-map_metadata", "-1",
                str(out)
            ]
            slot = _slot_pool.get()
            try:
                print(f"WORKER_START {slot} {i + 1}/{total_clips}", flush=True)
                total_frames = max(1, round(dur * float(framerate)))
                cmd_p = cmd[:-1] + ["-progress", "pipe:1", "-loglevel", "error", cmd[-1]]
                proc = subprocess.Popen(cmd_p, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                last_pct = -1
                _stderr = ""
                try:
                    for _pl in proc.stdout:
                        _pl = _pl.strip()
                        if _pl.startswith("frame="):
                            try:
                                frame = int(_pl.split("=")[1].strip())
                                pct = min(99, round(frame / total_frames * 100))
                                if pct != last_pct:
                                    last_pct = pct
                                    print(f"WORKER_PROGRESS {slot} {pct} {i + 1}/{total_clips}",
                                          flush=True)
                            except ValueError:
                                pass
                    proc.wait()
                    _stderr = proc.stderr.read()
                finally:
                    pass
                print(f"WORKER_DONE {slot}", flush=True)
                if proc.returncode != 0 or not out.exists():
                    print(f"  WARN: trim failed for {entry['scene']}\n{_stderr}", flush=True)
                    return (i, None, 0.0)
                with _done_lock:
                    _done_count[0] += 1
                    _dc = _done_count[0]
                # Use _snapped_dur (= n_frames/fps) as duration — NOT probed format=duration
                # which includes container overhead > last PTS → freeze frame in concat.
                print(f"  [{_dc}/{total_clips}] Clip encoding", flush=True)
                return (i, out, dur)
            finally:
                _slot_pool.put(slot)

        results = {}
        with ThreadPoolExecutor(max_workers=trim_workers) as pool:
            for i, out, actual_dur in pool.map(_trim_one, list(enumerate(edit))):
                results[i] = (out, actual_dur)

        trimmed: list[tuple[Path, float]] = []
        for i in range(len(edit)):
            out, actual_dur = results.get(i, (None, 0.0))
            if out:
                trimmed.append((out, actual_dur))
            else:
                print(f"  WARN: trim failed for {edit[i]['scene']}")

        if not trimmed:
            raise RuntimeError("All clip trims failed")

        print(f"{_ts()}  Trimmed {len(trimmed)}/{len(edit)} clips")

        # Concat video-only
        clist = tmp / "list.txt"
        # duration directive uses _snapped_dur (n_frames/fps) — prevents timestamp drift
        # across 50+ clips. Must NOT use probed format=duration (container overhead > PTS).
        clist_lines = []
        for p, _dur in trimmed:
            clist_lines.append(f"file '{p}'")
            clist_lines.append(f"duration {_dur:.6f}")
        clist.write_text("\n".join(clist_lines))
        vid = tmp / "vid.mp4"
        _cr = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(clist),
             "-c", "copy", str(vid)],
            capture_output=True, text=True
        )
        if _cr.returncode != 0:
            print(f"  WARN: concat copy failed (rc={_cr.returncode}), retrying with re-encode\n{_cr.stderr[-2000:]}", flush=True)
            _cr2 = subprocess.run(
                [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(clist),
                 "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                 "-c:a", "aac", "-b:a", "192k", str(vid)],
                capture_output=True, text=True
            )
            if _cr2.returncode != 0:
                raise RuntimeError(f"concat failed (rc={_cr2.returncode}):\n{_cr2.stderr[-2000:]}")

        # Probe actual duration
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(vid)],
            capture_output=True, text=True
        )
        video_dur = float(r.stdout.strip()) if r.stdout.strip() else sum(e["duration"] for e in edit)

        # Output the concatenated video as is (now contains audio)
        shutil.move(str(vid), str(output))

    print(f"{_ts()}  → {output.name}  ({video_dur:.1f}s  {len(trimmed)} shots)")


# ── Photo slot insertion ──────────────────────────────────────────────────────

def _insert_photos(edit: list[dict], photo_paths: list[str]) -> list[dict]:
    """Evenly distribute photo stills through the edit list and recalculate music_start."""
    if not photo_paths or not edit:
        return edit
    n = len(edit)
    result = list(edit)
    positions = [int((i + 1) * n / (len(photo_paths) + 1)) for i in range(len(photo_paths))]
    for pos, path in sorted(zip(positions, photo_paths), key=lambda x: x[0], reverse=True):
        result.insert(pos, {
            "type":       "photo",
            "path":       path,
            "duration":   2.5,
            "energy":     0.5,
            "clip_score": 1.0,
            "music_start": 0.0,
            "scene":      f"photo_{pos}",
        })
    t = 0.0
    for slot in result:
        slot["music_start"] = round(t, 3)
        t += slot["duration"]
    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def assemble(
    work_dir:       Path,
    music_path:     Path,
    output:         Path | None    = None,
    top_percent:    float          = 0.40,
    ffmpeg:         str            = "ffmpeg",
    ffprobe:        str            = "ffprobe",
    nvenc:          bool           = True,
    dry_run:        bool           = False,
    use_saved_sequence: bool       = False,
) -> Path:
    import configparser as _cp_mod
    _cp = _cp_mod.ConfigParser()
    _cp.read([
        str(Path(__file__).parent.parent / "config.ini"),
        str(work_dir / "config.ini"),
        str(Path(__file__).parent.parent / "webapp" / "config.ini"),
    ])
    # Auto-detect resolution from first clip in autocut/ — avoids unnecessary upscaling
    # when source footage is 1080p. Config override still works when set explicitly.
    _resolution_cfg = _cp.get("video", "resolution", fallback="")
    if _resolution_cfg:
        _resolution = _resolution_cfg
    else:
        _autocut_dir_probe = work_dir / "_autoframe" / "autocut"
        _resolution = "1920:1080"  # safe default
        _probe_clip = next(iter(sorted(_autocut_dir_probe.glob("*.mp4"))), None) if _autocut_dir_probe.exists() else None
        if _probe_clip:
            try:
                import subprocess as _sp2
                _pr = _sp2.run([ffprobe, "-v", "quiet", "-show_entries", "stream=width,height",
                                "-of", "csv=p=0", str(_probe_clip)],
                               capture_output=True, text=True, timeout=5)
                _dims = [l for l in _pr.stdout.strip().splitlines() if l.strip()]
                if _dims:
                    _w, _h = _dims[0].split(",")[:2]
                    _resolution = f"{_w.strip()}:{_h.strip()}"
            except Exception:
                pass
    _framerate   = _cp.get("video",        "framerate",   fallback="60")
    _cam_pattern        = _cp.get("music_driven", "cam_pattern",         fallback="")
    _max_consecutive_cam = int(_cp.get("music_driven", "max_consecutive_cam", fallback="3"))
    # Per-camera 4:3→16:9 center-crop map.
    _cam_crop_16x9: dict = {}
    if _cp.has_section("cam_crop_16x9"):
        _cam_crop_16x9 = {k: v.strip() in ("1", "true", "yes")
                          for k, v in _cp.items("cam_crop_16x9") if v.strip()}
    # Per-project color correction (applied during per-clip re-encode).
    # Built from [color_correct] sliders (brightness/gamma/contrast/saturation/temperature),
    # with legacy vf_chain= still honoured as a manual override.
    from color_correct import chain_from_cp as _cc_chain
    _color_correct   = _cc_chain(_cp)
    # Camera order from config: cam_a = 'a', cam_b = 'b' in pattern.
    # Falls back to alphabetical if not configured.
    _cam_a = _cp.get("job", "cam_a", fallback="")
    _cam_b = _cp.get("job", "cam_b", fallback="")
    _cameras_raw = _cp.get("job", "cameras", fallback="")
    _cam_order: list[str] = []
    if _cameras_raw:
        _cam_order = [c.strip() for c in _cameras_raw.split(",") if c.strip()]
    elif _cam_a:
        _cam_order = [c for c in [_cam_a, _cam_b] if c]

    auto_dir    = work_dir / "_autoframe"
    autocut_dir = auto_dir / "autocut"

    # ── Saved-sequence path: skip analyse/match, render the manual timeline ──
    # Used when the UI persists user-edited timeline (drag/drop/photos) and the
    # webapp wants the final render to honour that exact ordering.
    if use_saved_sequence:
        seq_path = auto_dir / "preview_sequence.json"
        if not seq_path.exists():
            raise FileNotFoundError(f"--use-saved-sequence: {seq_path} missing")
        seq_data = _json.loads(seq_path.read_text())
        edit = seq_data.get("sequence", [])
        if not edit:
            raise RuntimeError("--use-saved-sequence: empty sequence")

        # Drag-drop in the UI does not refresh per-slot music_start. Reset them
        # cumulatively from 0 so audio aligns with the rendered video.
        _t_run = 0.0
        for slot in edit:
            slot["music_start"] = round(_t_run, 3)
            _t_run += float(slot.get("duration", 0))
        music_ss = 0.0

        # Cap sequence so that clips + intro + outro <= music length.
        # pipeline.py adds intro_dur + outro_dur (default 3s each) after render,
        # so we must leave that headroom here, same as the normal render path.
        try:
            _r = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(music_path)],
                capture_output=True, text=True, timeout=5,
            )
            _music_dur = float(_r.stdout.strip()) if _r.stdout.strip() else 0.0
        except Exception:
            _music_dur = 0.0
        if _music_dur > 0:
            _no_intro_cfg = _cp.getboolean("job", "no_intro", fallback=False)
            _card_dur_cfg = _cp.getfloat("intro_outro", "duration", fallback=3.0) if not _no_intro_cfg else 0.0
            _cap_dur = _music_dur - _card_dur_cfg * 2  # reserve intro + outro
            _cap_dur = max(_cap_dur, _music_dur * 0.9)  # never shave more than 10%
            _accum = 0.0
            _capped = []
            for slot in edit:
                d = float(slot.get("duration", 0))
                if _accum + d > _cap_dur:
                    _last = _cap_dur - _accum
                    if _last > 0.5:
                        _capped.append({**slot, "duration": round(_last, 3)})
                    break
                _capped.append(slot)
                _accum += d
            if len(_capped) < len(edit):
                print(f"  Capped sequence {len(edit)} → {len(_capped)} slots "
                      f"({_accum:.1f}s clips + {_card_dur_cfg*2:.0f}s cards "
                      f"of {_music_dur:.1f}s music)", flush=True)
                edit = _capped

        print(f"  Saved sequence: {len(edit)} slots — bypassing analyse/match", flush=True)
        if output is None:
            output = auto_dir / "highlight_music_driven.mp4"
        render(edit, music_path, music_ss, output, ffmpeg=ffmpeg, nvenc=nvenc,
               resolution=_resolution, framerate=_framerate,
               color_correct=_color_correct, cam_crop=_cam_crop_16x9)
        _music_vol = _cp.getfloat("music", "music_volume", fallback=0.7)
        (auto_dir / "music_info.json").write_text(
            _json.dumps({"music_path": str(music_path), "music_ss": music_ss, "music_vol": _music_vol})
        )
        return output

    # Multicam: use all-cam scores when available
    allcam_csv = auto_dir / "scene_scores_allcam.csv"
    scores_csv = allcam_csv if allcam_csv.exists() else auto_dir / "scene_scores.csv"

    if not scores_csv.exists():
        raise FileNotFoundError("scene_scores.csv not found — run Analyze first")

    # Load manual overrides from gallery UI
    # States: "include" = force-include, "ban" = hard-exclude (never, not even fallback),
    #         "exclude" treated as "ban" for backwards compatibility
    _overrides_path = auto_dir / "manual_overrides.json"
    _manual_banned: set[str] = set()   # never use, hard exclude
    _manual_included: set[str] = set() # force include regardless of threshold
    if _overrides_path.exists():
        try:
            _ov = _json.loads(_overrides_path.read_text())
            _manual_banned   = {k for k, v in _ov.items() if v in ("ban", "ban-new", "exclude")}
            _manual_included = {k for k, v in _ov.items() if v == "include"}
            if _manual_banned:
                print(f"  Banned: {len(_manual_banned)} scene(s) — hard excluded from pool")
        except Exception:
            pass

    # Precompute source→camera and source→absolute epoch (used for exclusion propagation
    # and back-cam filtering below). Done once here, reused in stem_to_time later.
    _interval_s  = float(_cp.get("clip_scan", "interval_sec", fallback="3.0"))
    _clip_dur_s  = float(_cp.get("clip_scan", "clip_dur_sec", fallback="10.0"))
    _src_cam_off: dict[str, float] = {}
    if _cp.has_section("cam_offsets"):
        for _ko, _vo in _cp.items("cam_offsets"):
            try: _src_cam_off[_ko] = float(_vo)
            except ValueError: pass
    _src_cam_map: dict[str, str] = {}
    _cam_src_path = auto_dir / "camera_sources.csv"
    if _cam_src_path.exists():
        with open(_cam_src_path) as _csf:
            for _csr in csv.DictReader(_csf):
                if "source" in _csr and "camera" in _csr:
                    _src_cam_map[_csr["source"]] = _csr["camera"]
    _src_epoch: dict[str, float] = {}
    _vext3 = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts"}
    for _svf in sorted(work_dir.rglob("*")):
        if _svf.suffix.lower() not in _vext3: continue
        if "_autoframe" in _svf.parts: continue
        try:
            _r3 = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries", "format_tags=creation_time",
                 "-of", "csv=p=0", str(_svf)],
                capture_output=True, text=True, timeout=5)
            _ts3 = _r3.stdout.strip()
            if not _ts3: continue
            from datetime import datetime as _dtcls
            _ep3 = _dtcls.fromisoformat(_ts3.replace("Z", "+00:00")).timestamp()
            _cam3 = _src_cam_map.get(_svf.stem, "")
            _src_epoch[_svf.stem] = _ep3 + _src_cam_off.get(_cam3, 0.0)
        except Exception:
            pass

    # Load per-clip offsets from CSV (offset_sec = actual start within source file).
    # Produced by clip_scan.py; absent in older CSVs → fallback to clip_N * interval (buggy).
    _clip_offset: dict[str, float] = {}
    with open(scores_csv) as _foff:
        for _roff in csv.DictReader(_foff):
            _os = _roff.get("offset_sec", "")
            if _os:
                try: _clip_offset[_roff["scene"]] = float(_os)
                except ValueError: pass

    import re as _re3
    def _clip_range(scene_key: str):
        src = _clip_source(scene_key)
        epoch = _src_epoch.get(src)
        if epoch is None: return None
        if scene_key in _clip_offset:
            t0 = epoch + _clip_offset[scene_key]
        else:
            # Fallback for CSVs without offset_sec (pre-fix analyze)
            m = _re3.search(r'-clip-(\d+)$', scene_key)
            if not m: return None
            t0 = epoch + int(m.group(1)) * _interval_s
        return (t0, t0 + _clip_dur_s)

    # Propagate bans to other cameras: banned cam-A clip → ban all cam-B clips
    # overlapping the same absolute time window.
    if _manual_banned and _src_epoch:
        _ban_ranges = [r for s in _manual_banned if (r := _clip_range(s))]
        if _ban_ranges:
            _sync_banned: set[str] = set()
            with open(scores_csv) as _f4:
                for _r4 in csv.DictReader(_f4):
                    _sc4 = _r4.get("scene", "")
                    if _sc4 in _manual_banned: continue
                    _rng4 = _clip_range(_sc4)
                    if not _rng4: continue
                    for (ban_s, ban_e) in _ban_ranges:
                        if _rng4[0] < ban_e and _rng4[1] > ban_s:
                            _sync_banned.add(_sc4)
                            break
            if _sync_banned:
                print(f"  Sync-banned: {len(_sync_banned)} scene(s) from other cameras "
                      f"in banned time windows")
                _manual_banned.update(_sync_banned)

    # Load CLIP scores; hard-exclude banned scenes and negative-dominant scenes
    _all_scores: dict[str, float] = {}
    _gps_raw:    dict[str, tuple[float, float]] = {}
    _mood_raw:   dict[str, tuple[float, float]] = {}
    _neg_excluded = 0
    _BRIGHTNESS_BAN = float(_cp.get("music_driven", "brightness_ban", fallback="50.0") or "50.0")
    _dark_excluded = 0
    with open(scores_csv) as f:
        for row in csv.DictReader(f):
            try:
                scene = row["scene"]
                if scene in _manual_banned:
                    continue
                if row.get("avg_brightness", "") not in ("", "nan"):
                    try:
                        if float(row["avg_brightness"]) < _BRIGHTNESS_BAN:
                            _dark_excluded += 1
                            continue
                    except ValueError:
                        pass
                final = float(row["score"])
                if final < 0:
                    _neg_excluded += 1
                    continue
                _all_scores[scene] = final
                try:
                    _gps_raw[scene] = (
                        float(row.get("gps_speed_max") or 0),
                        float(row.get("gps_turn_max")  or 0),
                    )
                except (ValueError, TypeError):
                    pass
                _a_str  = row.get("action_score", "")
                _sc_str = row.get("scenic_score", "")
                if _a_str not in ("", "nan") and _sc_str not in ("", "nan"):
                    try:
                        _mood_raw[scene] = (float(_a_str), float(_sc_str))
                    except ValueError:
                        pass
            except (KeyError, ValueError):
                pass
    if _dark_excluded:
        print(f"  Brightness filter: excluded {_dark_excluded} dark scenes (< {_BRIGHTNESS_BAN})")
    if _neg_excluded:
        print(f"  Neg-score excluded: {_neg_excluded} scene(s)")
    if not _all_scores:
        raise ValueError("scene_scores.csv is empty")

    # Music-driven ignores scene_selection threshold — take all clips by score.
    # Threshold read for info logging only; manual includes are guaranteed first.
    _threshold = float(_cp.get("scene_selection", "threshold", fallback="0"))
    _above_thr = sum(1 for v in _all_scores.values() if v >= _threshold)
    _all_sorted = sorted(_all_scores.items(), key=lambda x: x[1], reverse=True)
    # Guarantee manual includes appear first in the sorted list
    _all_sorted = (
        [(k, v) for k, v in _all_sorted if k in _manual_included] +
        [(k, v) for k, v in _all_sorted if k not in _manual_included]
    )

    # Back-cam scenes are allowed freely — sync-ban already propagates bans from
    # main-cam to back-cam for the same time window. Camera pattern in match_clips()
    # handles switching between cameras to build dynamics.

    print(f"\n[music-driven] {len(_all_scores)} clips (above threshold {_threshold:.3f}: {_above_thr})  "
          f"music={music_path.name}  scores={scores_csv.name}  res={_resolution}@{_framerate}fps")

    # 1. Analyse music
    music_info = analyze_music(music_path)
    beat_times = music_info["beat_times"]
    beat_energy = music_info["beat_energy"]

    # Full highlight always starts from 0 — find_best_offset is for shorts only
    music_ss = 0.0

    # Shift beat_times to start from music_ss
    start_idx = next((i for i, t in enumerate(beat_times) if t >= music_ss), 0)
    beat_times  = [t - music_ss for t in beat_times[start_idx:]]
    beat_energy = beat_energy[start_idx:]

    # Read intro card duration early — needed for beat-alignment below.
    _no_intro = _cp.getboolean("job", "no_intro", fallback=False)
    _card_dur = _cp.getfloat("intro_outro", "duration", fallback=3.0) if not _no_intro else 0.0

    # 2. Build cut schedule (beats per shot configurable via [music_driven] in config.ini)
    def _cpint(s, k, fb): v = _cp.get(s, k, fallback=fb); return int(v) if v.strip() else int(fb)
    def _cpfloat(s, k, fb): v = _cp.get(s, k, fallback=fb); return float(v) if v.strip() else float(fb)
    _beats_ultra_fast = _cpint("music_driven", "beats_ultra_fast", "2")
    _beats_auto   = _cp.getboolean("music_driven", "beats_auto",   fallback=False)
    _beats_method = _cp.get(       "music_driven", "beats_method", fallback="segments")
    _beats_fast = _cpint("music_driven", "beats_fast", "3")
    _beats_mid  = _cpint("music_driven", "beats_mid",  "4")
    _beats_slow = _cpint("music_driven", "beats_slow", "6")
    _min_shot_sec = _cpfloat("music_driven", "min_shot_sec", "1.5")
    _max_shot_sec = _cpfloat("music_driven", "max_shot_sec", "8.0")
    # BPM-adaptive: ensure minimum clip durations regardless of tempo.
    # At high BPM (e.g. 117) default 3 beats = 1.5s → slideshow.
    import math as _math
    _bpm = music_info.get("tempo", 120.0)
    _beat_sec = 60.0 / _bpm
    _min_ultra = _cpfloat("music_driven", "min_clip_ultra_sec", "0.8")
    _min_fast = _cpfloat("music_driven", "min_clip_fast_sec", "1.5")
    _min_mid  = _cpfloat("music_driven", "min_clip_mid_sec",  "2.5")
    _min_slow = float(_cp.get("music_driven", "min_clip_slow_sec", fallback="4.0"))
    _beats_ultra_fast = max(_beats_ultra_fast, _math.ceil(_min_ultra / _beat_sec))
    _beats_fast = max(_beats_fast, _math.ceil(_min_fast / _beat_sec))
    _beats_mid  = max(_beats_mid,  _math.ceil(_min_mid  / _beat_sec))
    _beats_slow = max(_beats_slow, _math.ceil(_min_slow / _beat_sec))
    print(f"  BPM={_bpm:.0f}  beat={_beat_sec:.2f}s  "
          f"beats: ultra={_beats_ultra_fast}({_beats_ultra_fast*_beat_sec:.1f}s) "
          f"fast={_beats_fast}({_beats_fast*_beat_sec:.1f}s) "
          f"mid={_beats_mid}({_beats_mid*_beat_sec:.1f}s) "
          f"slow={_beats_slow}({_beats_slow*_beat_sec:.1f}s)")
    _section_energy  = music_info.get("section_energy")
    _onset_energy    = music_info.get("onset_energy")
    _harm_energy     = music_info.get("harm_energy")
    _onset_env_perc  = music_info.get("onset_env_perc")
    _segments_data   = music_info.get("segments", [])
    _music_sr        = music_info.get("sr", 22050)
    _music_hop       = music_info.get("hop", 512)

    def _make_schedule(bt, be):
        if _beats_auto and _onset_env_perc:
            # Legacy onset-peak mode (dense music → fixed min_shot_sec gap)
            return build_schedule(bt, be,
                                  _beats_ultra_fast, _beats_fast, _beats_mid, _beats_slow,
                                  auto=True,
                                  min_shot_sec=_min_shot_sec, max_shot_sec=_max_shot_sec,
                                  section_energy=_section_energy,
                                  onset_energy=_onset_energy,
                                  onset_env_perc=_onset_env_perc,
                                  sr=_music_sr, hop=_music_hop)
        if _beats_method == "segments" and _segments_data:
            sched = _build_schedule_segments(bt, _segments_data,
                                             _beats_ultra_fast, _beats_fast,
                                             _beats_mid, _beats_slow,
                                             section_energy=_section_energy,
                                             perc_energy=_onset_energy,
                                             harm_energy=_harm_energy)
            if sched:
                return sched
            print("  Segments schedule empty — falling back to section energy")
        # section mode: beat-grid with section_energy (4s RMS) + percentile tiers
        return build_schedule(bt, be,
                              _beats_ultra_fast, _beats_fast, _beats_mid, _beats_slow,
                              auto=False,
                              min_shot_sec=_min_shot_sec, max_shot_sec=_max_shot_sec,
                              section_energy=_section_energy,
                              onset_energy=_onset_energy,
                              onset_env_perc=_onset_env_perc,
                              sr=_music_sr, hop=_music_hop)

    schedule = _make_schedule(beat_times, beat_energy)
    if not schedule:
        raise RuntimeError("Could not build cut schedule from music")

    # Align music_ss so the first clip cut (after intro card) lands on a beat.
    # In the final video: [intro card: card_dur] [clip1] [clip2] ...
    # At the intro→clip1 cut, music is at (music_ss + card_dur) — must be a beat.
    # Find the beat nearest to (first_beat + card_dur), rebuild schedule from there,
    # set music_ss so that beat aligns exactly with the intro card end.
    music_ss = schedule[0]["start"]
    # In peak-based auto mode, the schedule is already built from exact percussive events
    # — skip beat-grid intro sync (which would destroy peak alignment).
    if _card_dur > 0 and beat_times and not (_beats_auto and _onset_env_perc):
        _target = music_ss + _card_dur
        _sync_idx = min(range(len(beat_times)), key=lambda _i: abs(beat_times[_i] - _target))
        _sync_beat = beat_times[_sync_idx]
        music_ss = max(0.0, _sync_beat - _card_dur)
        _bt_sync = beat_times[_sync_idx:]
        _be_sync = list(beat_energy)[_sync_idx:]
        if len(_bt_sync) > 1:
            schedule = _make_schedule(_bt_sync, _be_sync)
            print(f"  Intro sync: music_ss={music_ss:.3f}s  "
                  f"first_cut_beat={_sync_beat:.3f}s  "
                  f"card={_card_dur:.1f}s  drift={abs(_sync_beat-_target)*1000:.0f}ms")
    if not schedule:
        raise RuntimeError("Could not build cut schedule from music after intro sync")

    # Reserve intro + outro card time; trim slots that exceed available window.
    _reserve    = _card_dur * 2
    _photo_reserve = 0.0
    _photo_sel_file_early = auto_dir / "photo_selection.json"
    if _photo_sel_file_early.exists():
        try:
            _psel_early = _json.loads(_photo_sel_file_early.read_text())
            _n_photos = len([p for p in _psel_early.get("photos", []) if Path(p).exists()])
            _photo_reserve = _n_photos * 2.5
            if _photo_reserve:
                print(f"  Photos: reserving {_photo_reserve:.1f}s ({_n_photos} × 2.5s) from music window", flush=True)
        except Exception:
            pass
    avail_dur   = music_info["duration"] - music_ss - _reserve - _photo_reserve
    schedule = [s for s in schedule if s["start"] < avail_dur]
    if schedule and schedule[-1]["end"] > avail_dur:
        schedule[-1] = {**schedule[-1], "end": avail_dur,
                        "duration": avail_dur - schedule[-1]["start"]}

    tail_gap    = avail_dur - schedule[-1]["end"]
    if tail_gap > 1.5:
        t = schedule[-1]["end"]
        while t < avail_dur - 1.0:
            slot_dur = min(4.0, avail_dur - t)
            if slot_dur < 1.0:
                break
            schedule.append({
                "start": t, "end": t + slot_dur,
                "duration": slot_dur, "energy": 0.2, "n_beats": 4,
            })
            t += slot_dur
        print(f"  Tail fill: +{tail_gap:.1f}s  schedule now {len(schedule)} slots")

    # Build stem → camera mapping for camera-level diversity in match_clips()
    stem_to_camera: dict[str, str] = {}
    cam_sources_csv = auto_dir / "camera_sources.csv"
    if cam_sources_csv.exists():
        with open(cam_sources_csv) as _f:
            for _row in csv.DictReader(_f):
                if "source" in _row and "camera" in _row:
                    stem_to_camera[_row["source"]] = _row["camera"]
    else:
        # Fallback: scan work_dir subdirectories
        _video_ext = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts"}
        for _sub in sorted(work_dir.iterdir()):
            if _sub.is_dir() and not _sub.name.startswith("_"):
                for _vf in _sub.glob("*"):
                    if _vf.suffix.lower() in _video_ext:
                        stem_to_camera[_vf.stem] = _sub.name
    _cams = sorted(set(stem_to_camera.values()))
    if _cams:
        print(f"  Camera map: {len(stem_to_camera)} sources → {_cams}")

    # Build stem → normalised creation_time [0, 1] for chronological arc
    # 0 = first recording of the day, 1 = last recording of the day
    stem_to_time: dict[str, float] = {}
    _video_ext2 = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts"}
    # Read cam_offsets from config (same keys as [cam_offsets] in config.ini)
    _cam_offsets: dict[str, float] = {}
    if _cp.has_section("cam_offsets"):
        for _k, _v in _cp.items("cam_offsets"):
            try:
                _cam_offsets[_k] = float(_v)
            except ValueError:
                pass
    for _vf in sorted(work_dir.rglob("*")):
        if _vf.suffix.lower() not in _video_ext2:
            continue
        if "_autoframe" in _vf.parts:
            continue
        try:
            _r2 = subprocess.run(
                [ffprobe, "-v", "quiet",
                 "-show_entries", "format_tags=creation_time",
                 "-of", "csv=p=0", str(_vf)],
                capture_output=True, text=True, timeout=5,
            )
            _ts = _r2.stdout.strip()
            if not _ts:
                continue
            from datetime import datetime
            _dt = datetime.fromisoformat(_ts.replace("Z", "+00:00"))
            _epoch = _dt.timestamp()
            _cam = stem_to_camera.get(_vf.stem, "")
            _epoch += _cam_offsets.get(_cam, 0.0)
            stem_to_time[_vf.stem] = _epoch
        except Exception:
            pass
    if len(stem_to_time) >= 2:
        _t_min   = min(stem_to_time.values())
        _t_max   = max(stem_to_time.values())
        _t_range = _t_max - _t_min
        if _t_range > 0:
            stem_to_time = {k: (v - _t_min) / _t_range
                            for k, v in stem_to_time.items()}
            print(f"  Chronological arc: {len(stem_to_time)} sources  "
                  f"span={_t_range/3600:.1f}h")
        else:
            stem_to_time = {}
    else:
        stem_to_time = {}

    # 3. Build clip pool: top clips by score — no threshold cutoff for music-driven.
    # Banned scenes already excluded from _all_sorted.
    needed = len(schedule)
    _pool_size = max(needed * 2, 50)
    scene_scores = dict(_all_sorted[:_pool_size])
    _fallback = [(k, v) for k, v in _all_sorted if k not in scene_scores]
    print(f"  Pool: {len(scene_scores)} clips (top by score, {_above_thr} above threshold)")

    # Camera-pattern balance: if cam_pattern active, ensure each camera has enough
    # clips in the pool to cover its pattern share. Add fallback for deficient cameras.
    _resolved_pat_early = _parse_cam_pattern(_cam_pattern, _cam_order) if _cam_pattern and _cam_order else None
    if _resolved_pat_early and stem_to_camera:
        _cam_needed: dict[str, int] = {}
        for _pi in range(needed):
            _pc = _resolved_pat_early[_pi % len(_resolved_pat_early)]
            _cam_needed[_pc] = _cam_needed.get(_pc, 0) + 1
        _cam_have: dict[str, int] = {}
        for _sc in scene_scores:
            _pc2 = stem_to_camera.get(_clip_source(_sc), "unknown")
            _cam_have[_pc2] = _cam_have.get(_pc2, 0) + 1
        _pat_added = 0
        for _pc, _cnt in _cam_needed.items():
            _deficit = _cnt - _cam_have.get(_pc, 0)
            if _deficit > 0:
                _fb_cam = [(k, v) for k, v in _fallback if stem_to_camera.get(_clip_source(k), "") == _pc and k not in scene_scores]
                _fb_add = dict(_fb_cam[:_deficit])
                scene_scores.update(_fb_add)
                _pat_added += len(_fb_add)
        if _pat_added:
            print(f"  Pattern balance: +{_pat_added} fallback clips to cover camera pattern")

    # Motion analysis — skip entirely for dry-run, use duration_cache.json instead
    if dry_run:
        _dur_cache: dict[str, float] = {}
        _dur_cache_path = auto_dir / "duration_cache.json"
        if _dur_cache_path.exists():
            try:
                _raw = _json.loads(_dur_cache_path.read_text())
                _dur_cache = {k.removesuffix(".mp4"): float(v) for k, v in _raw.items()}
            except Exception:
                pass
        clips = []
        for _scene, _score in sorted(scene_scores.items(), key=lambda x: x[1], reverse=True):
            _dur = _dur_cache.get(_scene, 0.0)
            if _dur < 0.5:
                continue
            _clip_path = autocut_dir / f"{_scene}.mp4"
            _src = _clip_source(_scene)
            clips.append({
                "scene":          _scene,
                "score":          _score,
                "path":           _clip_path,
                "duration":       _dur,
                "motion_peak":    _dur * 0.3,
                "motion_level":   0.0,
                "motion_norm":    0.0,
                "camera":         (stem_to_camera or {}).get(_src, "unknown"),
                "clip_time_norm": (stem_to_time or {}).get(_src),
            })
        print(f"  Dry-run: {len(clips)} clips from duration cache (motion skipped)")
    else:
        clips = analyse_clips(autocut_dir, scene_scores, 1.0, ffprobe,
                              stem_to_camera=stem_to_camera or None,
                              stem_to_time=stem_to_time or None)
    if not clips:
        raise RuntimeError("No clips available for motion analysis")

    # Filter out static clips: configurable via [music_driven] min_motion_score (0.0 = off)
    # Smart-detect: ≤1.0 = relative (motion_norm); >1.0 = absolute pixel diff (motion_level).
    # Absolute is more robust — relative gives 1.0 to least-static clip even if absolutely static.
    # Skipped in dry-run: motion_level is always 0 there (motion analysis not run).
    _min_motion = float(_cp.get("music_driven", "min_motion_score", fallback="5.0"))
    if _min_motion > 0 and len(clips) > len(schedule) and not dry_run:
        _field = "motion_norm" if _min_motion <= 1.0 else "motion_level"
        _before = len(clips)
        _filtered = [c for c in clips if c.get(_field, 0) >= _min_motion]
        # Only apply filter if enough clips remain to fill schedule
        if len(_filtered) >= len(schedule):
            clips = _filtered
            print(f"  Motion filter: removed {_before - len(clips)} static clips "
                  f"({_field} < {_min_motion}, {len(clips)} remain)")
        else:
            print(f"  Motion filter: skipped (would leave only {len(_filtered)} for "
                  f"{len(schedule)} slots — pool too small)")

    # CLIP score floor: exclude clips below min_clip_score (0.0 = off).
    # No pool-size guard — match_clips reuses good clips rather than pulling bad ones.
    _min_clip = float(_cp.get("music_driven", "min_clip_score", fallback="0.0"))
    if _min_clip > 0:
        _score_filtered = [c for c in clips if c.get("score", 0) >= _min_clip]
        if _score_filtered:
            _removed = len(clips) - len(_score_filtered)
            clips = _score_filtered
            print(f"  Score filter: removed {_removed} clips below {_min_clip} "
                  f"({len(clips)} remain)")

    # Post-motion pattern balance: re-check per-camera count after motion filter.
    # Adds lower-scored clips for deficient cameras, bypassing motion filter.
    if _resolved_pat_early and stem_to_camera and not dry_run:
        _clips_scenes = {c["scene"] for c in clips}
        _cam_have_post: dict[str, int] = {}
        for _c in clips:
            _cam = _c.get("camera", "unknown")
            _cam_have_post[_cam] = _cam_have_post.get(_cam, 0) + 1
        _post_added = 0
        for _pc, _cnt in _cam_needed.items():
            _deficit_post = _cnt - _cam_have_post.get(_pc, 0)
            if _deficit_post <= 0:
                continue
            _candidates = [(k, v) for k, v in _all_sorted
                           if stem_to_camera.get(_clip_source(k), "") == _pc
                           and k not in _clips_scenes]
            for _ek, _ev in _candidates:
                if _cam_have_post.get(_pc, 0) >= _cnt:
                    break
                _cp_path = autocut_dir / f"{_ek}.mp4"
                if not _cp_path.exists():
                    continue
                try:
                    _dr = subprocess.run(
                        [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                         "-of", "csv=p=0", str(_cp_path)],
                        capture_output=True, text=True, timeout=5)
                    _dur = float(_dr.stdout.strip())
                except Exception:
                    continue
                if _dur < 1.0:
                    continue
                _src = _clip_source(_ek)
                clips.append({
                    "scene":          _ek,
                    "score":          _ev,
                    "path":           _cp_path,
                    "duration":       _dur,
                    "motion_peak":    _dur * 0.3,
                    "motion_level":   0.5,
                    "motion_norm":    0.5,
                    "camera":         _pc,
                    "clip_time_norm": (stem_to_time or {}).get(_src),
                })
                _clips_scenes.add(_ek)
                _cam_have_post[_pc] = _cam_have_post.get(_pc, 0) + 1
                _post_added += 1
        if _post_added:
            print(f"  Post-motion balance: +{_post_added} clips added for camera pattern coverage")

    if len(clips) < len(schedule):
        print(f"  ⚠ Only {len(clips)} unique clips for {len(schedule)} slots — schedule trimmed (no reuse)")
        schedule = schedule[:len(clips)]

    # GPS bonus: normalize speed/turn across pool, attach gps_norm to each clip
    _gps_weight = float(_cp.get("scene_selection", "gps_weight", fallback="0.0"))
    if _gps_weight > 0 and _gps_raw:
        _spd_vals = sorted(s for s, _ in _gps_raw.values())
        _trn_vals = sorted(t for _, t in _gps_raw.values())
        # 95th percentile normalization — robust against GPS artifacts (e.g.
        # 160°/s spin at standstill that would otherwise dominate max-based scale)
        _p95 = int(len(_spd_vals) * 0.95)
        _spd_ref = _spd_vals[_p95] or 1.0
        _trn_ref = _trn_vals[_p95] or 1.0
        _gps_annotated = sum(1 for s, _ in _gps_raw.values() if s > 0)
        for c in clips:
            spd, trn = _gps_raw.get(c["scene"], (0.0, 0.0))
            c["gps_norm"] = min(1.0, (spd / _spd_ref) * 0.7 + (trn / _trn_ref) * 0.3)
        print(f"  GPS blend (music-driven): weight={_gps_weight}  "
              f"annotated={_gps_annotated}/{len(_all_scores)} scenes  "
              f"spd_p95={_spd_ref:.0f}km/h  trn_p95={_trn_ref:.0f}°/s")

    # Attach mood scores to all clips (incl. those added by post-motion balance)
    if _mood_raw:
        for _mc in clips:
            _ma, _msc = _mood_raw.get(_mc["scene"], (float("nan"), float("nan")))
            _mc["action_score"] = _ma
            _mc["scenic_score"] = _msc
        _mood_cnt = sum(1 for _mc in clips if _mc.get("action_score") == _mc.get("action_score"))
        print(f"  Mood scores: {_mood_cnt}/{len(clips)} clips annotated (action/scenic)")

    # 4. Match clips to schedule
    _chron_weight = 0.20 if stem_to_time else 0.0
    edit = match_clips(schedule, clips, chron_weight=_chron_weight,
                       cam_pattern=_cam_pattern, cam_order=_cam_order,
                       max_consecutive_cam=_max_consecutive_cam,
                       gps_weight=_gps_weight,
                       mood_weight=1.0 if _mood_raw else 0.0)
    if not edit:
        raise RuntimeError("Clip matching produced no edit")

    # Inject selected photos (evenly spaced through timeline)
    _photo_sel_file = auto_dir / "photo_selection.json"
    if _photo_sel_file.exists():
        try:
            _psel = _json.loads(_photo_sel_file.read_text())
            _photo_paths = [p for p in _psel.get("photos", []) if Path(p).exists()]
            if _photo_paths:
                edit = _insert_photos(edit, _photo_paths)
                print(f"  Photos: inserted {len(_photo_paths)} stills into timeline", flush=True)
        except Exception as _pe:
            print(f"  Photos: failed to load selection — {_pe}", flush=True)

    # 5a. Dry-run: write sequence JSON and exit without encoding
    if dry_run:
        seq = []
        for e in edit:
            if e.get("type") == "photo":
                seq.append({
                    "type":       "photo",
                    "path":       e["path"],
                    "duration":   round(e["duration"], 2),
                    "music_start": round(e["music_start"], 2),
                    "frame_path": e["path"],
                    "frame_url":  "/" + Path(e["path"]).relative_to("/").as_posix(),
                })
                continue
            scene = e["scene"]
            frame_path = None
            for suffix in ("_f0.jpg", "_f1.jpg", ".jpg"):
                fp = auto_dir / "frames" / (scene + suffix)
                if fp.exists():
                    frame_path = str(fp)
                    break
            seq.append({
                "scene":      scene,
                "duration":   round(e["duration"], 2),
                "energy":     round(e["energy"], 3),
                "clip_score": e.get("clip_score", 0),
                "clip_ss":    round(e.get("clip_ss", 0), 3),
                "clip_path":  e.get("clip_path", ""),
                "music_start": round(e["music_start"], 2),
                "frame_path": frame_path,
            })
        out_json = auto_dir / "preview_sequence.json"
        out_json.write_text(_json.dumps({"sequence": seq, "music": str(music_path)}, indent=2))
        print(f"Dry-run complete → {len(seq)} slots → {out_json}")
        return out_json

    # 5b. Render
    if output is None:
        output = auto_dir / "highlight_music_driven.mp4"

    render(edit, music_path, music_ss, output, ffmpeg=ffmpeg, nvenc=nvenc,
           resolution=_resolution, framerate=_framerate,
           color_correct=_color_correct, cam_crop=_cam_crop_16x9)

    # Write music info so apply_postprocess() can mix music over the final video
    # (after intro/outro are added), ensuring the fade covers the outro too.
    _music_vol = _cp.getfloat("music", "music_volume", fallback=0.7)
    (auto_dir / "music_info.json").write_text(
        _json.dumps({"music_path": str(music_path), "music_ss": music_ss, "music_vol": _music_vol})
    )

    return output


def _pick_music_from_dir(music_dir: Path) -> Path | None:
    """Pick a random MP3/M4A from a directory (mirrors make_shorts logic)."""
    exts = {".mp3", ".m4a", ".ogg", ".flac", ".wav"}
    tracks = [p for p in sorted(music_dir.rglob("*")) if p.suffix.lower() in exts]
    if not tracks:
        return None
    import random
    return random.choice(tracks)


if __name__ == "__main__":
    import argparse, configparser, sys as _sys

    ap = argparse.ArgumentParser(description="Music-driven highlight assembler")
    ap.add_argument("work_dir")
    ap.add_argument("--music",       default="", help="Path to music file")
    ap.add_argument("--music-dir",   default="", help="Directory to auto-pick music from")
    ap.add_argument("--output",      default="")
    ap.add_argument("--top-percent", type=float, default=0.40,
                    help="Fraction of top CLIP clips to motion-analyse (default 0.4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run scene selection only, write preview_sequence.json, skip encoding")
    ap.add_argument("--use-saved-sequence", action="store_true",
                    help="Skip analyse/match — render exact slots from preview_sequence.json")
    args = ap.parse_args()

    # Read ffmpeg/ffprobe from global config.ini
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).parent.parent / "config.ini")
    _ffmpeg  = cfg.get("paths", "ffmpeg",  fallback="ffmpeg")
    _ffprobe = cfg.get("paths", "ffprobe", fallback="ffprobe")

    # Resolve music file
    if args.music:
        _music = Path(args.music)
    elif args.music_dir:
        _music = _pick_music_from_dir(Path(args.music_dir))
        if not _music:
            _sys.exit(f"ERROR: no music files found in {args.music_dir}")
    else:
        # Fall back to global config [music] dir
        _mdir = cfg.get("music", "dir", fallback="")
        _music = _pick_music_from_dir(Path(_mdir)) if _mdir else None
        if not _music:
            _sys.exit("ERROR: no music file — use --music or --music-dir")

    # Detect NVENC
    _nvenc = False
    try:
        r = subprocess.run([_ffmpeg, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=5)
        _nvenc = "h264_nvenc" in r.stdout
    except Exception:
        pass

    out = assemble(
        Path(args.work_dir),
        _music,
        Path(args.output) if args.output else None,
        top_percent=args.top_percent,
        ffmpeg=_ffmpeg,
        ffprobe=_ffprobe,
        nvenc=_nvenc,
        dry_run=args.dry_run,
        use_saved_sequence=args.use_saved_sequence,
    )
    print(f"\nDone → {out}")
