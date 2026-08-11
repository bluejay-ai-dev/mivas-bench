"""Local smoke for the `openai-realtime-2.1` runtime — builds the real services and pipeline
without a transport, so a bad service kwarg fails here instead of mid-call.

    uv run python voice-agent-harnesses/pipecat/openai-realtime-2.1/agent.py control-industry
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check import check_runtime

if __name__ == "__main__":
    check_runtime("openai-realtime-2.1", sys.argv[1] if len(sys.argv) > 1 else "control-industry")
