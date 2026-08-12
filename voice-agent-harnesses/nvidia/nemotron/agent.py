"""NVIDIA Nemotron cascaded harness — cross with any industry agent_blueprint.json.

Cloud NIM profile (matches NVIDIA-AI-Blueprints/nemotron-voice-agent):
  Nemotron ASR Streaming → nemotron-3-nano-30b-a3b → Magpie TTS Multilingual
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import check_pipeline  # noqa: E402
from harness import MODEL, RUNTIME, build_agents, industry_path, tool_server_url  # noqa: E402


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    if "--check" in sys.argv:
        start, agents = build_agents(industry_dir)
        check_pipeline(str(industry_dir))
        print(
            f"ok {industry_dir.name} × {RUNTIME} model={MODEL} start={start} "
            f"agents={agents} tool_server={tool_server_url()}"
        )
    else:
        print(
            "Nemotron runs under CHIRP: "
            "uv run python voice-agent-harnesses/nvidia/nemotron/adapters/chirp.py\n"
            "or: uv run python run.py --harness nvidia/nemotron --mode chirp",
            file=sys.stderr,
        )
        sys.exit(2)
