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

    def test_hf_online_refreshes_marker(self, tmp_path, monkeypatch):
        # 2026-10-01 production: clip_scan's successful online retry did NOT
        # refresh the marker, so mood_score (a separate subprocess moments
        # later) hit the same stale-but-fresh marker with no retry of its
        # own and crashed. apply() under HF_ONLINE=1 must now refresh the
        # clock so sibling subprocesses in the same batch benefit too.
        hp, marker = self._fresh(tmp_path, monkeypatch)
        monkeypatch.setenv("HF_ONLINE", "1")
        assert not marker.exists()
        hp.apply()
        assert marker.exists()

    def test_check_days_env_is_respected(self, tmp_path, monkeypatch):
        hp, marker = self._fresh(tmp_path, monkeypatch)
        marker.touch()
        old = time.time() - 2 * 86400
        os.utime(marker, (old, old))
        monkeypatch.setenv("HF_CHECK_DAYS", "7")
        hp.apply()
        assert os.environ.get("HF_HUB_OFFLINE") == "1"


# ── critic: sampling and deterministic metrics ───────────────────────────────

# ── device_policy: CUDA/MPS/CPU selection contract ───────────────────────────

class TestDevicePolicy:
    def test_require_cuda_rejects_mps_even_when_allowed(self, monkeypatch):
        # 2026-10-06 audit: require_cuda=True + allow_mps=True used to let
        # MPS silently satisfy "require CUDA" because the MPS branch ran
        # before the require_cuda check. Must raise, not return "mps".
        torch = pytest.importorskip("torch")
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
        from device_policy import select_torch_device
        with pytest.raises(RuntimeError, match="CUDA required"):
            select_torch_device("test", allow_mps=True, require_cuda=True)

    def test_mps_still_used_without_require_cuda(self, monkeypatch):
        torch = pytest.importorskip("torch")
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
        from device_policy import select_torch_device
        assert select_torch_device("test", allow_mps=True) == "mps"


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

class TestForwardExclude:
    def test_boundary_sides_kept_front_excluded(self):
        from insta360_scan import _is_forward
        assert _is_forward(0) is True
        assert _is_forward(45) is True
        assert _is_forward(315) is True
        assert _is_forward(89.9) is True
        assert _is_forward(90) is False      # side: kept
        assert _is_forward(135) is False     # rear-diagonal: kept
        assert _is_forward(180) is False     # rear: kept
        assert _is_forward(225) is False     # rear-diagonal: kept
        assert _is_forward(270) is False     # side: kept

    def test_forward_hard_excluded_even_with_much_higher_score(self):
        # User policy (2026-10-01): forward must be ignored entirely, not
        # just discounted — a dramatically better forward score must NOT
        # win anymore (this used to be allowed under the old soft bias).
        from insta360_scan import select_windows
        rows = [(100, 0, 0.9), (100, 90, 0.05)]
        wins = select_windows(rows, dur_limit=200, clip_dur=6,
                              min_gap=1000, per_file=5)
        assert len(wins) == 1
        assert wins[0]["yaw"] == 90

    def test_negative_scores_forward_still_excluded(self):
        from insta360_scan import select_windows
        rows = [(100, 0, -0.05), (100, 90, -0.05)]
        wins = select_windows(rows, dur_limit=200, clip_dur=6,
                              min_gap=1000, per_file=5)
        assert len(wins) == 1
        assert wins[0]["yaw"] == 90

    def test_file_with_only_forward_candidates_yields_nothing(self):
        # Accepted consequence of a hard exclusion: a file whose only
        # interesting moments are all dead-ahead now gets zero 360 windows
        # rather than a forward-duplicate pick.
        from insta360_scan import select_windows
        rows = [(10, 0, 0.8), (40, 20, 0.7), (70, 340, 0.6)]
        wins = select_windows(rows, dur_limit=100, clip_dur=6,
                              min_gap=5, per_file=5)
        assert wins == []

    def test_forward_yaw_offset_rotates_excluded_wedge(self):
        # Different physical mount: raw yaw=90 is actually dead-ahead for
        # this rig ([insta360] forward_yaw_deg=90). Raw yaw=0 (true left
        # relative to travel) must now be KEPT, and raw yaw=90 EXCLUDED.
        from insta360_scan import _is_forward, select_windows
        assert _is_forward(90, forward_yaw=90) is True
        assert _is_forward(0, forward_yaw=90) is False
        assert _is_forward(180, forward_yaw=90) is False
        rows = [(100, 90, 0.9), (100, 0, 0.05)]
        wins = select_windows(rows, dur_limit=200, clip_dur=6,
                              min_gap=1000, per_file=5, forward_yaw=90)
        assert len(wins) == 1
        assert wins[0]["yaw"] == 0


class TestSelectWindows:
    # yaw=180 (rear) everywhere in this class on purpose: these tests target
    # min_gap/clamp/per_file mechanics, independent of forward-exclusion
    # (covered separately by TestForwardExclude) — yaw=0 rows would now be
    # dropped before any of this logic runs and silently empty the fixture.
    def _rows(self):
        # (ts, yaw, score)
        return [(100, 180, 0.1), (10, 180, 0.9), (12, 180, 0.85),
                (60, 180, 0.5), (200, 180, 0.7)]

    def test_min_gap_suppresses_neighbours(self):
        from insta360_scan import select_windows
        wins = select_windows(self._rows(), dur_limit=300, clip_dur=6,
                              min_gap=30, per_file=10)
        # 12s peak is within 30s of the 10s (higher-score) peak — suppressed;
        # the rest (100/60/200, each >=30s from every pick) all survive.
        assert len(wins) == 4
        assert [w["score"] for w in wins] == [0.9, 0.5, 0.1, 0.7]

    def test_window_clamped_to_file(self):
        from insta360_scan import select_windows
        wins = select_windows([(1, 180, 1.0)], dur_limit=20, clip_dur=6,
                              min_gap=30, per_file=5)
        assert wins[0]["ts"] == 0.0
        wins = select_windows([(19, 180, 1.0)], dur_limit=20, clip_dur=6,
                              min_gap=30, per_file=5)
        assert wins[0]["ts"] == pytest.approx(14.0)

    def test_short_file_yields_nothing(self):
        from insta360_scan import select_windows
        assert select_windows([(1, 0, 1.0)], dur_limit=4, clip_dur=6,
                              min_gap=30, per_file=5) == []

    def test_per_file_cap_and_sorted_by_ts(self):
        from insta360_scan import select_windows
        rows = [(t, 180, 1.0 - t / 1000) for t in range(0, 1000, 40)]
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


class TestDropStaleMarkers:
    def test_removes_other_markers_keeps_current(self, tmp_path):
        from insta360_scan import _drop_stale_markers
        d = tmp_path / "done"
        d.mkdir()
        keep = d / "VID_X_00_001.newwsig"
        old_a = d / "VID_X_00_001.oldwsig"
        other_stem = d / "VID_Y_00_002.somewsig"
        old_a.touch()
        other_stem.touch()
        _drop_stale_markers(d, "VID_X_00_001", keep)
        assert not old_a.exists()          # stale marker of the SAME stem: gone
        assert other_stem.exists()         # different stem: untouched
        # `keep` itself doesn't need to exist yet (it's touched later on
        # success) — the helper must not require or create it.
        assert not keep.exists()

    def test_noop_on_empty_dir(self, tmp_path):
        from insta360_scan import _drop_stale_markers
        d = tmp_path / "done"  # doesn't exist yet
        _drop_stale_markers(d, "VID_X_00_001", d / "VID_X_00_001.wsig")
        assert d.exists()  # mkdir'd, but nothing to remove — no error


class TestScanLock:
    def test_blocks_concurrent_live_process(self, tmp_path):
        # Must be a PID that is NOT our own — self-PID is intentionally a
        # hit (re-audit #1: execve-preserved PID re-acquiring its own
        # lock). Use a real other live process to simulate a genuine
        # concurrent scan.
        import subprocess
        from insta360_scan import acquire_lock, release_lock
        other = subprocess.Popen(["sleep", "5"])
        try:
            lockfile = tmp_path / "insta360" / ".scan.lock"
            lockfile.parent.mkdir(parents=True)
            lockfile.write_text(str(other.pid))
            assert acquire_lock(tmp_path) is None   # other live pid → blocked
        finally:
            other.kill()
            other.wait()
        l = acquire_lock(tmp_path)   # now stale → reclaimable
        assert l is not None
        release_lock(l)
        assert acquire_lock(tmp_path) is not None

    def test_reclaims_stale_dead_pid(self, tmp_path):
        from insta360_scan import acquire_lock, release_lock
        lockfile = tmp_path / "insta360" / ".scan.lock"
        lockfile.parent.mkdir(parents=True)
        lockfile.write_text("999999999")   # not a live pid
        l = acquire_lock(tmp_path)
        assert l is not None
        release_lock(l)

    def test_own_pid_reacquires_after_execve(self, tmp_path):
        # Re-audit High #1 (reproduced in production): hf_policy's offline-
        # retry re-execs via os.execve, which PRESERVES the PID. The
        # re-exec'd process calling acquire_lock() again must see its own
        # PID and succeed — not treat itself as a live blocker and refuse
        # to ever download the model.
        from insta360_scan import acquire_lock
        l1 = acquire_lock(tmp_path)
        assert l1 is not None
        l2 = acquire_lock(tmp_path)   # simulates the post-execve re-call
        assert l2 is not None


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

    def test_segment_boundary_snaps_to_downbeat(self):
        # User-requested (2026-10-06): "major cuts" = segment (verse/chorus)
        # transitions snap to the nearest downbeat; cuts within a segment
        # stay on the plain beat grid. Fixed n_beats=2 sidesteps the energy
        # percentile tiers — only the segment-boundary slot should move.
        from music_driven import _build_schedule_segments
        beat_times = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
        segments = [{"start": 0.0, "end": 2.0, "energy": 0.2},
                   {"start": 2.0, "end": 10.0, "energy": 0.8}]
        downbeat_times = [0.0, 1.8, 3.6]  # deliberately offset from the beat grid
        sched = _build_schedule_segments(
            beat_times, segments, 2, 2, 2, 2, downbeat_times=downbeat_times)
        # Natural beat-grid boundary would be exactly 2.0 — must have moved.
        boundary_slot = next(s for s in sched if s["start"] > 1.0 and s["start"] < 2.0)
        assert boundary_slot["start"] == pytest.approx(1.8)
        prev_slot = sched[sched.index(boundary_slot) - 1]
        assert prev_slot["end"] == pytest.approx(1.8)   # contiguity preserved

    def test_no_downbeats_leaves_grid_unsnapped(self):
        from music_driven import _build_schedule_segments
        beat_times = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
        segments = [{"start": 0.0, "end": 2.0, "energy": 0.2},
                   {"start": 2.0, "end": 10.0, "energy": 0.8}]
        sched = _build_schedule_segments(beat_times, segments, 2, 2, 2, 2)
        boundary_slot = next(s for s in sched if s["start"] == pytest.approx(2.0))
        assert boundary_slot is not None

    def test_snap_rejected_if_it_would_empty_adjacent_slot(self):
        # Re-audit (2026-10-06) caught this before I did: a nearest downbeat
        # within the 2-beat-gap bound can still land too close to the
        # PREVIOUS slot's own start, producing a near-zero/negative duration
        # slot. 1.05 is the nearest downbeat to the 2.0 boundary (within
        # bound) but only 0.05s after the previous slot's start (1.0) — the
        # snap must be rejected, keeping the original 2.0 boundary.
        from music_driven import _build_schedule_segments
        beat_times = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
        segments = [{"start": 0.0, "end": 2.0, "energy": 0.2},
                   {"start": 2.0, "end": 10.0, "energy": 0.8}]
        downbeat_times = [0.0, 1.05, 3.6]
        sched = _build_schedule_segments(
            beat_times, segments, 2, 2, 2, 2, downbeat_times=downbeat_times)
        boundary_slot = next(s for s in sched if s["start"] == pytest.approx(2.0))
        assert boundary_slot is not None
        # No slot anywhere should have a non-positive duration.
        assert all(s["duration"] > 0 for s in sched)

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
