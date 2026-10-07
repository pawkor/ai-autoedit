"""Torch device selection — THE single copy.

2026-10-06 audit: every GPU-using script picked its device with a bare
`"cuda" if torch.cuda.is_available() else "cpu"`, with no warning when it
silently degraded to CPU. A host driver/kernel break can leave CUDA
unavailable for hours before anyone notices — the job just runs dramatically
slower, not visibly broken. Use select_torch_device() instead everywhere a
script would otherwise write that check.

Call this AFTER hf_policy.apply() but it does its own lazy `import torch` —
never import torch at module level in a caller that also needs offline/online
policy applied first.
"""
import sys


def select_torch_device(component: str, *, allow_mps: bool = False,
                        require_cuda: bool = False) -> str:
    """Pick "cuda" / "mps" / "cpu", warning loudly (stdout, flushed — these
    scripts always run as subprocesses whose stdout lands in the webapp's
    Log tab) whenever the fallback is a DEGRADATION, not a deliberate choice.

    `component` names the caller in the warning (e.g. "clip_scan",
    "Beat This!") so a slow run's log explains itself without guessing.

    require_cuda=True raises RuntimeError instead of silently returning
    "cpu" — for callers where CPU execution isn't a safe/sane fallback at
    all (e.g. a float16-only compute path that would fail differently on
    CPU anyway).
    """
    import torch
    if torch.cuda.is_available():
        try:
            torch.cuda.init()
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            if require_cuda:
                raise RuntimeError(
                    f"[{component}] CUDA reported available but failed to "
                    f"initialize ({reason}) — require_cuda=True, not "
                    f"falling back to CPU") from e
            _warn_cpu_fallback(component, reason)
            return "cpu"
        return "cuda"

    # require_cuda means CUDA specifically — MPS must never satisfy it, so
    # this check has to come BEFORE the MPS branch, not after (2026-10-06
    # audit: the original order let require_cuda=True + allow_mps=True
    # silently return "mps" on a CUDA-less Apple Silicon host, ignoring
    # require_cuda entirely; dormant today since no caller combines both).
    if require_cuda:
        raise RuntimeError(f"[{component}] CUDA required but unavailable ({_why_no_cuda()})")

    if allow_mps and getattr(torch.backends, "mps", None) is not None \
            and torch.backends.mps.is_available():
        return "mps"

    reason = _why_no_cuda()
    _warn_cpu_fallback(component, reason)
    return "cpu"


def _why_no_cuda() -> str:
    """Best-effort diagnostic for why torch.cuda.is_available() was False —
    distinguishes a CPU-only torch build from a real driver/runtime failure
    so the warning says something actionable instead of just "no GPU"."""
    import torch
    built_cuda = getattr(torch.version, "cuda", None)
    if not built_cuda:
        return "CPU-only torch build (no CUDA support compiled in)"
    try:
        torch.cuda.init()
        n = torch.cuda.device_count()
        return f"torch={torch.__version__} built_cuda={built_cuda} device_count={n}"
    except Exception as e:
        return (f"{type(e).__name__}: {e}; torch={torch.__version__} "
                f"built_cuda={built_cuda}")


def _warn_cpu_fallback(component: str, reason: str) -> None:
    print(f"WARNING [{component}] CUDA unavailable; using CPU — {reason}",
          file=sys.stdout, flush=True)
