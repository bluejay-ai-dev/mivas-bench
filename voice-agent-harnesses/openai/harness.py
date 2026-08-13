"""Shared blueprint → RealtimeRunner builder for OpenAI Realtime harnesses.

Tool kinds (from agent_blueprint.json):
  - industry (default): the harness is a dumb pipe — every industry tool is
    POSTed to {TOOL_SERVER_URL}/tools/{name} with {"arguments": {...}} and the
    server's JSON envelope goes back to the model verbatim
  - handoff: provider handoff API
  - session: harness-local tool (e.g. end_call); then close the realtime session

Session and handoff tools never hit the tool server.

Callers must pass a mutable context into RealtimeRunner.run and stash the
session on it (`context["session"] = session`) so session tools can hang up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from agents import FunctionTool
from agents.realtime import RealtimeAgent, RealtimeRunner, realtime_handoff
from pydantic import Field, create_model

from report import register_handoff_tool_names

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
# Let farewell audio finish before tearing down Realtime after end_call.
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "2.5"))

# Session tools: name → harness-local side effect (no state API).
SessionMapper = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# Set once per CHIRP connection so the state API can isolate this call's DB and
# identity pin from every other call in flight. Industries that keep no per-call
# state ignore the header.
CALL_ID: ContextVar[str] = ContextVar("mivas_call_id", default="")


async def dispatch_industry_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Generic dispatch: POST /tools/{name}; the server's envelope is the result."""
    call_id = CALL_ID.get()
    headers = {"X-Mivas-Call-Id": call_id} if call_id else None
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}", json={"arguments": args}, headers=headers
        )
        return resp.json()


async def _end_call_local(args: dict[str, Any]) -> dict[str, Any]:
    _ = args.get("reason", "")
    return {"success": True}


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


def _handoff_input_type(spec: dict[str, Any]) -> type[Any] | None:
    """Build a pydantic model from a tools.json handoff inputSchema, if any."""
    props = (spec.get("inputSchema") or {}).get("properties") or {}
    if not props:
        return None
    required = set((spec.get("inputSchema") or {}).get("required") or [])
    fields: dict[str, Any] = {}
    for name, prop in props.items():
        desc = (prop or {}).get("description") or ""
        if name in required:
            fields[name] = (str, Field(description=desc))
        else:
            fields[name] = (str | None, Field(default=None, description=desc))
    return create_model(f"{spec['name']}HandoffInput", **fields)


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
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return await dispatch_industry_tool(name, args)

    async def on_invoke(tool_ctx: Any, raw: str) -> str:
        args = json.loads(raw or "{}")
        result = await handler(args)
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
            spec = catalog.get(t["name"], {})
            desc = spec.get("description", f"Hand off to {t['handoff_to']}")
            input_type = _handoff_input_type(spec) if spec else None
            target = t["handoff_to"]
            register_handoff_tool_names({target: t["name"]})
            if input_type is not None:
                # the SDK requires on_handoff to take exactly (context, input),
                # so bind the target via a factory rather than a default arg
                def _make_on_handoff(handoff_target: str) -> Callable[[Any, Any], None]:
                    def _on_handoff(ctx: Any, data: Any) -> None:
                        # history already carries the tool args for the next agent;
                        # stash on context for harness/debug use.
                        payload = data.model_dump() if hasattr(data, "model_dump") else data
                        context = getattr(ctx, "context", None)
                        if isinstance(context, dict):
                            context["last_handoff"] = {"to": handoff_target, "input": payload}

                    return _on_handoff

                handoffs.append(
                    realtime_handoff(
                        agents[target],
                        tool_name_override=t["name"],
                        tool_description_override=desc,
                        input_type=input_type,
                        on_handoff=_make_on_handoff(target),
                    )
                )
            else:
                handoffs.append(
                    realtime_handoff(
                        agents[target],
                        tool_name_override=t["name"],
                        tool_description_override=desc,
                    )
                )
        if handoffs:
            agents[entry["name"]].handoffs = handoffs

    return agents[blueprint["agents"][0]["name"]], agents


def build_from_blueprint(industry_dir: str | Path, model: str) -> RealtimeRunner:
    start, _ = build_agents(industry_dir)
    industry_name = Path(industry_path(industry_dir)).name
    # Realtime session.tracing.workflow_name: ^[A-Za-z0-9_ -]+$
    workflow = f"mivas {industry_name} {model}".replace(".", "-").replace("/", " ")
    return RealtimeRunner(
        starting_agent=start,
        config={
            "model_settings": {
                "model_name": model,
                "audio": {
                    "input": {
                        "format": "pcm16",
                        # User audio → text for the instrumentor's prompt buffer.
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "interrupt_response": True,
                        },
                    },
                    "output": {"format": "pcm16", "voice": "ash"},
                },
                "tool_choice": "auto",
                # Realtime API server-side traces → OpenAI dashboard.
                "tracing": {
                    "workflow_name": workflow,
                    "metadata": {
                        "mivas.industry": industry_name,
                        "mivas.model": model,
                    },
                },
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

        # every shipped industry builds without per-tool harness handlers
        for industry in ("healthcare", "legal", "travel"):
            ind_start, ind_agents = build_agents(industry)
            assert ind_agents, industry
        print(f"ok session tools start={start.name} agents={list(agents)}")

    asyncio.run(_check())
