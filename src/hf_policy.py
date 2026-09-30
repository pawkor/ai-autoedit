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


def apply(days_default: float = 3.0) -> None:
    if os.environ.get("HF_ONLINE") == "1":
        # Must also UNDO an inherited offline env (e.g. set by a parent
        # process) — a bare early-return could not force online.
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        return
    try:
        home = Path(os.environ.get("HF_HOME")
                    or Path.home() / ".cache" / "huggingface")
        marker = home / ".last_online_check"
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
