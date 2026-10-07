"""HF Hub access policy for the pipeline's model-loading scripts.

The hub client pings the API (etag checks) on every process start even with
a complete local cache. Policy: allow ONE online check every HF_CHECK_DAYS
(default 3) — tracked by a marker file inside the HF cache, which is shared
between host and container via the bind mount — and run fully offline in
between. HF_ONLINE=1 always forces online (first download, model change).

Call hf_policy.apply() BEFORE importing torch/open_clip/transformers — the
hub reads the offline env vars at import time.
"""
import os
import time
from pathlib import Path


def retry_online_or_return(reason: str) -> None:
    """An OFFLINE model load failed (cache miss/incomplete). The hub reads
    the offline env at import, so flipping it in-process is too late —
    re-exec this script with HF_ONLINE=1 for a one-shot online run (which
    also repairs the cache for future offline runs). If the load failed
    while already ONLINE, just return and let the caller's last-resort
    fallback act. Production case: an offline SigLIP2 miss silently
    rescanned a whole day on ViT-H, poisoning the embedding space."""
    import sys
    if os.environ.get("HF_ONLINE") == "1" or not os.environ.get("HF_HUB_OFFLINE"):
        print(f"  model load failed while ONLINE ({reason})", flush=True)
        return
    print(f"  HF cache miss in offline mode ({reason}) — re-executing "
          f"ONLINE once to (re)download", flush=True)
    env = dict(os.environ)
    env["HF_ONLINE"] = "1"
    env.pop("HF_HUB_OFFLINE", None)
    env.pop("TRANSFORMERS_OFFLINE", None)
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def _marker_path() -> Path:
    home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    return home / ".last_online_check"


def apply(days_default: float = 3.0) -> None:
    if os.environ.get("HF_ONLINE") == "1":
        # Must also UNDO an inherited offline env (e.g. set by a parent
        # process) — a bare early-return could not force online.
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        # A forced-online run (manual HF_ONLINE=1 or a retry_online_or_return
        # re-exec) proves the network/cache are current — refresh the clock
        # so OTHER subprocesses started moments later in the same analyze
        # batch don't each have to hit their own failure-then-retry cycle
        # (2026-10-01: mood_score ran straight into a stale-but-fresh marker
        # right after clip_scan's own retry, with no refresh in between).
        try:
            marker = _marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except Exception:
            pass
        return
    try:
        marker = _marker_path()
        days = float(os.environ.get("HF_CHECK_DAYS", days_default))
        if (marker.exists()
                and time.time() - marker.stat().st_mtime < days * 86400):
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        else:
            # This run goes online (refreshes etags / picks up updates)
            # and restarts the clock.
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
    except Exception:
        pass
