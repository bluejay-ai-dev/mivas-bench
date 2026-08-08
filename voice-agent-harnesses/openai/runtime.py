"""Shared blueprint → RealtimeRunner builder for OpenAI Realtime harnesses.

Tool kinds (from agent_blueprint.json):
  - industry (default): POST → industry tool server
  - handoff: provider handoff API
  - session: POST → tool server, then close the realtime session

Callers must pass a mutable context into RealtimeRunner.run and stash the
session on it (`context["session"] = session`) so session tools can hang up.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
from agents import FunctionTool
from agents.realtime import RealtimeAgent, RealtimeRunner, realtime_handoff

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    return (REPO_ROOT / "industries" / name).resolve()


def _tool_catalog(industry_dir: Path) -> dict[str, dict]:
    data = json.loads((industry_dir / "tools.json").read_text())
    return {t["name"]: t for t in data["tools"]}


def _session_from_ctx(tool_ctx: Any) -> Any:
    ctx = getattr(tool_ctx, "context", None)
    if isinstance(ctx, dict):
        return ctx.get("session")
    return getattr(ctx, "session", None)


def _fn_tool(spec: dict, *, session_tool: bool = False) -> FunctionTool:
    name = spec["name"]
    schema = {**spec["inputSchema"], "additionalProperties": False}
    schema.setdefault("properties", {})
    url = f"{TOOL_SERVER_URL}/tools/{name}"

    async def on_invoke(tool_ctx: Any, raw: str) -> str:
        args = json.loads(raw or "{}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=args)
            resp.raise_for_status()
            body = resp.text
        if session_tool:
            session = _session_from_ctx(tool_ctx)
            # don't await: SDK cleanup waits on tool tasks, so awaiting close here deadlocks
            if session is not None:
                asyncio.create_task(session.close())
        return body

    return FunctionTool(
        name=name,
        description=spec.get("description", name),
        params_json_schema=schema,
        on_invoke_tool=on_invoke,
    )


def build_agents(industry_dir: str | Path) -> tuple[RealtimeAgent, dict[str, RealtimeAgent]]:
    industry_dir = industry_path(industry_dir)
    blueprint = json.loads((industry_dir / "agent_blueprint.json").read_text())
    catalog = _tool_catalog(industry_dir)

    agents: dict[str, RealtimeAgent] = {}
    for entry in blueprint["agents"]:
        agents[entry["name"]] = RealtimeAgent(
            name=entry["name"],
            instructions=(industry_dir / entry["system_prompt"]).read_text(),
            tools=[
                _fn_tool(catalog[t["name"]], session_tool=bool(t.get("session")))
                for t in entry["tools"]
                if not t.get("handoff") and t["name"] in catalog
            ],
        )

    for entry in blueprint["agents"]:
        handoffs = []
        for t in entry["tools"]:
            if not t.get("handoff"):
                continue
            desc = catalog.get(t["name"], {}).get(
                "description", f"Hand off to {t['handoff_to']}"
            )
            handoffs.append(
                realtime_handoff(
                    agents[t["handoff_to"]],
                    tool_name_override=t["name"],
                    tool_description_override=desc,
                )
            )
        if handoffs:
            agents[entry["name"]].handoffs = handoffs

    return agents[blueprint["agents"][0]["name"]], agents


def build_from_blueprint(industry_dir: str | Path, model: str) -> RealtimeRunner:
    start, _ = build_agents(industry_dir)
    return RealtimeRunner(
        starting_agent=start,
        config={
            "model_settings": {
                "model_name": model,
                "audio": {
                    "input": {
                        "format": "pcm16",
                        "turn_detection": {
                            "type": "semantic_vad",
                            "interrupt_response": True,
                        },
                    },
                    "output": {"format": "pcm16", "voice": "ash"},
                },
                "tool_choice": "auto",
            }
        },
    )


if __name__ == "__main__":
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    async def _check() -> None:
        closed = False

        class FakeSession:
            async def close(self) -> None:
                nonlocal closed
                closed = True

        tool = _fn_tool(
            {
                "name": "end_call",
                "description": "end",
                "inputSchema": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                },
            },
            session_tool=True,
        )
        fake_resp = MagicMock()
        fake_resp.text = '{"success": true}'
        fake_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = fake_resp

        with patch("runtime.httpx.AsyncClient", return_value=mock_client):
            out = await tool.on_invoke_tool(
                SimpleNamespace(context={"session": FakeSession()}),
                '{"reason": "done"}',
            )
        await asyncio.sleep(0)  # let create_task(session.close) run
        assert out == '{"success": true}'
        assert closed
        start, agents = build_agents("control-industry")
        assert any(t.name == "end_call" for t in start.tools)
        print(f"ok session tools start={start.name} agents={list(agents)}")

    asyncio.run(_check())
