import os
import sys
from pathlib import Path

# src/ modules import each other flatly (script-style)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# hf_policy runs at insta360_scan import — keep its marker inside the test
# tree, never in the real HF cache.
os.environ.setdefault("HF_HOME", "/tmp/hf_test_home")
