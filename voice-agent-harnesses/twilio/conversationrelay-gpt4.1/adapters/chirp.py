"""Shim → family adapters/chirp.py (ConversationRelay + GPT-4.1)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from adapters.chirp import main  # noqa: E402

if __name__ == "__main__":
    main()
