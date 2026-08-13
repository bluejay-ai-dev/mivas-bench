"""Blueprint → Vapi squad (assistants + native handoff) helpers.

Vapi is a platform: prompts, tools and handoffs all live server-side, so the
harness only (re)pushes blueprint config and then serves the webhook Vapi calls
back on. Multi-agent is a **squad**: the receptionist member carries a `handoff`
tool whose destination is the scheduler member, and the first member answers the
call. `end_call` is Vapi's built-in `endCall` tool, so it never reaches the
harness — every industry tool arrives as an HTTPS POST from Vapi to
`adapters/chirp.py`'s `/tool/{name}` route (which is also where its
`execute_tool` span comes from) and is forwarded verbatim to the industry tool
server's POST /tools/{name} dispatch.

Tool URLs point at the current cloudflared tunnel, which is ephemeral, so
`ensure_squad` re-pushes the whole assistant config on every chirp boot; only the
ids are cached (`.agents.json`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = Path(__file__).resolve().parent
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")

API_BASE = "https://api.vapi.ai"
AGENT_CACHE_PATH = HARNESS_DIR / ".agents.json"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
DEFAULT_GREETING = "Welcome to Bluejay's Repair Services!"
LLM_MODEL = os.environ.get("VAPI_LLM_MODEL", "gpt-4.1")
# plain "flux" is rejected at call time (error-vapifault-deepgram-transcriber-failed)
STT_MODEL = os.environ.get("VAPI_STT_MODEL", "flux-general-en")
TTS_MODEL = os.environ.get("VAPI_TTS_MODEL", "eleven_flash_v2_5")
# 16 kHz raw pcm both directions == CHIRP's format, so the bridge never resamples.
AUDIO_FORMAT = {"format": "pcm_s16le", "container": "raw", "sampleRate": 16000}


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
        # the brand line the answering node speaks; without it every industry
        # would open with the control-industry repair-shop greeting
        "greeting": blueprint.get("greeting"),
    }


def _api_key() -> str:
    key = os.environ.get("VAPI_API_KEY")
    if not key:
        raise SystemExit("need VAPI_API_KEY")
    return key


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = httpx.request(
        method,
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        json=body,
        timeout=60.0,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"vapi {method} {path} -> {r.status_code} {r.text[:600]}")
    return r.json()


_SCHEMA_KEYS = {"type", "description", "enum", "items", "properties", "required"}


def _prop(prop: dict[str, Any]) -> dict[str, Any]:
    """One tools.json property → the schema subset a Vapi function tool takes.

    `items` must survive: an array parameter with no item type (find_slots'
    location_ids, join_waitlist's) leaves the model guessing what to put in it.
    """
    out = {k: v for k, v in prop.items() if k in _SCHEMA_KEYS}
    out.setdefault("type", "string")
    if isinstance(out.get("items"), dict):
        out["items"] = _prop(out["items"])
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: _prop(v) for k, v in out["properties"].items() if isinstance(v, dict)}
    if out["type"] == "array" and not out.get("items"):
        out["items"] = {"type": "string"}
    return out


def _function_tool(spec: dict[str, Any], public_url: str) -> dict[str, Any]:
    """tools.json entry → Vapi custom function tool pointed at our webhook."""
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    props = {}
    for key, prop in (raw.get("properties") or {}).items():
        props[key] = _prop(dict(prop))
    parameters: dict[str, Any] = {"type": "object", "properties": props}
    if raw.get("required"):
        parameters["required"] = list(raw["required"])
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec.get("description", spec["name"]),
            "parameters": parameters,
        },
        "server": {"url": f"{public_url.rstrip('/')}/tool/{spec['name']}"},
    }


def _handoff_tool(name: str, target_id: str, description: str) -> dict[str, Any]:
    return {
        "type": "handoff",
        "destinations": [
            {"type": "assistant", "assistantId": target_id, "description": description}
        ],
        "function": {"name": name, "description": description},
    }


def _build_tools(
    agent_entry: dict[str, Any],
    bp: dict[str, Any],
    public_url: str,
    *,
    assistant_ids: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Blueprint tools → Vapi tools for one assistant.

    `assistant_ids` maps blueprint agent name → Vapi assistant id. Handoff edges
    are only emitted for targets present there, so the first (id-less) pass can
    create the assistants and the second pass can wire the real graph.
    """
    tools: list[dict[str, Any]] = []
    for t in agent_entry["tools"]:
        name = t["name"]
        if t.get("handoff"):
            target_id = (assistant_ids or {}).get(t["handoff_to"])
            if target_id:
                spec = bp["catalog"].get(name) or {}
                tools.append(
                    _handoff_tool(
                        name,
                        target_id,
                        spec.get("description") or f"Hand off to the {t['handoff_to']} agent.",
                    )
                )
            continue
        if t.get("session") or name == "end_call":
            tools.append({"type": "endCall"})
            continue
        spec = bp["catalog"].get(name)
        if spec:
            tools.append(_function_tool(spec, public_url))
    return tools


def _assistant_payload(
    *,
    name: str,
    prompt: str,
    first_message: str | None,
    tools: list[dict[str, Any]],
    voice_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "model": {
            "provider": "openai",
            "model": LLM_MODEL,
            "messages": [{"role": "system", "content": prompt}],
            "tools": tools,
        },
        "voice": {"provider": "11labs", "model": TTS_MODEL, "voiceId": voice_id},
        "transcriber": {"provider": "deepgram", "model": STT_MODEL, "language": "en"},
    }
    if first_message:
        payload["firstMessage"] = first_message
        payload["firstMessageMode"] = "assistant-speaks-first"
    else:
        # the scheduler is only ever reached by handoff; it opens the new leg itself.
        payload["firstMessageMode"] = "assistant-speaks-first-with-model-generated-message"
    return payload


def _load_cache() -> dict[str, Any]:
    if not AGENT_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(AGENT_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    AGENT_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")


def _upsert_assistant(assistant_id: str | None, payload: dict[str, Any]) -> str:
    if assistant_id:
        try:
            return _request("PATCH", f"/assistant/{assistant_id}", payload)["id"]
        except RuntimeError as e:
            if "404" not in str(e):
                raise
    return _request("POST", "/assistant", payload)["id"]


def ensure_squad(industry_dir: str | Path, public_url: str, *, voice_id: str | None = None) -> dict[str, str]:
    """Create or refresh one Vapi assistant per blueprint agent, plus the squad.

    Two passes, because a handoff tool needs its target's assistant id: pass one
    creates/patches every assistant with its industry tools only, pass two patches
    each one again with the handoff edges resolved against the ids from pass one.
    An industry with N nodes gets N assistants and every edge in the blueprint —
    a 7-node graph is not collapsible to receptionist + scheduler.

    Always re-pushes the full config so every tool webhook points at this run's
    tunnel; only ids are reused from `.agents.json`.
    """
    bp = load_blueprint(industry_dir)
    industry_name = Path(bp["industry_dir"]).name
    voice_id = voice_id or os.environ.get("VAPI_VOICE_ID", DEFAULT_VOICE_ID)
    greeting = os.environ.get("VAPI_GREETING") or bp.get("greeting") or DEFAULT_GREETING

    cache = _load_cache()
    entry = dict(cache.get(industry_name) or {})
    cached_ids: dict[str, str] = dict(entry.get("assistants") or {})
    # pre-`assistants` cache layout, so an existing industry keeps its ids
    if not cached_ids and entry.get("receptionist_id"):
        cached_ids[bp["start"]] = entry["receptionist_id"]

    start = bp["start"]
    order = [start] + [n for n in bp["agents"] if n != start]

    def payload_for(name: str, ids: dict[str, str] | None) -> dict[str, Any]:
        agent = bp["agents"][name]
        return _assistant_payload(
            name=f"mivas-{industry_name}-{name}",
            prompt=agent["instructions"],
            # only the node that answers the call speaks a scripted greeting;
            # every other node is reached mid-call and must never re-greet
            first_message=greeting if name == start else None,
            tools=_build_tools(agent, bp, public_url, assistant_ids=ids),
            voice_id=voice_id,
        )

    assistant_ids: dict[str, str] = {}
    for name in order:
        assistant_ids[name] = _upsert_assistant(cached_ids.get(name), payload_for(name, None))
    for name in order:
        if any(t.get("handoff") for t in bp["agents"][name]["tools"]):
            _upsert_assistant(assistant_ids[name], payload_for(name, assistant_ids))

    squad_payload = {
        "name": f"mivas-{industry_name}",
        # first member answers the call
        "members": [{"assistantId": assistant_ids[name]} for name in order],
    }
    squad_id = entry.get("squad_id")
    if squad_id:
        try:
            squad_id = _request("PATCH", f"/squad/{squad_id}", squad_payload)["id"]
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            squad_id = None
    if not squad_id:
        squad_id = _request("POST", "/squad", squad_payload)["id"]

    entry = {
        "assistants": assistant_ids,
        # kept so the runtime agent.py smoke print keeps working on two-node blueprints
        "receptionist_id": assistant_ids[start],
        "scheduler_id": assistant_ids[order[1]] if len(order) > 1 else assistant_ids[start],
        "squad_id": squad_id,
    }
    cache[industry_name] = entry
    _save_cache(cache)
    return entry


def start_websocket_call(squad_id: str) -> tuple[str, str]:
    """POST /call with the websocket transport → (websocketCallUrl, call_id)."""
    resp = _request(
        "POST",
        "/call",
        {
            "squadId": squad_id,
            "transport": {"provider": "vapi.websocket", "audioFormat": AUDIO_FORMAT},
        },
    )
    return resp["transport"]["websocketCallUrl"], resp["id"]


def webhook_tool_names(bp: dict[str, Any]) -> set[str]:
    """The tools that execute through our webhook: every non-handoff,
    non-session blueprint tool (handoff/endCall run Vapi-side)."""
    return {
        t["name"]
        for entry in bp["agents"].values()
        for t in entry["tools"]
        if not t.get("handoff") and not t.get("session") and t["name"] in bp["catalog"]
    }


async def _execute_tool(
    name: str, args: dict[str, Any], call_id: str | None = None
) -> dict[str, Any]:
    """Generic dispatch: POST /tools/{name}; the server's envelope is the result.

    The Vapi call id becomes X-Mivas-Call-Id so the state API keeps this call's DB
    and identity pin separate from every other call in flight.
    """
    headers = {"X-Mivas-Call-Id": call_id} if call_id else None
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}", json={"arguments": args}, headers=headers
        )
        return resp.json()


async def run_tool(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str | None = None,
    vapi_call_id: str | None = None,
) -> dict[str, Any]:
    """Execute a Vapi function tool under an execute_tool span.

    `call_id` identifies the tool call; `vapi_call_id` identifies the conversation.
    The conversation id is what the state API namespaces on and what picks this
    call's trace root — keying either on the tool-call id gives every single tool
    call its own DB, so a verified caller looks unverified one tool later.

    Never raises — failures become `{success: false, error: ...}` so the call (and
    its OTel tree) still finishes cleanly.
    """
    from report import finish_tool_span, tool_span
    with tool_span(name, args, call_id=call_id, vapi_call_id=vapi_call_id) as span:
        try:
            result = await _execute_tool(name, args, vapi_call_id)
            ok = bool(result.get("ok", result.get("success", True)))
            finish_tool_span(span, result, ok=ok)
            return result
        except Exception as e:
            err = {"success": False, "error": f"{type(e).__name__}: {e}"}
            finish_tool_span(span, err, ok=False)
            return err
