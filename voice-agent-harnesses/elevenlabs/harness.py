"""Blueprint → ElevenLabs Conversational AI (ElevenAgents) session helpers.

Multi-agent is native, not soft: each blueprint agent is a persisted ElevenLabs
agent, and the receptionist hands off via the `transfer_to_agent` system tool
(server-side, no harness-side handoff/routing). `end_call` is likewise the
`end_call` system tool — the harness never executes it. `ensure_agents` creates
(or reuses cached) agent IDs; `run_tool` only ever runs client tools such as
`schedule_appointment`.
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
HARNESS_DIR = Path(__file__).resolve().parent
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")

API_BASE = "https://api.elevenlabs.io"
AGENT_CACHE_PATH = HARNESS_DIR / ".agents.json"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
DEFAULT_GREETING = "Thanks for calling Bluejay's Repair Services! How can I help you today?"
AUDIO_FORMAT = "pcm_16000"

# Client events we rely on — setting this list overrides the server default,
# so anything the harness reads off the wire must be listed explicitly.
# `agent_response_complete` is documented as the end-of-turn signal, but in
# testing it doesn't reliably fire promptly (esp. for a scripted `first_message`
# turn) — `adapters/chirp.py` also falls back to a silence-gap heuristic, and
# `run_session` below keys off `agent_response` (finalized text) instead.
CLIENT_EVENTS = [
    "conversation_initiation_metadata",
    "ping",
    "audio",
    "interruption",
    "user_transcript",
    "agent_response",
    "agent_response_correction",
    "client_tool_call",
    # server-side system tools (transfer_to_agent / end_call) never arrive as a
    # `client_tool_call`; `agent_tool_response` is the only way to see them.
    # Response only, not `agent_tool_request` — one event per tool, no double-count.
    "agent_tool_response",
    "agent_response_complete",
    "client_error",
    "guardrail_triggered",
]


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


def _client_tool(spec: dict) -> dict[str, Any]:
    """tools.json inputSchema → ElevenLabs client-tool `parameters` JSON schema.

    The catalog's `{type, properties: {name: {type, description, enum}}, required}`
    shape already matches ElevenLabs' literal-property schema; we just drop keys
    ElevenLabs' schema doesn't recognize (e.g. `additionalProperties`).
    """
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    props = {}
    for key, prop in (raw.get("properties") or {}).items():
        p = {k: v for k, v in dict(prop).items() if k in {"type", "description", "enum"}}
        p.setdefault("type", "string")
        props[key] = p
    parameters: dict[str, Any] = {"type": "object", "properties": props}
    if raw.get("required"):
        parameters["required"] = list(raw["required"])
    return {
        "type": "client",
        "name": spec["name"],
        "description": spec.get("description", spec["name"]),
        "expects_response": True,
        "parameters": parameters,
    }


def _system_tool_end_call() -> dict[str, Any]:
    return {
        "type": "system",
        "name": "end_call",
        "description": "End the call once the caller is done, or immediately if it is spam or a wrong number.",
        "params": {"system_tool_type": "end_call"},
    }


def _system_tool_transfer(target_agent_id: str, condition: str) -> dict[str, Any]:
    return {
        "type": "system",
        "name": "transfer_to_agent",
        "description": "Transfer the caller to a specialized agent based on their request.",
        "params": {
            "system_tool_type": "transfer_to_agent",
            "transfers": [
                {
                    "agent_id": target_agent_id,
                    "condition": condition,
                    "delay_ms": 0,
                    "transfer_message": None,
                    "enable_transferred_agent_first_message": False,
                }
            ],
        },
    }


def _transfer_condition(bp: dict[str, Any], handoff_tool_name: str, target_name: str) -> str:
    spec = bp["catalog"].get(handoff_tool_name) or {}
    return spec.get("description") or f"When the caller should be transferred to the {target_name} agent."


def _adapt_prompt(prompt: str, *, handoff_tool_name: str | None = None) -> str:
    """rewrite industry handoff tool names to ElevenLabs' transfer_to_agent system tool."""
    text = prompt
    if not handoff_tool_name:
        # no transfer tool on this agent (scheduler / single-agent industry) — the
        # note would tell it to use a tool it doesn't have.
        return text
    if handoff_tool_name != "transfer_to_agent":
        text = text.replace(f"`{handoff_tool_name}`", "`transfer_to_agent`")
        text = text.replace(handoff_tool_name, "transfer_to_agent")
    note = (
        "\n\n# ElevenLabs multi-agent\n"
        "Use the `transfer_to_agent` system tool for handoffs. It takes no arguments — "
        "the destination is preconfigured. Do not invent a handoff_to_* client tool.\n"
    )
    if "ElevenLabs multi-agent" not in text:
        text = text.rstrip() + note
    return text


def _build_tools(
    agent_entry: dict[str, Any],
    bp: dict[str, Any],
    *,
    transfer_target_id: str | None = None,
    transfer_condition: str | None = None,
) -> list[dict[str, Any]]:
    """Blueprint tool entries → ElevenLabs tools. Handoffs become `transfer_to_agent`
    (native, server-side); session tools become `end_call`; everything else is a
    client tool the harness must execute (see `run_tool`)."""
    tools: list[dict[str, Any]] = []
    for t in agent_entry["tools"]:
        name = t["name"]
        if t.get("handoff"):
            if transfer_target_id:
                tools.append(_system_tool_transfer(transfer_target_id, transfer_condition or ""))
            continue
        if t.get("session") or name == "end_call":
            tools.append(_system_tool_end_call())
            continue
        spec = bp["catalog"].get(name)
        if spec:
            tools.append(_client_tool(spec))
    return tools


def _agent_payload(
    *, name: str, prompt: str, first_message: str, tools: list[dict[str, Any]], voice_id: str
) -> dict[str, Any]:
    return {
        "name": name,
        "conversation_config": {
            "agent": {
                "prompt": {"prompt": prompt, "tools": tools},
                "first_message": first_message,
            },
            "asr": {"user_input_audio_format": AUDIO_FORMAT},
            "tts": {"agent_output_audio_format": AUDIO_FORMAT, "voice_id": voice_id},
            "conversation": {"client_events": CLIENT_EVENTS},
        },
    }


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise SystemExit("need ELEVENLABS_API_KEY")
    return key


def _post_create_agent(payload: dict[str, Any]) -> str:
    r = httpx.post(
        f"{API_BASE}/v1/convai/agents/create",
        headers={"xi-api-key": _api_key(), "Content-Type": "application/json"},
        json=payload,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["agent_id"]


def _load_cache() -> dict[str, Any]:
    if not AGENT_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(AGENT_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    AGENT_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")


def ensure_agents(industry_dir: str | Path, *, voice_id: str | None = None) -> dict[str, str]:
    """Create (or reuse) the receptionist/scheduler agent pair for an industry.

    Priority: ELEVENLABS_RECEPTIONIST_AGENT_ID/ELEVENLABS_SCHEDULER_AGENT_ID env
    overrides > `.agents.json` cache (keyed by industry name) > fresh REST creates,
    which are then written back to the cache.
    """
    env_r = os.environ.get("ELEVENLABS_RECEPTIONIST_AGENT_ID", "").strip()
    env_s = os.environ.get("ELEVENLABS_SCHEDULER_AGENT_ID", "").strip()
    if env_r and env_s:
        return {"receptionist_id": env_r, "scheduler_id": env_s}

    bp = load_blueprint(industry_dir)
    industry_name = Path(bp["industry_dir"]).name
    voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)

    cache = _load_cache()
    cached = cache.get(industry_name)
    if cached and cached.get("receptionist_id") and cached.get("scheduler_id"):
        return cached

    start_name = bp["start"]
    start_agent = bp["agents"][start_name]
    handoff = next((t for t in start_agent["tools"] if t.get("handoff")), None)

    if handoff is None:
        # single-agent industry — no transfer tool, one agent plays both roles.
        agent_id = _post_create_agent(
            _agent_payload(
                name=f"mivas-{industry_name}-{start_name}",
                prompt=_adapt_prompt(start_agent["instructions"]),
                first_message=os.environ.get("ELEVENLABS_GREETING", DEFAULT_GREETING),
                tools=_build_tools(start_agent, bp),
                voice_id=voice_id,
            )
        )
        entry = {"receptionist_id": agent_id, "scheduler_id": agent_id}
    else:
        target_name = handoff["handoff_to"]
        target_agent = bp["agents"][target_name]
        scheduler_id = _post_create_agent(
            _agent_payload(
                name=f"mivas-{industry_name}-{target_name}",
                prompt=_adapt_prompt(target_agent["instructions"]),
                first_message="",
                tools=_build_tools(target_agent, bp),
                voice_id=voice_id,
            )
        )
        condition = _transfer_condition(bp, handoff["name"], target_name)
        receptionist_id = _post_create_agent(
            _agent_payload(
                name=f"mivas-{industry_name}-{start_name}",
                prompt=_adapt_prompt(
                    start_agent["instructions"], handoff_tool_name=handoff["name"]
                ),
                first_message=os.environ.get("ELEVENLABS_GREETING", DEFAULT_GREETING),
                tools=_build_tools(
                    start_agent, bp, transfer_target_id=scheduler_id, transfer_condition=condition
                ),
                voice_id=voice_id,
            )
        )
        entry = {"receptionist_id": receptionist_id, "scheduler_id": scheduler_id}

    cache[industry_name] = entry
    _save_cache(cache)
    return entry


async def get_signed_url(agent_id: str) -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{API_BASE}/v1/convai/conversation/get-signed-url",
            params={"agent_id": agent_id},
            headers={"xi-api-key": _api_key()},
        )
        r.raise_for_status()
        return r.json()["signed_url"]


async def _post_appointment(date: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TOOL_SERVER_URL}/appointments", json={"date": date})
        resp.raise_for_status()
        body = resp.json()
    return {"success": True, "date": body["date"]}


async def _execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a client tool. `end_call`/`transfer_to_agent` are ElevenLabs system
    tools — they never reach the harness as a `client_tool_call`."""
    if name == "schedule_appointment":
        return await _post_appointment(args["date"])
    return {"success": False, "error": f"unknown tool {name}"}


async def run_tool(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str | None = None,
) -> dict[str, Any]:
    """Execute a client tool under a GenAI execute_tool span when a traced_run is active.

    Never raises — tool failures become `{success: false, error: ...}` so the
    Conversational AI session (and its OTel tree) can finish cleanly.
    """
    from report import call_offset_ms, finish_tool_span, tool_span

    offset = call_offset_ms()
    with tool_span(name, args, call_id=call_id) as span:
        try:
            result = await _execute_tool(name, args)
            finish_tool_span(
                span, result, ok=True, name=name, parameters=args, start_offset_ms=offset
            )
            return result
        except Exception as e:
            err = {"success": False, "error": f"{type(e).__name__}: {e}"}
            finish_tool_span(
                span, err, ok=False, name=name, parameters=args, start_offset_ms=offset
            )
            return err


async def run_session(industry_dir: str | Path, model: str) -> None:
    """open a Conversational AI session against the receptionist agent; stdin text
    turns in (via `user_message`), event types on stdout."""
    from report import traced_run

    ids = ensure_agents(industry_dir)
    bp = load_blueprint(industry_dir)
    name = Path(bp["industry_dir"]).name

    async with traced_run(f"mivas-{name}-{model}", model=model):
        signed_url = await get_signed_url(ids["receptionist_id"])
        async with websockets.connect(signed_url) as ws:
            await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
            async for raw in ws:
                event = json.loads(raw)
                etype = event.get("type")
                if etype == "conversation_initiation_metadata":
                    print("conversation_initiation_metadata", flush=True)
                elif etype == "audio":
                    data = event.get("audio_event", {}).get("audio_base_64", "")
                    print(f"audio {len(data)}", flush=True)
                elif etype == "agent_response":
                    # `agent_response` carries the finalized text; don't block the
                    # next turn on `agent_response_complete` (trailing TTS audio can
                    # take a while, and — per ElevenLabs' own docs — isn't guaranteed
                    # promptly for a scripted `first_message` turn).
                    text = event.get("agent_response_event", {}).get("agent_response", "")
                    print(f"agent_response: {text}", flush=True)
                    line = await asyncio.to_thread(sys.stdin.readline)
                    reply = line.strip() if line else ""
                    if not reply:
                        return
                    await ws.send(json.dumps({"type": "user_message", "text": reply}))
                elif etype == "agent_response_complete":
                    print("agent_response_complete", flush=True)
                elif etype in ("client_error", "guardrail_triggered"):
                    print(f"{etype} {event}", flush=True)
                    return
                elif etype == "ping":
                    ev = event.get("ping_event", {})
                    await ws.send(json.dumps({"type": "pong", "event_id": ev.get("event_id")}))
                elif etype == "client_tool_call":
                    call = event.get("client_tool_call", {})
                    result = await run_tool(
                        call.get("tool_name"),
                        dict(call.get("parameters") or {}),
                        call_id=call.get("tool_call_id"),
                    )
                    is_error = not bool(result.get("success", True))
                    print(f"tool {call.get('tool_name')} error={is_error}", flush=True)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "client_tool_result",
                                "tool_call_id": call.get("tool_call_id"),
                                "result": json.dumps(result),
                                "is_error": is_error,
                            }
                        )
                    )
                elif etype == "interruption":
                    print("interruption", flush=True)
