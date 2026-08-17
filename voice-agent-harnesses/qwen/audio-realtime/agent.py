"""Qwen-Audio Realtime harness — DashScope × industry agent_blueprint.json.

family/runtime = qwen/audio-realtime
Docs: https://help.aliyun.com/en/model-studio/qwen-audio-realtime-user-guides

Frontend-only: no ACP / coding-agent backend. One Qwen-Audio WebSocket per
call; handoff is session.update. Speak-first: seed a user text item, then
response.create after session.updated (pack owns greeting text).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    MODEL,
    RUNTIME,
    TOOL_SERVER_URL,
    advertised_tools,
    build_agents,
    demo,
    industry_path,
    load_blueprint,
    run_session,
    tool_names,
    tool_server_url,
    ws_url,
)


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    if "--check" in sys.argv:
        demo()
        start, agents = build_agents(industry_dir)
        bp = load_blueprint(industry_dir)
        tools = advertised_tools(industry_dir)
        per = {a: tool_names(bp, a) for a in bp["agents"]}
        try:
            upstream = ws_url()
        except SystemExit:
            upstream = "(unset)"
        print(
            f"ok {industry_dir.name} × {RUNTIME} model={MODEL} start={start} "
            f"agents={agents} start_tools={tools} per_agent={per} "
            f"ws={upstream} tool_server={tool_server_url() or TOOL_SERVER_URL}"
        )
    else:
        asyncio.run(run_session(industry_dir))
