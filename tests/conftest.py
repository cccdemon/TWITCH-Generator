"""Test setup: isolated data dir and a known login before anything imports src.web."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["TG_DATA_DIR"] = tempfile.mkdtemp(prefix="tg-test-")
os.environ["USERS"] = "t:t"
os.environ["SESSION_SECRET"] = "x"
os.environ.pop("POSTER_URL", None)
os.environ.pop("POSTER_TOKEN", None)
