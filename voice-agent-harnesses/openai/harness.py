"""Shared blueprint → RealtimeRunner builder for OpenAI Realtime harnesses.

Tool kinds (from agent_blueprint.json):
  - industry (default): harness maps the tool onto the industry state API
    (e.g. schedule_appointment → POST /appointments)
  - handoff: provider handoff API
  - session: harness-local tool (e.g. end_call); then close the realtime session

The industry tool_server is a state/DB API — not a 1:1 tools.json mirror.
Session tools never hit it.

Callers must pass a mutable context into RealtimeRunner.run and stash the
session on it (`context["session"] = session`) so session tools can hang up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from agents import FunctionTool
from agents.realtime import RealtimeAgent, RealtimeRunner, realtime_handoff

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
# Let farewell audio finish before tearing down Realtime after end_call.
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "2.5"))

# Industry tools: name → call state API and shape the tools.json result.
IndustryMapper = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# Session tools: name → harness-local side effect (no state API).
SessionMapper = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def _post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TOOL_SERVER_URL}{path}", json=body)
        resp.raise_for_status()
        return resp.json()


async def _schedule_appointment_via_api(args: dict[str, Any]) -> dict[str, Any]:
    created = await _post_json("/appointments", {"date": args["date"]})
    return {"success": True, "date": created["date"]}


async def _end_call_local(args: dict[str, Any]) -> dict[str, Any]:
    _ = args.get("reason", "")
    return {"success": True}


INDUSTRY_TOOL_HANDLERS: dict[str, IndustryMapper] = {
    "schedule_appointment": _schedule_appointment_via_api,
}

SESSION_TOOL_HANDLERS: dict[str, SessionMapper] = {
    "end_call": _end_call_local,
}


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    # Docker/k8s mounts the selected industry at INDUSTRY_DIR (/app/industry).
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    return (REPO_ROOT / "industries" / name).resolve()


def _tool_catalog(industry_dir: Path) -> dict[str, dict]:
    data = json.loads((industry_dir / "tools.json").read_text())
    return {t["name"]: t for t in data["tools"]}


def _session_from_ctx(tool_ctx: Any) -> Any:
    ctx = getattr(tool_ctx, "context", None)
    if isinstance(ctx, dict):
        return ctx.get("session")
    return getattr(ctx, "session", None)


async def _close_session_soon(session: Any, delay_s: float | None = None) -> None:
    """Hang up after a short delay so farewell audio can finish playing.

    Awaiting close inside the tool deadlocks SDK cleanup; closing immediately
    cuts off the model's goodbye and races Chirp inbound send_audio.
    """
    await asyncio.sleep(END_CALL_CLOSE_DELAY_S if delay_s is None else delay_s)
    with contextlib.suppress(Exception):
        await session.close()


def _fn_tool(spec: dict, *, session_tool: bool = False) -> FunctionTool:
    name = spec["name"]
    schema = {**spec["inputSchema"], "additionalProperties": False}
    schema.setdefault("properties", {})

    if session_tool:
        handler = SESSION_TOOL_HANDLERS.get(name)
        if handler is None:
            raise KeyError(
                f"no harness session handler for tool {name!r} "
                "(session tools are harness-native, not state API routes)"
            )
    else:
        handler = INDUSTRY_TOOL_HANDLERS.get(name)
        if handler is None:
            raise KeyError(
                f"no harness industry handler for tool {name!r} "
                "(map it to a state API call in INDUSTRY_TOOL_HANDLERS)"
            )

    async def on_invoke(tool_ctx: Any, raw: str) -> str:
        from report import finish_tool_span, tool_span

        args = json.loads(raw or "{}")
        call_id = getattr(tool_ctx, "tool_call_id", None)
        with tool_span(name, args, call_id=call_id) as span:
            result = await handler(args)
            finish_tool_span(span, result)
            if session_tool:
                session = _session_from_ctx(tool_ctx)
                if session is not None:
                    asyncio.create_task(_close_session_soon(session))
            return json.dumps(result)

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
    from agents.tool_context import ToolContext

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
        tool_ctx: ToolContext[dict[str, Any]] = ToolContext(
            context={"session": FakeSession()},
            tool_name="end_call",
            tool_call_id="test",
            tool_arguments='{"reason": "done"}',
        )
        out = await tool.on_invoke_tool(tool_ctx, '{"reason": "done"}')
        await asyncio.sleep(END_CALL_CLOSE_DELAY_S + 0.05)
        assert out == '{"success": true}'
        assert closed

        start, agents = build_agents("control-industry")
        assert any(t.name == "end_call" for t in start.tools)
        assert any(t.name == "schedule_appointment" for t in agents["scheduler"].tools)
        print(f"ok session tools start={start.name} agents={list(agents)}")

    asyncio.run(_check())
