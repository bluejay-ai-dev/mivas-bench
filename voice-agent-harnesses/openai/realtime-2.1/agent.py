"""OpenAI Realtime 2.1 harness — cross with any industry agent_blueprint.json."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    TOOL_SERVER_URL,
    build_agents,
    build_from_blueprint as _build_from_blueprint,
    industry_path,
)

MODEL = "gpt-realtime-2.1"
# https://developers.openai.com/api/docs/models/gpt-realtime-2.1


def build_from_blueprint(industry_dir: str | Path):
    """Harness-local wrapper so callers don't need to pass MODEL."""
    return _build_from_blueprint(industry_dir, MODEL)


async def run(industry: str = "control-industry") -> None:
    industry_dir = os.environ.get("INDUSTRY_DIR") or str(industry_path(industry))
    runner = build_from_blueprint(industry_dir)
    ctx: dict = {}
    async with await runner.run(context=ctx) as session:
        ctx["session"] = session
        async for event in session:
            print(event.type)


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    if "--check" in sys.argv:
        start, agents = build_agents(industry_dir)
        build_from_blueprint(industry_dir)
        print(
            f"ok {industry_dir.name} × {MODEL} start={start.name} "
            f"agents={list(agents)} tool_server={TOOL_SERVER_URL}"
        )
    else:
        asyncio.run(run(industry))
