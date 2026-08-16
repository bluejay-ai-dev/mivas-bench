"""MIVAS Pipecat runtime — OpenAI Realtime `gpt-realtime-2.1` (speech-to-speech).

    python openai-realtime-2.1/agent.py --check control-industry
    python openai-realtime-2.1/agent.py dev
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from check import check_runtime  # noqa: E402

RUNTIME = "openai-realtime-2.1"
_CLI = frozenset({"dev", "start", "download-files", "console"})


if __name__ == "__main__":
    argv = sys.argv[1:]
    positional = [a for a in argv if not a.startswith("-")]
    if "--check" in argv or (positional and positional[0] not in _CLI):
        industry = next((a for a in positional if a not in _CLI), "control-industry")
        check_runtime(RUNTIME, industry)
    else:
        from bot import serve

        serve(RUNTIME)
