"""Regression tests for the pure helpers that have bitten us before.

Run: highlight-env/bin/python -m pytest tests/ -q
No GPU, no ffmpeg, no network — everything here must stay runnable on a
bare checkout.
"""
import os
import time

import pytest


# ── hf_policy: the "one online check every N days" contract ─────────────────

class TestHfPolicy:
    def _fresh(self, tmp_path, monkeypatch):
        import hf_policy
        monkeypatch.setenv("HF_HOME", str(tmp_path))
        for k in ("HF_ONLINE", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
                  "HF_CHECK_DAYS"):
            monkeypatch.delenv(k, raising=False)
        return hf_policy, tmp_path / ".last_online_check"

    def test_first_run_goes_online_and_creates_marker(self, tmp_path, monkeypatch):
        hp, marker = self._fresh(tmp_path, monkeypatch)
        hp.apply()
        assert os.environ.get("HF_HUB_OFFLINE") is None
        assert marker.exists()

    def test_fresh_marker_forces_offline(self, tmp_path, monkeypatch):
        hp, marker = self._fresh(tmp_path, monkeypatch)
        marker.touch()
        hp.apply()
        assert os.environ.get("HF_HUB_OFFLINE") == "1"
        assert os.environ.get("TRANSFORMERS_OFFLINE") == "1"

    def test_stale_marker_goes_online_and_refreshes(self, tmp_path, monkeypatch):
        hp, marker = self._fresh(tmp_path, monkeypatch)
        marker.touch()
        old = time.time() - 4 * 86400
        os.utime(marker, (old, old))
        hp.apply()
        assert os.environ.get("HF_HUB_OFFLINE") is None
        assert time.time() - marker.stat().st_mtime < 60

    def test_hf_online_overrides_everything(self, tmp_path, monkeypatch):
        hp, marker = self._fresh(tmp_path, monkeypatch)
        marker.touch()
        monkeypatch.setenv("HF_ONLINE", "1")
        hp.apply()
        assert os.environ.get("HF_HUB_OFFLINE") is None

    def test_hf_online_clears_inherited_offline(self, tmp_path, monkeypatch):
        hp, _ = self._fresh(tmp_path, monkeypatch)
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")     # inherited from parent
        monkeypatch.setenv("HF_ONLINE", "1")
        hp.apply()
        assert os.environ.get("HF_HUB_OFFLINE") is None

    def test_importing_insta360_does_not_apply_policy(self, tmp_path, monkeypatch):
        # pipeline imports the module for the ownership manifest only —
        # the import must neither consume the online window nor set the
        # offline env in the long-lived webapp process (audit finding).
        import importlib
        import insta360_scan
        monkeypatch.setenv("HF_HOME", str(tmp_path))
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        importlib.reload(insta360_scan)
        assert os.environ.get("HF_HUB_OFFLINE") is None
        assert not (tmp_path / ".last_online_check").exists()

    def test_check_days_env_is_respected(self, tmp_path, monkeypatch):
        hp, marker = self._fresh(tmp_path, monkeypatch)
        marker.touch()
        old = time.time() - 2 * 86400
        os.utime(marker, (old, old))
        monkeypatch.setenv("HF_CHECK_DAYS", "7")
        hp.apply()
        assert os.environ.get("HF_HUB_OFFLINE") == "1"


# ── critic: sampling and deterministic metrics ───────────────────────────────

class TestCritic:
    def test_even_idxs_covers_both_ends(self):
        from critic import _even_idxs
        idxs = _even_idxs(100, 10)
        assert idxs[0] == 0 and idxs[-1] == 99
        assert idxs == sorted(set(idxs))

    def test_even_idxs_small_inputs(self):
        from critic import _even_idxs
        assert _even_idxs(0, 5) == []
        assert _even_idxs(1, 5) == [0]
        assert _even_idxs(3, 10) == [0, 1, 2]

    def test_metrics_camera_and_chronology(self):
        from critic import _metrics
        slots = [
            {"duration": 3.0, "scene": "a-clip-001", "camera": "helmet",
             "clip_time_norm": 0.1, "music_start": 0},
            {"duration": 2.0, "scene": "a-clip-002", "camera": "back",
             "clip_time_norm": 0.3, "music_start": 3},
            {"duration": 4.0, "scene": "b-clip-001", "camera": "back",
             "clip_time_norm": 0.2, "music_start": 5},
        ]
        out = _metrics(slots)
        assert "adjacent same-source pairs=1" in out
        assert "'back': 2" in out and "'helmet': 1" in out
        assert "backward jumps=1" in out


# ── insta360_scan: selection, discovery, geometry, merge, manifest ───────────

class TestSelectWindows:
    def _rows(self):
        # (ts, yaw, score)
        return [(0, 0, 0.1), (10, 45, 0.9), (12, 90, 0.85),
                (60, 0, 0.5), (200, 270, 0.7)]

    def test_min_gap_suppresses_neighbours(self):
        from insta360_scan import select_windows
        wins = select_windows(self._rows(), dur_limit=300, clip_dur=6,
                              min_gap=30, per_file=10)
        # 12s peak is within 30s of the 10s peak — must be suppressed
        assert [w["yaw"] for w in wins] == [45, 0, 270]

    def test_window_clamped_to_file(self):
        from insta360_scan import select_windows
        wins = select_windows([(1, 0, 1.0)], dur_limit=20, clip_dur=6,
                              min_gap=30, per_file=5)
        assert wins[0]["ts"] == 0.0
        wins = select_windows([(19, 0, 1.0)], dur_limit=20, clip_dur=6,
                              min_gap=30, per_file=5)
        assert wins[0]["ts"] == pytest.approx(14.0)

    def test_short_file_yields_nothing(self):
        from insta360_scan import select_windows
        assert select_windows([(1, 0, 1.0)], dur_limit=4, clip_dur=6,
                              min_gap=30, per_file=5) == []

    def test_per_file_cap_and_sorted_by_ts(self):
        from insta360_scan import select_windows
        rows = [(t, 0, 1.0 - t / 1000) for t in range(0, 1000, 40)]
        wins = select_windows(rows, dur_limit=1000, clip_dur=6,
                              min_gap=30, per_file=4)
        assert len(wins) == 4
        assert [w["ts"] for w in wins] == sorted(w["ts"] for w in wins)


class TestDiscover:
    def test_pairs_and_missing_counterpart(self, tmp_path):
        from insta360_scan import discover
        for n in ("VID_20250423_082930_00_001.insv",
                  "VID_20250423_082930_10_001.insv",
                  "LRV_20250423_082930_11_001.insv",
                  "VID_20250423_090000_00_002.insv"):   # no _10_ partner
            (tmp_path / n).touch()
        pairs = discover(tmp_path)
        assert len(pairs) == 1
        assert pairs[0]["stem"] == "VID_20250423_082930_00_001"
        assert pairs[0]["lrv"] is not None

    def test_lrv_optional(self, tmp_path):
        from insta360_scan import discover
        (tmp_path / "VID_20250423_082930_00_001.insv").touch()
        (tmp_path / "VID_20250423_082930_10_001.insv").touch()
        pairs = discover(tmp_path)
        assert len(pairs) == 1 and pairs[0]["lrv"] is None


class TestGeometry:
    def test_center_pixel_looks_at_yaw(self):
        pytest.importorskip("cv2")
        np = pytest.importorskip("numpy")
        from insta360_scan import _make_ray_grid, _remap_view
        H, W = 200, 400
        eq = np.zeros((H, W, 3), dtype=np.uint8)
        # paint a band at lon=+90° (x = 3/4 of the width); a 1-px line can
        # fall between sample points of the low-res view grid
        eq[:, int(W * 0.75) - 5:int(W * 0.75) + 5, :] = 255
        rays = _make_ray_grid(64, 32, 100, 62)
        view = _remap_view(eq, rays, 90.0, 0.0)
        col = int(view[:, :, 0].sum(axis=0).argmax())
        assert abs(col - 31) <= 3
        # opposite yaw must not see it at all
        assert int(_remap_view(eq, rays, -90.0, 0.0)[:, :, 0].max()) == 0

    def test_yaw_wraps_across_seam(self):
        pytest.importorskip("cv2")
        np = pytest.importorskip("numpy")
        from insta360_scan import _make_ray_grid, _remap_view
        eq = np.zeros((100, 200, 3), dtype=np.uint8)
        eq[:, 0:8, :] = 255           # content at lon≈-180°
        rays = _make_ray_grid(64, 32, 100, 62)
        view = _remap_view(eq, rays, 180.0, 0.0)
        assert int(view[:, :, 0].max()) == 255


class TestMergeAndManifest:
    def _rows(self):
        return [{"scene": "VID_X_00_001-clip-001", "score": 0.5,
                 "offset_sec": 10.0},
                {"scene": "VID_X_00_001-clip-002", "score": 0.6,
                 "offset_sec": 40.0}]

    def test_merge_creates_and_updates(self, tmp_path):
        from insta360_scan import merge_outputs
        merge_outputs(tmp_path, self._rows(), ["VID_X_00_001"],
                      {"VID_X_00_001-clip-001": 6.0})
        csv_text = (tmp_path / "scene_scores_allcam.csv").read_text()
        assert csv_text.count("VID_X_00_001-clip-001") == 1
        # re-merge with a changed score must replace, not duplicate
        rows2 = self._rows()
        rows2[0]["score"] = 0.9
        merge_outputs(tmp_path, rows2, ["VID_X_00_001"], {})
        csv_text = (tmp_path / "scene_scores_allcam.csv").read_text()
        assert csv_text.count("VID_X_00_001-clip-001") == 1
        assert "0.9" in csv_text
        cams = (tmp_path / "camera_sources.csv").read_text()
        assert cams.count("VID_X_00_001") == 1 and ",360" in cams

    def test_manifest_roundtrip_and_reintegrate(self, tmp_path):
        from insta360_scan import (save_manifest, protected_scenes,
                                   reintegrate)
        (tmp_path / "autocut").mkdir()
        (tmp_path / "autocut" / "VID_X_00_001-clip-001.mp4").touch()
        save_manifest(tmp_path, self._rows(), ["VID_X_00_001"],
                      {"VID_X_00_001-clip-001": 6.0,
                       "VID_X_00_001-clip-002": 6.0})
        assert protected_scenes(tmp_path) == {"VID_X_00_001-clip-001",
                                              "VID_X_00_001-clip-002"}
        # clip-002 file is gone → reintegrate must restore only clip-001
        assert reintegrate(tmp_path) == 1
        csv_text = (tmp_path / "scene_scores_allcam.csv").read_text()
        assert "clip-001" in csv_text and "clip-002" not in csv_text

    def test_protected_scenes_empty_without_manifest(self, tmp_path):
        from insta360_scan import protected_scenes
        assert protected_scenes(tmp_path) == set()


# ── music_driven: pure helpers ───────────────────────────────────────────────

class TestMusicDriven:
    def test_parse_cam_pattern(self):
        from music_driven import _parse_cam_pattern
        assert _parse_cam_pattern("aabaab", ["helmet", "back"]) == \
            ["helmet", "helmet", "back", "helmet", "helmet", "back"]
        assert _parse_cam_pattern("", ["helmet", "back"]) is None
        assert _parse_cam_pattern("ab", ["only"]) is None

    def test_hard_no_reuse_and_slot_trim(self):
        # Audit #14 (restored policy): two 4s slots with an 8s and a 1s clip
        # must use BOTH clips (no reuse of the 8s one) and trim the second
        # slot to its 1s source instead of claiming footage it doesn't have.
        from music_driven import match_clips
        clips = [
            {"scene": "a-clip-001", "path": "/x/a.mp4", "duration": 8.0,
             "score": 0.9, "motion_peak": 4.0},
            {"scene": "b-clip-001", "path": "/x/b.mp4", "duration": 1.0,
             "score": 0.1, "motion_peak": 0.5},
        ]
        sched = [{"start": 0.0, "end": 4.0, "duration": 4.0,
                  "energy": 0.5, "n_beats": 2},
                 {"start": 4.0, "end": 8.0, "duration": 4.0,
                  "energy": 0.5, "n_beats": 2}]
        edit = match_clips(sched, clips,
                           temporal_exclusion_sec=0, adjacent_gap_sec=0)
        scenes = sorted(e["scene"] for e in edit)
        assert scenes == ["a-clip-001", "b-clip-001"]
        short = next(e for e in edit if e["scene"] == "b-clip-001")
        assert short["duration"] == pytest.approx(1.0)

    def test_reuse_only_when_slots_exceed_pool(self):
        from music_driven import match_clips
        clips = [{"scene": "a-clip-001", "path": "/x/a.mp4", "duration": 8.0,
                  "score": 0.9, "motion_peak": 4.0}]
        sched = [{"start": i * 4.0, "end": i * 4.0 + 4.0, "duration": 4.0,
                  "energy": 0.5, "n_beats": 2} for i in range(3)]
        edit = match_clips(sched, clips,
                           temporal_exclusion_sec=0, adjacent_gap_sec=0)
        assert len(edit) == 3   # unavoidable rotation fills every slot

    def test_clip_source_strips_only_terminal_suffix(self):
        from music_driven import _clip_source
        assert _clip_source("VID_20250423_082930_00_001-clip-001") == \
            "VID_20250423_082930_00_001"
        assert _clip_source("GX01-scene-042") == "GX01"
        assert _clip_source("name-without-suffix") == "name-without-suffix"
