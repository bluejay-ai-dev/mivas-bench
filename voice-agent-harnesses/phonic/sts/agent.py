"""Phonic harness entry: offline --check of pack → Phonic agents, tools and config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    INTELLIGENCE,
    MODEL,
    TOOL_SERVER_URL,
    agent_specs,
    config_message,
    industry_path,
    load_pack,
    project_name,
    tool_specs,
)


def check(industry: str | Path) -> None:
    pack = load_pack(industry)
    agents, tools = agent_specs(pack), tool_specs(pack)
    json.dumps([agents, tools, config_message(pack)])  # wire-serialisable
    transfers = sum(t["type"] == "built_in_transfer_to_agent" for t in tools)
    print(
        f"ok {pack.industry} × {MODEL}:{INTELLIGENCE} project={project_name(pack)} agents={[a['name'] for a in agents]} "
        f"tools={len(tools)} transfers={transfers} tool_server={TOOL_SERVER_URL}"
    )


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    if "--check" in sys.argv:
        check(Path(os.environ.get("INDUSTRY_DIR") or industry_path(industry)))
    else:
        raise SystemExit("run adapters/chirp.py for a live bridge; agent.py only supports --check")
