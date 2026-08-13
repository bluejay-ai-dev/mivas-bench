"""Shim → family adapters/conversationrelay.py (TwiML + ConversationRelay WS)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from adapters.conversationrelay import main  # noqa: E402

if __name__ == "__main__":
    main()
