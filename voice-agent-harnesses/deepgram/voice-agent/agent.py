"""Deepgram Voice Agent harness — cross with any industry agent_blueprint.json."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import TOOL_SERVER_URL, build_agents, industry_path, run_session  # noqa: E402

MODEL = "deepgram-voice-agent"


async def run(industry: str = "control-industry") -> None:
    industry_dir = os.environ.get("INDUSTRY_DIR") or str(industry_path(industry))
    await run_session(industry_dir, MODEL)


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    if "--check" in sys.argv:
        start, agents = build_agents(industry_dir)
        print(
            f"ok {industry_dir.name} × {MODEL} start={start} "
            f"agents={agents} tool_server={TOOL_SERVER_URL}"
        )
        # DEEPGRAM_API_KEY present → also exercise a real connect + Settings smoke test.
        if os.environ.get("DEEPGRAM_API_KEY"):
            asyncio.run(run(industry))
    else:
        asyncio.run(run(industry))
