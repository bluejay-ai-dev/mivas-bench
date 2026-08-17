"""Blueprint → ElevenLabs Conversational AI (ElevenAgents) session helpers.

Multi-agent is native, not soft: each blueprint agent is a persisted ElevenLabs
agent, and the receptionist hands off via the `transfer_to_agent` system tool
(server-side, no harness-side handoff/routing). `end_call` is likewise the
`end_call` system tool — the harness never executes it. `ensure_agents` creates
(or reuses cached) agent IDs; `run_tool` only ever runs client tools, which it
forwards generically to the industry tool server's POST /tools/{name} dispatch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
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
from session_tools import hangup_tool_names as _hangup_names  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = Path(__file__).resolve().parent
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")

API_BASE = "https://api.elevenlabs.io"
AGENT_CACHE_PATH = HARNESS_DIR / ".agents.json"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
DEFAULT_GREETING = "Thanks for calling Bluejay's Repair Services! How can I help you today?"
AUDIO_FORMAT = "pcm_16000"
_ENSURE_LOCK = threading.Lock()

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
        "greeting": (blueprint.get("greeting") or "").strip(),
    }


def hangup_tool_names(bp: dict[str, Any]) -> set[str]:
    """Human-transfer session tools: POST, then the chirp bridge hangs up."""
    return _hangup_names(bp["agents"].values())


def _prop(prop: dict[str, Any], *, name: str = "parameter") -> dict[str, Any]:
    """one tools.json property → ElevenLabs client-tool JSON schema.

    ElevenLabs 422s unless every property sets one of: description,
    dynamic_variable, is_system_provided, constant_value, or is_omitted.
    Healthcare tools.json is often type-only. `items` must also survive so
    array params (find_slots.location_ids) keep an element type.
    """
    raw = dict(prop) if isinstance(prop, dict) else {"type": "string"}
    typ = raw.get("type") or "string"
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "string")
    out: dict[str, Any] = {
        "type": typ,
        "description": raw.get("description") or str(name).replace("_", " "),
    }
    if "enum" in raw:
        out["enum"] = raw["enum"]
    if typ == "array":
        items = raw.get("items") if isinstance(raw.get("items"), dict) else {"type": "string"}
        out["items"] = _prop(items, name=f"{name} item")
    if typ == "object" and isinstance(raw.get("properties"), dict):
        out["properties"] = {
            k: _prop(v, name=k) for k, v in raw["properties"].items() if isinstance(v, dict)
        }
        if raw.get("required"):
            out["required"] = list(raw["required"])
    return out


def _client_tool(spec: dict) -> dict[str, Any]:
    """tools.json inputSchema → ElevenLabs client-tool `parameters` JSON schema."""
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    props = {}
    for key, prop in (raw.get("properties") or {}).items():
        if isinstance(prop, dict):
            props[key] = _prop(prop, name=key)
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


def _system_tool_transfer(transfers: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "type": "system",
        "name": "transfer_to_agent",
        "description": "Transfer the caller to a specialized agent based on their request.",
        "params": {
            "system_tool_type": "transfer_to_agent",
            "transfers": [
                {
                    "agent_id": t["agent_id"],
                    "condition": t["condition"],
                    "delay_ms": 0,
                    "transfer_message": "One moment.",
                    "enable_transferred_agent_first_message": True,
                }
                for t in transfers
            ],
        },
    }


def _transfer_condition(bp: dict[str, Any], handoff_tool_name: str, target_name: str) -> str:
    spec = bp["catalog"].get(handoff_tool_name) or {}
    return spec.get("description") or f"When the caller should be transferred to the {target_name} agent."


def _handoff_names(agent_entry: dict[str, Any]) -> list[str]:
    return [t["name"] for t in agent_entry["tools"] if t.get("handoff")]


def _adapt_prompt(prompt: str, *, handoff_tool_names: list[str] | None = None) -> str:
    """rewrite industry handoff tool names to ElevenLabs' transfer_to_agent system tool."""
    text = prompt
    names = [n for n in (handoff_tool_names or []) if n and n != "transfer_to_agent"]
    if not names:
        return text
    for name in names:
        text = text.replace(f"`{name}`", "`transfer_to_agent`")
        text = text.replace(name, "transfer_to_agent")
    note = (
        "\n\n# ElevenLabs multi-agent\n"
        "Use the `transfer_to_agent` system tool for handoffs. It takes no arguments — "
        "the destination is preconfigured. Do not invent a handoff_to_* client tool. "
        "Transfer at most once, then do the work. Never transfer back to an agent you "
        "just left, and never transfer in a loop. If you are already the right "
        "specialist, stay and use your client tools immediately — do not wait for the "
        "caller, and never ask if they are still there. After a transfer, continue "
        "mid-stride from the conversation so far; do not re-greet. Never narrate "
        "tool names, agent numbers, or internal reasoning. Do not call "
        "transfer_to_human unless the caller asked for a person or this is a 911 "
        "emergency.\n"
    )
    if "ElevenLabs multi-agent" not in text:
        text = text.rstrip() + note
    return text


def _agent_order(bp: dict[str, Any]) -> dict[str, int]:
    return {name: i for i, name in enumerate(bp["agents"])}


def _build_tools(
    agent_entry: dict[str, Any],
    bp: dict[str, Any],
    *,
    agent_ids: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Blueprint tool entries → ElevenLabs tools. Handoffs become one
    `transfer_to_agent` with a transfers[] row per *downstream* destination.

    Reverse edges (later blueprint agent → earlier one) are dropped. Native
    ElevenLabs transfer with 2-cycles (identity ↔ scheduling, coverage ↔
    identity, …) loops for tens of seconds of dead air and floods
    `agent_tool_response`.
    """
    tools: list[dict[str, Any]] = []
    transfers: list[dict[str, str]] = []
    order = _agent_order(bp)
    src_idx = order.get(agent_entry["name"], 0)
    for t in agent_entry["tools"]:
        name = t["name"]
        if t.get("handoff"):
            target = t.get("handoff_to")
            if (
                agent_ids
                and target in agent_ids
                and order.get(target, 0) > src_idx
            ):
                transfers.append(
                    {
                        "agent_id": agent_ids[target],
                        "condition": _transfer_condition(bp, name, target),
                    }
                )
            continue
        if name == "end_call":
            tools.append(_system_tool_end_call())
            continue
        spec = bp["catalog"].get(name)
        if spec:
            tools.append(_client_tool(spec))
    if transfers:
        tools.append(_system_tool_transfer(transfers))
    return tools


def _agent_payload(
    *, name: str, prompt: str, first_message: str, tools: list[dict[str, Any]], voice_id: str
) -> dict[str, Any]:
    agent: dict[str, Any] = {"prompt": {"prompt": prompt, "tools": tools}}
    if first_message:
        agent["first_message"] = first_message
    return {
        "name": name,
        "conversation_config": {
            "agent": agent,
            "asr": {"user_input_audio_format": AUDIO_FORMAT},
            "tts": {"agent_output_audio_format": AUDIO_FORMAT, "voice_id": voice_id},
            "turn": {
                "turn_eagerness": "normal",
                "turn_timeout": 7,
                "silence_end_call_timeout": -1,
            },
            "conversation": {"client_events": CLIENT_EVENTS},
        },
    }


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise SystemExit("need ELEVENLABS_API_KEY")
    return key


def _api_headers() -> dict[str, str]:
    return {"xi-api-key": _api_key(), "Content-Type": "application/json"}


def _raise_el(action: str, r: httpx.Response) -> None:
    if r.is_error:
        print(f"elevenlabs {action} {r.status_code}: {r.text[:2000]}", flush=True)
        r.raise_for_status()


def _post_create_agent(payload: dict[str, Any]) -> str:
    r = httpx.post(
        f"{API_BASE}/v1/convai/agents/create",
        headers=_api_headers(),
        json=payload,
        timeout=30.0,
    )
    _raise_el("create agent", r)
    return r.json()["agent_id"]


def _patch_agent(agent_id: str, payload: dict[str, Any]) -> None:
    r = httpx.patch(
        f"{API_BASE}/v1/convai/agents/{agent_id}",
        headers=_api_headers(),
        json=payload,
        timeout=30.0,
    )
    _raise_el(f"patch agent {agent_id}", r)


def _load_cache() -> dict[str, Any]:
    if not AGENT_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(AGENT_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    AGENT_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")


def _greeting(bp: dict[str, Any]) -> str:
    env = os.environ.get("ELEVENLABS_GREETING", "").strip()
    return env or bp.get("greeting") or DEFAULT_GREETING


def _cache_complete(cached: dict[str, Any] | None, names: list[str]) -> bool:
    if not cached:
        return False
    agents = cached.get("agents") or {}
    if all(agents.get(n) for n in names):
        return True
    # legacy 2-agent cache (receptionist + scheduler ids only)
    return (
        len(names) <= 2
        and bool(cached.get("receptionist_id"))
        and bool(cached.get("scheduler_id"))
    )


def _entry_from_ids(bp: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    start = bp["start"]
    first_handoff = next(
        (t.get("handoff_to") for t in bp["agents"][start]["tools"] if t.get("handoff")),
        None,
    )
    return {
        "receptionist_id": ids[start],
        "scheduler_id": ids.get(first_handoff or start) or ids[start],
        "agents": ids,
    }


def ensure_agents(industry_dir: str | Path, *, voice_id: str | None = None) -> dict[str, str]:
    """Create (or reuse) every blueprint agent for an industry.

    Priority: ELEVENLABS_RECEPTIONIST_AGENT_ID/ELEVENLABS_SCHEDULER_AGENT_ID env
    overrides > `.agents.json` cache (keyed by industry name) > fresh REST
    creates. Multi-agent industries (healthcare) get one ElevenLabs agent per
    blueprint agent; handoffs become a single `transfer_to_agent` with one
    transfers[] row per destination. Cache stores `agents` (name → id) plus
    receptionist_id/scheduler_id for the chirp entrypoint.

    Chirp calls this at process start so the first Bluejay socket is not blocked
    on seven sequential POSTs (healthcare was ~17s of recorded silence).
    """
    with _ENSURE_LOCK:
        return _ensure_agents_locked(industry_dir, voice_id=voice_id)


def _ensure_agents_locked(
    industry_dir: str | Path, *, voice_id: str | None = None
) -> dict[str, str]:
    env_r = os.environ.get("ELEVENLABS_RECEPTIONIST_AGENT_ID", "").strip()
    env_s = os.environ.get("ELEVENLABS_SCHEDULER_AGENT_ID", "").strip()
    if env_r and env_s:
        return {"receptionist_id": env_r, "scheduler_id": env_s}

    bp = load_blueprint(industry_dir)
    industry_name = Path(bp["industry_dir"]).name
    voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
    names = list(bp["agents"])

    cache = _load_cache()
    cached = cache.get(industry_name)
    if _cache_complete(cached, names):
        assert cached is not None
        return {
            "receptionist_id": cached["receptionist_id"],
            "scheduler_id": cached.get("scheduler_id") or cached["receptionist_id"],
        }

    greeting = _greeting(bp)
    ids: dict[str, str] = {}
    # phase 1: create every agent without transfers (need ids before wiring).
    for name, agent in bp["agents"].items():
        first = greeting if name == bp["start"] else "I can take it from here."
        agent_id = _post_create_agent(
            _agent_payload(
                name=f"mivas-{industry_name}-{name}",
                prompt=_adapt_prompt(agent["instructions"], handoff_tool_names=_handoff_names(agent)),
                first_message=first,
                tools=_build_tools(agent, bp),
                voice_id=voice_id,
            )
        )
        ids[name] = agent_id
        print(f"elevenlabs created {industry_name}/{name}={agent_id}", flush=True)

    # phase 2: patch agents that hand off now that every destination exists.
    for name, agent in bp["agents"].items():
        if not _handoff_names(agent):
            continue
        first = greeting if name == bp["start"] else "I can take it from here."
        _patch_agent(
            ids[name],
            _agent_payload(
                name=f"mivas-{industry_name}-{name}",
                prompt=_adapt_prompt(agent["instructions"], handoff_tool_names=_handoff_names(agent)),
                first_message=first,
                tools=_build_tools(agent, bp, agent_ids=ids),
                voice_id=voice_id,
            ),
        )
        print(f"elevenlabs patched transfers {industry_name}/{name}", flush=True)

    entry = _entry_from_ids(bp, ids)
    cache[industry_name] = entry
    _save_cache(cache)
    return {"receptionist_id": entry["receptionist_id"], "scheduler_id": entry["scheduler_id"]}


async def get_signed_url(agent_id: str) -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{API_BASE}/v1/convai/conversation/get-signed-url",
            params={"agent_id": agent_id},
            headers={"xi-api-key": _api_key()},
        )
        r.raise_for_status()
        return r.json()["signed_url"]


async def _execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a client tool by dispatching to POST /tools/{name}; the server's
    envelope goes back to the model. `end_call`/`transfer_to_agent` are ElevenLabs
    system tools — they never reach the harness as a `client_tool_call`.

    After the envelope migration some industries report outcomes with `ok`
    instead of `success`. Normalize a missing `success` key from `ok` so the
    Chirp bridge's ElevenLabs error check uses the same fallback as
    `run_session` and correctly marks guarded/policy failures as errors."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        result = resp.json()
        if isinstance(result, dict) and "success" not in result and "ok" in result:
            result["success"] = result["ok"]
        return result


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
    from report import finish_tool_span, tool_span
    with tool_span(name, args, call_id=call_id) as span:
        try:
            result = await _execute_tool(name, args)
            ok = bool(result.get("ok", result.get("success", False)))
            finish_tool_span(span, result, ok=ok)
            return result
        except Exception as e:
            err = {"success": False, "error": f"{type(e).__name__}: {e}"}
            finish_tool_span(span, err, ok=False)
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
                    is_error = not bool(result.get("ok", result.get("success", False)))
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
