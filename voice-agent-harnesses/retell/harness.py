"""Blueprint → Retell LLM state machine + agent.

Multi-agent is native: the blueprint's receptionist/scheduler pair becomes a
single retell-llm with two `states`, and the handoff becomes an `edge` between
them (Retell exposes the transition to the model as `transition_to_scheduler`,
server-side — the harness never routes it). `end_call` is the built-in
`{type: "end_call"}` tool, also server-side.

Only `schedule_appointment` reaches us, and it does so as an HTTPS callback:
Retell tools are *platform* tools, so the tool `url` points back at the chirp
process (`{PUBLIC_URL}/tool/schedule_appointment`). The tunnel URL is ephemeral,
so `ensure_agent` re-PATCHes the tool URLs on every boot; the cached llm/agent
ids in `.agents.json` survive.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = Path(__file__).resolve().parent
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")

API_BASE = "https://api.retellai.com"
AGENT_CACHE_PATH = HARNESS_DIR / ".agents.json"
DEFAULT_VOICE_ID = "11labs-Kate"
DEFAULT_GREETING = "Welcome to Bluejay's Repair Services! How can I help you today?"
# Retell's own room; the JS SDK hardcodes it. Overridable in case they re-shard.
DEFAULT_LIVEKIT_URL = "wss://retell-ai-4ihahnq7.livekit.cloud"


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


def _api_key() -> str:
    key = os.environ.get("RETELL_API_KEY")
    if not key:
        raise SystemExit("need RETELL_API_KEY")
    return key


def livekit_url() -> str:
    return os.environ.get("RETELL_LIVEKIT_URL", DEFAULT_LIVEKIT_URL)


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = httpx.request(
        method,
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        json=body,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _custom_tool(spec: dict[str, Any], public_url: str) -> dict[str, Any]:
    """tools.json entry → Retell `{type: "custom"}` webhook tool."""
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
        "type": "custom",
        "name": spec["name"],
        "description": spec.get("description", spec["name"]),
        "url": f"{public_url.rstrip('/')}/tool/{spec['name']}",
        "speak_during_execution": False,
        "speak_after_execution": True,
        "parameters": parameters,
    }


def _state_tools(agent_entry: dict[str, Any], bp: dict[str, Any], public_url: str) -> list[dict]:
    """Handoffs become edges (see `_states`), `end_call` becomes Retell's built-in."""
    tools: list[dict[str, Any]] = []
    for t in agent_entry["tools"]:
        name = t["name"]
        if t.get("handoff"):
            continue
        if t.get("session") or name == "end_call":
            spec = bp["catalog"].get(name) or {}
            tools.append(
                {
                    "type": "end_call",
                    "name": "end_call",
                    "description": spec.get("description", "End the call."),
                }
            )
            continue
        spec = bp["catalog"].get(name)
        if spec:
            tools.append(_custom_tool(spec, public_url))
    return tools


def _adapt_prompt(prompt: str, handoff_tool_name: str | None, target_state: str) -> str:
    """Retell surfaces an edge as `transition_to_<state>`, not the blueprint tool name."""
    if not handoff_tool_name:
        return prompt
    transition = f"transition_to_{target_state}"
    return prompt.replace(f"`{handoff_tool_name}`", f"`{transition}`").replace(
        handoff_tool_name, transition
    )


def _states(bp: dict[str, Any], public_url: str) -> list[dict[str, Any]]:
    states = []
    for name, entry in bp["agents"].items():
        handoff = next((t for t in entry["tools"] if t.get("handoff")), None)
        state: dict[str, Any] = {
            "name": name,
            "state_prompt": _adapt_prompt(
                entry["instructions"],
                handoff["name"] if handoff else None,
                handoff["handoff_to"] if handoff else "",
            ),
            "tools": _state_tools(entry, bp, public_url),
        }
        if handoff:
            spec = bp["catalog"].get(handoff["name"]) or {}
            state["edges"] = [
                {
                    "destination_state_name": handoff["handoff_to"],
                    "description": spec.get("description")
                    or f"Transition when the caller should be handled by {handoff['handoff_to']}.",
                }
            ]
        states.append(state)
    return states


def _llm_payload(bp: dict[str, Any], public_url: str) -> dict[str, Any]:
    return {
        "model": os.environ.get("RETELL_LLM_MODEL", "gpt-4.1"),
        "general_prompt": "You are a voice agent for Bluejay's Repair Services. "
        "Follow the instructions of your current state exactly.",
        "begin_message": os.environ.get("RETELL_GREETING", DEFAULT_GREETING),
        # "agent" matches an inbound receptionist answering the phone, and is what
        # the industry prompt's scripted greeting assumes. "user" makes Retell wait
        # for the caller — and drops begin_message, so the greeting then depends on
        # the model obeying the prompt. See README before flipping this.
        "start_speaker": os.environ.get("RETELL_START_SPEAKER", "agent"),
        "starting_state": bp["start"],
        "states": _states(bp, public_url),
    }


def _agent_payload(llm_id: str, industry_name: str) -> dict[str, Any]:
    return {
        "agent_name": f"mivas-{industry_name}-retell",
        "response_engine": {"type": "retell-llm", "llm_id": llm_id},
        "voice_id": os.environ.get("RETELL_VOICE_ID", DEFAULT_VOICE_ID),
        "voice_model": "eleven_flash_v2_5",
        "language": "en-US",
        # Retell exposes no STT model field and no Flux — provider + endpointing only.
        "stt_mode": "custom",
        "custom_stt_config": {
            "provider": os.environ.get("RETELL_STT_PROVIDER", "deepgram"),
            "endpointing_ms": int(os.environ.get("RETELL_ENDPOINTING_MS", "100")),
        },
    }


def _load_cache() -> dict[str, Any]:
    if not AGENT_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(AGENT_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def ensure_agent(industry_dir: str | Path, public_url: str) -> dict[str, str]:
    """Create (or reuse) the retell-llm + agent for an industry, with fresh tool URLs.

    Cached ids come from RETELL_AGENT_ID/RETELL_LLM_ID or `.agents.json`; either way
    the llm is PATCHed every boot so `{PUBLIC_URL}/tool/...` matches this run's tunnel.
    """
    bp = load_blueprint(industry_dir)
    industry_name = Path(bp["industry_dir"]).name
    payload = _llm_payload(bp, public_url)

    cache = _load_cache()
    cached = cache.get(industry_name) or {}
    llm_id = os.environ.get("RETELL_LLM_ID", "").strip() or cached.get("llm_id")
    agent_id = os.environ.get("RETELL_AGENT_ID", "").strip() or cached.get("agent_id")

    if llm_id and agent_id:
        _call("PATCH", f"/update-retell-llm/{llm_id}", payload)
        _call("PATCH", f"/update-agent/{agent_id}", _agent_payload(llm_id, industry_name))
        return {"llm_id": llm_id, "agent_id": agent_id}

    llm_id = _call("POST", "/create-retell-llm", payload)["llm_id"]
    agent_id = _call("POST", "/create-agent", _agent_payload(llm_id, industry_name))["agent_id"]
    cache[industry_name] = {"llm_id": llm_id, "agent_id": agent_id}
    AGENT_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")
    return cache[industry_name]


def create_web_call(agent_id: str) -> dict[str, Any]:
    """→ {access_token, call_id}. The token is a LiveKit JWT for room web_call_<id>."""
    return _call("POST", "/v2/create-web-call", {"agent_id": agent_id})


def handoff_tool_names(bp: dict[str, Any]) -> dict[str, str]:
    """Retell's edge tool `transition_to_<state>` → the blueprint's handoff tool name."""
    return {
        f"transition_to_{t['handoff_to']}": t["name"]
        for entry in bp["agents"].values()
        for t in entry["tools"]
        if t.get("handoff")
    }


def platform_tool_calls(record: dict[str, Any], renames: dict[str, str]) -> list[dict[str, Any]]:
    """Call record → the tool calls Retell ran itself, as absolute-time span args.

    `type: "custom"` invocations are our webhook tools and already have a live span;
    everything else (edge transitions, end_call) ran platform-side and is invisible
    until we backfill it. `time_sec` is relative to the record's `start_timestamp`
    (epoch ms), so both become epoch nanoseconds.
    """
    entries = record.get("transcript_with_tool_calls") or []
    base_ns = int(record.get("start_timestamp") or 0) * 1_000_000
    if not base_ns:
        return []
    results = {
        e.get("tool_call_id"): e for e in entries if e.get("role") == "tool_call_result"
    }
    calls = []
    for e in entries:
        if e.get("role") != "tool_call_invocation" or e.get("type") == "custom":
            continue
        provider_name = e.get("name") or "unknown"
        start_ns = base_ns + int(float(e.get("time_sec") or 0) * 1e9)
        done = results.get(e.get("tool_call_id"))
        try:
            args = json.loads(e.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"raw": e.get("arguments")}
        calls.append(
            {
                "name": renames.get(provider_name, provider_name),
                "provider_name": provider_name,
                "arguments": args,
                "call_id": e.get("tool_call_id"),
                "start_ns": start_ns,
                # no result row for transitions/end_call — give them a visible sliver
                "end_ns": base_ns + int(float(done["time_sec"]) * 1e9)
                if done
                else start_ns + 10_000_000,
            }
        )
    return calls


async def get_call(call_id: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """Poll the call record — Retell finalizes tool calls a moment after hangup."""
    deadline = time.monotonic() + timeout
    record: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        while time.monotonic() < deadline:
            r = await client.get(
                f"{API_BASE}/v2/get-call/{call_id}",
                headers={"Authorization": f"Bearer {_api_key()}"},
            )
            if r.status_code == 200:
                record = r.json()
                if record.get("call_status") == "ended" and record.get(
                    "transcript_with_tool_calls"
                ):
                    return record
            await asyncio.sleep(1.0)
    return record


async def report_platform_tools(call_id: str, bp: dict[str, Any]) -> list[str]:
    """Backfill execute_tool spans for the tools Retell ran without telling us."""
    from report import record_past_tool_span

    calls = platform_tool_calls(await get_call(call_id), handoff_tool_names(bp))
    for c in calls:
        record_past_tool_span(
            c["name"],
            c["arguments"],
            {"success": True, "source": "retell_call_record"},
            start_ns=c["start_ns"],
            end_ns=c["end_ns"],
            call_id=c["call_id"],
            attributes={"mivas.provider.tool_name": c["provider_name"]},
        )
    return [c["name"] for c in calls]


async def _execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "schedule_appointment":
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{TOOL_SERVER_URL}/appointments", json={"date": args["date"]})
            resp.raise_for_status()
            return {"success": True, "date": resp.json()["date"]}
    return {"success": False, "error": f"unknown tool {name}"}


async def run_tool(name: str, args: dict[str, Any], *, call_id: str | None = None) -> dict[str, Any]:
    """Execute a webhook tool under a GenAI execute_tool span. Never raises — a tool
    failure becomes `{success: false, error: ...}` so the call (and OTel tree) finishes."""
    from report import call_offset_ms, finish_tool_span, tool_span
    with tool_span(name, args, call_id=call_id) as span:
        try:
            result = await _execute_tool(name, args)
            finish_tool_span(span, result, ok=True)
            return result
        except Exception as e:
            err = {"success": False, "error": f"{type(e).__name__}: {e}"}
            finish_tool_span(span, err, ok=False)
            return err
