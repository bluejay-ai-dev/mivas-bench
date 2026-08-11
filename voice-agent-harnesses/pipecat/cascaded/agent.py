"""Local smoke for the `cascaded` runtime — builds the real services and pipeline
without a transport, so a bad service kwarg fails here instead of mid-call.

    uv run python voice-agent-harnesses/pipecat/cascaded/agent.py control-industry
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check import check_runtime

if __name__ == "__main__":
    check_runtime("cascaded", sys.argv[1] if len(sys.argv) > 1 else "control-industry")
