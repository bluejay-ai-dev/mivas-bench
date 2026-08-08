"""Gemini 3.1 Flash Live harness — cross with any industry agent_blueprint.json."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import TOOL_SERVER_URL, build_agents, industry_path, run_live  # noqa: E402

MODEL = "gemini-3.1-flash-live-preview"


async def run(industry: str = "control-industry") -> None:
    industry_dir = os.environ.get("INDUSTRY_DIR") or str(industry_path(industry))
    await run_live(industry_dir, MODEL)


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    if "--check" in sys.argv:
        start, agents = build_agents(industry_dir)
        print(
            f"ok {industry_dir.name} × {MODEL} start={start} "
            f"agents={agents} tool_server={TOOL_SERVER_URL}"
        )
    else:
        asyncio.run(run(industry))
