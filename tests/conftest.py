"""Test configuration: make the bot modules importable and keep their local state out of the repository."""

import os
import sys
import tempfile
from pathlib import Path

# Must be set before `config` is imported: it creates the data directory at import time.
os.environ.setdefault("ARB_BOT_DATA_DIR", tempfile.mkdtemp(prefix="arb_bot_tests_"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
