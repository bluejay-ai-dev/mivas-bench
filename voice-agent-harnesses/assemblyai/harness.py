"""Blueprint → AssemblyAI Voice Agent session helpers (raw websocket, inline config).

Industry tools map to the industry state API. Handoff is soft (session config is
sent once via session.update; tools stay declared, prompt narrative shifts role).
Session tools (end_call) end the session.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import websockets

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
WS_URL = "wss://agents.assemblyai.com/v1/ws"
DEFAULT_GREETING = "Thanks for calling Bluejay's Repair Services! How can I help you today?"


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    # Docker/k8s mounts the selected industry at INDUSTRY_DIR (/app/industry).
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    return (REPO_ROOT / "industries" / name).resolve()


def load_blueprint(industry_dir: str | Path) -> dict[str, Any]:
    industry_dir = industry_path(industry_dir)
    blueprint = json.loads((industry_dir / "agent_blueprint.json").read_text())
    catalog = {t["name"]: t for t in json.loads((industry_dir / "tools.json").read_text())["tools"]}
    agents = {}
    for entry in blueprint["agents"]:
        agents[entry["name"]] = {
            "name": entry["name"],
            "instructions": (industry_dir / entry["system_prompt"]).read_text(),
            "tools": entry["tools"],
        }
    return {
        "industry_dir": industry_dir,
        "start": blueprint["agents"][0]["name"],
        "agents": agents,
        "catalog": catalog,
    }


def _tool_decl(spec: dict) -> dict[str, Any]:
    schema = dict(spec.get("inputSchema") or {"type": "object"})
    schema.setdefault("properties", {})
    return {
        "type": "function",
        "name": spec["name"],
        "description": spec.get("description", spec["name"]),
        "parameters": schema,
    }


def session_config(
    bp: dict[str, Any], *, voice: str | None = None, greeting: str | None = None
) -> dict[str, Any]:
    """All blueprint tools are declared up front (config is fixed at connect)."""
    tools = []
    seen: set[str] = set()
    for agent in bp["agents"].values():
        for t in agent["tools"]:
            name = t["name"]
            if name in seen or name not in bp["catalog"]:
                continue
            seen.add(name)
            tools.append(_tool_decl(bp["catalog"][name]))

    start = bp["agents"][bp["start"]]
    # note soft-handoff: scheduler tools are visible; prompt still starts as receptionist
    instruction = (
        start["instructions"]
        + "\n\n# Multi-agent note\n"
        "Start as the receptionist. Only call schedule_appointment after "
        "handoff_to_scheduler has succeeded and you have adopted the scheduler role."
    )
    return {
        "system_prompt": instruction,
        "greeting": greeting or os.environ.get("ASSEMBLYAI_GREETING", DEFAULT_GREETING),
        "input": {"format": {"encoding": "audio/pcm"}},
        "output": {
            "voice": voice or os.environ.get("ASSEMBLYAI_VOICE", "alba"),
            "format": {"encoding": "audio/pcm"},
        },
        "tools": tools,
    }


async def _post_appointment(date: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TOOL_SERVER_URL}/appointments", json={"date": date})
        resp.raise_for_status()
        body = resp.json()
    return {"success": True, "date": body["date"]}


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    bp: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Execute a tool. Returns (result, should_end_call)."""
    if name == "handoff_to_scheduler":
        target = None
        for t in bp["agents"][state["agent"]]["tools"]:
            if t["name"] == name and t.get("handoff"):
                target = t.get("handoff_to")
                break
        if not target or target not in bp["agents"]:
            return {"success": False, "error": "unknown handoff target"}, False
        state["agent"] = target
        sched = bp["agents"][target]
        return {
            "success": True,
            "role": target,
            "instructions": sched["instructions"],
            "note": "You are now the scheduler. Follow the instructions field exactly.",
        }, False

    if name == "schedule_appointment":
        return await _post_appointment(args["date"]), False

    if name == "end_call":
        return {"success": True}, True

    return {"success": False, "error": f"unknown tool {name}"}, False


async def run_tool(
    name: str,
    args: dict[str, Any],
    bp: dict[str, Any],
    state: dict[str, Any],
    *,
    call_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Execute a tool under a GenAI execute_tool span when a traced_run is active."""
    from report import finish_tool_span, tool_span
    with tool_span(name, args, call_id=call_id) as span:
        result, stop = await _execute_tool(name, args, bp, state)
        finish_tool_span(span, result, ok=bool(result.get("success")))
        return result, stop


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


async def run_session(industry_dir: str | Path, model: str) -> None:
    """open a Voice Agent session; stdin text turns in (via conversation.message), event types on stdout."""
    from report import traced_run

    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise SystemExit("need ASSEMBLYAI_API_KEY")
    bp = load_blueprint(industry_dir)
    state = {"agent": bp["start"]}
    name = Path(industry_path(industry_dir)).name
    pending: list[dict[str, Any]] = []
    should_end = False

    async with traced_run(f"mivas-{name}-{model}", model=model):
        async with websockets.connect(f"{WS_URL}?token={key}") as ws:
            await ws.send(json.dumps({"type": "session.update", "session": session_config(bp)}))
            async for raw in ws:
                event = json.loads(raw)
                etype = event.get("type")
                if etype == "session.ready":
                    print("session.ready", flush=True)
                elif etype == "reply.audio":
                    print(f"audio {len(event.get('data', ''))}", flush=True)
                elif etype == "tool.call":
                    pending.append(event)
                    print(f"tool {event.get('name')}", flush=True)
                elif etype == "reply.done":
                    print("reply.done", flush=True)
                    if pending:
                        calls, pending[:] = list(pending), []
                        for call in calls:
                            result, stop = await run_tool(
                                call["name"],
                                dict(call.get("arguments") or {}),
                                bp,
                                state,
                                call_id=call.get("call_id"),
                            )
                            should_end = should_end or stop
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "tool.result",
                                        "call_id": call["call_id"],
                                        "result": json.dumps(result),
                                    }
                                )
                            )
                        continue
                    if should_end:
                        await ws.send(json.dumps({"type": "session.end"}))
                        continue
                    line = await asyncio.to_thread(sys.stdin.readline)
                    text = line.strip() if line else ""
                    if not text:
                        await ws.send(json.dumps({"type": "session.end"}))
                        continue
                    await ws.send(
                        json.dumps({"type": "conversation.message", "role": "user", "content": text})
                    )
                    await ws.send(json.dumps({"type": "reply.create"}))
                elif etype == "session.ended":
                    print("session.ended", flush=True)
                    return
                elif etype == "session.error":
                    print(f"session.error {event}", flush=True)
