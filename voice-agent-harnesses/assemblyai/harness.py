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

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import call_session, headers as tool_headers, set_call_id  # noqa: E402

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
        "greeting": (blueprint.get("greeting") or "").strip(),
    }


def _multiagent_note(bp: dict[str, Any]) -> str:
    """Soft-handoff note: the session's tool list is fixed at connect, so every
    agent's tools are visible — the prompt has to hold the role boundary."""
    handoffs = sorted(
        {t["name"] for a in bp["agents"].values() for t in a["tools"] if t.get("handoff")}
    )
    if not handoffs:
        return ""
    return (
        "\n\n# Multi-agent note\n"
        f"Start as the {bp['start']} agent. Tools belonging to other agents are "
        f"visible to you; only use another agent's tools after the matching handoff "
        f"tool ({', '.join(handoffs)}) has succeeded and you have adopted that "
        "agent's role."
    )


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
    bp: dict[str, Any],
    *,
    voice: str | None = None,
    greeting: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """All blueprint tools are declared up front (config is fixed at connect).

    Pass greeting="" on a mid-call handoff update so the specialist does not
    re-play the opening line. Omit greeting to use env / pack / default.
    """
    tools = []
    seen: set[str] = set()
    for entry in bp["agents"].values():
        for t in entry["tools"]:
            name = t["name"]
            if name in seen or name not in bp["catalog"]:
                continue
            seen.add(name)
            tools.append(_tool_decl(bp["catalog"][name]))

    role_name = agent if agent in bp["agents"] else bp["start"]
    role = bp["agents"][role_name]
    instruction = role["instructions"] + _multiagent_note(bp)
    if greeting is not None:
        spoken = greeting.strip()
    else:
        spoken = (
            os.environ.get("ASSEMBLYAI_GREETING", "").strip()
            or (bp.get("greeting") or "").strip()
            or DEFAULT_GREETING
        )
    return {
        "system_prompt": instruction,
        "greeting": spoken,
        "input": {"format": {"encoding": "audio/pcm"}},
        "output": {
            "voice": voice or os.environ.get("ASSEMBLYAI_VOICE") or "alba",
            "format": {"encoding": "audio/pcm"},
        },
        "tools": tools,
    }


def _tool_entry(bp: dict[str, Any], agent: str, name: str) -> dict[str, Any] | None:
    """The blueprint entry for a tool, preferring the current agent's copy."""
    for owner in [agent] + [a for a in bp["agents"] if a != agent]:
        for t in bp["agents"][owner]["tools"]:
            if t["name"] == name:
                return t
    return None


async def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Generic dispatch: POST /tools/{name}; the server's envelope is the result."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        return resp.json()


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    bp: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Execute a tool. Returns (result, should_end_call). Handoff and session
    tools are harness-native; every other tool dispatches to the tool server."""
    entry = _tool_entry(bp, state["agent"], name)
    if entry is not None and entry.get("handoff"):
        target = entry.get("handoff_to")
        if not target or target not in bp["agents"]:
            return {"success": False, "error": "unknown handoff target"}, False
        state["agent"] = target
        agent = bp["agents"][target]
        return {
            "success": True,
            "role": target,
            "instructions": agent["instructions"],
            "note": f"You are now the {target} agent. Follow the instructions field exactly.",
        }, False

    if entry is not None and entry.get("session"):
        return {"success": True}, True

    return await _dispatch(name, args), False


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
        ok = bool(result.get("ok", result.get("success", True)))
        finish_tool_span(span, result, ok=ok)
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
