"""OpenAI Realtime 2.1 Mini harness — cross with any industry agent_blueprint.json."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import build_agents, build_from_blueprint, industry_path  # noqa: E402

MODEL = "gpt-realtime-2.1-mini"
# https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini


async def run(industry: str = "healthcare") -> None:
    runner = build_from_blueprint(industry_path(industry), MODEL)
    async with await runner.run() as session:
        async for event in session:
            print(event.type)


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "healthcare")
    if "--check" in sys.argv:
        start, agents = build_agents(industry_path(industry))
        build_from_blueprint(industry_path(industry), MODEL)
        print(f"ok {industry} × {MODEL} start={start.name} agents={list(agents)}")
    else:
        asyncio.run(run(industry))
