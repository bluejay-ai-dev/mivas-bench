"""Amazon Nova 2 Sonic harness — Bedrock bidirectional stream × industry blueprint.

family/runtime = aws/nova-sonic-2
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    MODEL,
    REGION,
    RUNTIME,
    TOOL_SERVER_URL,
    advertised_tools,
    build_agents,
    demo,
    industry_path,
    load_blueprint,
    tool_names,
    tool_server_url,
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
        print(
            f"ok {industry_dir.name} × {RUNTIME} model={MODEL} region={REGION} "
            f"start={start} agents={agents} start_tools={tools} per_agent={per} "
            f"tool_server={tool_server_url() or TOOL_SERVER_URL}"
        )
    else:
        raise SystemExit("live sessions run via adapters/chirp.py")
