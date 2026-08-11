"""NVIDIA Nemotron VoiceChat harness — full-duplex S2S × industry agent_blueprint.json.

Model: https://build.nvidia.com/nvidia/nemotron-voicechat
       https://huggingface.co/nvidia/NVIDIA-NemotronLabs-VoiceChat-11B

Needs a running VoiceChat NIM (`VOICECHAT_WS_URL`, default ws://127.0.0.1:9000/v1/realtime).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import build_agents, industry_path, tool_server_url  # noqa: E402
from voicechat import (  # noqa: E402
    MODEL,
    RUNTIME,
    advertised_tools,
    demo,
    run_session,
    ws_url,
)


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    if "--check" in sys.argv:
        demo()
        start, agents = build_agents(industry_dir)
        tools = advertised_tools(industry_dir)
        print(
            f"ok {industry_dir.name} × {RUNTIME} model={MODEL} start={start} "
            f"agents={agents} tools={tools} ws={ws_url()} "
            f"tool_server={tool_server_url()}"
        )
    else:
        asyncio.run(run_session(industry_dir))
