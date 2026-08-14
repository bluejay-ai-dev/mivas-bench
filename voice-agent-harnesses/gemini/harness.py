"""Blueprint → Gemini Live session helpers (google-genai SDK).

Industry tools map to the industry state API. Handoff is soft (Live config is
fixed at connect). Session tools (end_call) end the live session.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import google.genai as genai
from google.genai import types

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import call_session, headers as tool_headers, set_call_id  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
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


def _multiagent_note(bp: dict[str, Any]) -> str:
    """Soft-handoff note: the Live config's tool list is fixed at connect, so every
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


_SCHEMA_KEYS = {"type", "description", "enum", "items", "properties", "required"}


def _prop(prop: dict[str, Any]) -> dict[str, Any]:
    """One tools.json property → the subset of JSON Schema Gemini Live accepts.

    `items` has to survive: Live rejects the whole setup with
    `properties[<name>].items: missing field` for a bare array (that killed every
    healthcare call, since find_slots takes location_ids). Nested objects and
    arrays-of-objects recurse for the same reason.
    """
    out = {k: v for k, v in prop.items() if k in _SCHEMA_KEYS}
    if isinstance(out.get("items"), dict):
        out["items"] = _prop(out["items"]) or {"type": "string"}
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: _prop(v) for k, v in out["properties"].items() if isinstance(v, dict)}
    if out.get("type") == "array" and not out.get("items"):
        out["items"] = {"type": "string"}
    return out


def _decl(spec: dict) -> types.FunctionDeclaration:
    # Gemini Live rejects JSON-Schema keys like additionalProperties.
    raw = dict(spec.get("inputSchema") or {})
    raw.pop("additionalProperties", None)
    props = {}
    for key, prop in (raw.get("properties") or {}).items():
        props[key] = _prop(dict(prop))
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if raw.get("required"):
        schema["required"] = list(raw["required"])
    return types.FunctionDeclaration(
        name=spec["name"],
        description=spec.get("description", spec["name"]),
        parameters=schema,
    )


def live_config(bp: dict[str, Any], *, voice: str = "Puck") -> types.LiveConnectConfig:
    """All blueprint tools are declared up front (Live config is fixed at connect)."""
    decls = []
    seen: set[str] = set()
    for agent in bp["agents"].values():
        for t in agent["tools"]:
            name = t["name"]
            if name in seen or name not in bp["catalog"]:
                continue
            seen.add(name)
            decls.append(_decl(bp["catalog"][name]))

    start = bp["agents"][bp["start"]]
    # note soft-handoff: other agents' tools are visible; prompt still starts as bp["start"]
    instruction = start["instructions"] + _multiagent_note(bp)
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=instruction)]),
        tools=[types.Tool(function_declarations=decls)] if decls else None,
    )


def _tool_entry(bp: dict[str, Any], agent: str, name: str,
                *, local_only: bool = False) -> dict[str, Any] | None:
    """The blueprint entry for a tool.

    When *local_only* is True the search is restricted to *agent*'s own tool
    list — this is the correct scope for handoff and session tools, which must
    never resolve against a different agent.  Industry (dispatchable) tools may
    fall back across all agents because the Gemini Live tool list is fixed at
    connect time and every agent's tools are visible.
    """
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == name:
            return t
    if local_only:
        return None
    for owner in bp["agents"]:
        if owner == agent:
            continue
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

    # Handoff / session tools must belong to the *current* agent — never fall
    # back to another agent's tool table, otherwise a post-handoff agent could
    # re-trigger the previous agent's handoff.
    local = _tool_entry(bp, state["agent"], name, local_only=True)
    if local is not None and local.get("handoff"):
        target = local.get("handoff_to")
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

    if local is not None and local.get("session"):
        return {"success": True}, True

    # A handoff/session tool still visible from a *previous* agent (Gemini can
    # keep offering it post-handoff) must stay harness-native and fail here,
    # not fall through to the tool server — it isn't a dispatchable tool and a
    # 404 there would wrongly read as a successful call in the trace.
    visible = _tool_entry(bp, state["agent"], name)
    if local is None and visible is not None and (visible.get("handoff") or visible.get("session")):
        return {"success": False, "error": "tool unavailable for current agent"}, False

    # Industry (dispatchable) tools: fall back across all agents so shared
    # tools remain reachable regardless of which agent is currently active.
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


async def run_live(industry_dir: str | Path, model: str) -> None:
    """open a Live session; stdin text turns in, event types on stdout."""
    from report import traced_run

    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("need GOOGLE_API_KEY")
    bp = load_blueprint(industry_dir)
    state = {"agent": bp["start"]}
    client = genai.Client(api_key=key)
    name = Path(industry_path(industry_dir)).name
    async with traced_run(f"mivas-{name}-{model}", model=model):
        async with client.aio.live.connect(model=model, config=live_config(bp)) as session:
            prompt = "Hello"
            while True:
                await session.send_realtime_input(text=prompt)
                async for response in session.receive():
                    if response.data:
                        print(f"audio {len(response.data)}", flush=True)
                    if response.tool_call:
                        replies = []
                        should_end = False
                        for fc in response.tool_call.function_calls or []:
                            result, stop = await run_tool(
                                fc.name,
                                dict(fc.args or {}),
                                bp,
                                state,
                                call_id=getattr(fc, "id", None),
                            )
                            should_end = should_end or stop
                            print(f"tool {fc.name}", flush=True)
                            replies.append(
                                types.FunctionResponse(
                                    id=fc.id, name=fc.name, response=result
                                )
                            )
                        if replies:
                            await session.send_tool_response(function_responses=replies)
                        if should_end:
                            return
                    sc = response.server_content
                    if sc is not None and getattr(sc, "turn_complete", False):
                        print("turn_complete", flush=True)
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    return
                prompt = line.strip() or "Hello"
