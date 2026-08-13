"""Blueprint → Deepgram Voice Agent Settings helpers (raw websockets, no SDK).

Deepgram Voice Agent is a single WS session (`wss://agent.deepgram.com/v1/agent/converse`)
that bundles listen (STT) + think (LLM) + speak (TTS). Handoff is soft — the
Settings payload (and therefore the tool list) is fixed at connect, so all
blueprint tools are declared up front and the prompt carries the handoff note.
Session tools (end_call) end the agent session.
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
from call_id import headers as tool_headers, set_call_id  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")

WS_URL = "wss://agent.deepgram.com/v1/agent/converse"
DEEPGRAM_LISTEN_MODEL = os.environ.get("DEEPGRAM_LISTEN_MODEL", "flux-general-en")
DEEPGRAM_THINK_MODEL = os.environ.get("DEEPGRAM_THINK_MODEL", "gpt-4.1")
DEEPGRAM_SPEAK_MODEL = os.environ.get("DEEPGRAM_SPEAK_MODEL", "flux-hannah-en")
DEEPGRAM_GREETING = os.environ.get("DEEPGRAM_GREETING", "Thanks for calling! How can I help you today?")


def _speak_provider() -> dict[str, Any]:
    """Flux TTS (`flux-{voice}-{lang}`) is served on /v2/speak, so the provider
    needs `version: v2`; aura-* stays on the default v1."""
    provider: dict[str, Any] = {"type": "deepgram", "model": DEEPGRAM_SPEAK_MODEL}
    if DEEPGRAM_SPEAK_MODEL.startswith("flux-"):
        provider["version"] = "v2"
    return provider


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


def _function_spec(spec: dict) -> dict[str, Any]:
    """tools.json inputSchema → Deepgram's `functions[].parameters` shape."""
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    schema: dict[str, Any] = {"type": "object", "properties": dict(raw.get("properties") or {})}
    if raw.get("required"):
        schema["required"] = list(raw["required"])
    return {
        "name": spec["name"],
        "description": spec.get("description", spec["name"]),
        "parameters": schema,
    }


def settings_payload(bp: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """All blueprint tools are declared up front (Settings is fixed at connect).

    `model` is the harness/runtime identifier (e.g. "deepgram-voice-agent") used for
    tracing only — it is not an LLM name. The think-provider model always comes from
    DEEPGRAM_THINK_MODEL (or its default), independent of `model`.
    """
    functions = []
    seen: set[str] = set()
    for agent in bp["agents"].values():
        for t in agent["tools"]:
            name = t["name"]
            if name in seen or name not in bp["catalog"]:
                continue
            seen.add(name)
            functions.append(_function_spec(bp["catalog"][name]))

    start = bp["agents"][bp["start"]]
    # note soft-handoff: other agents' tools are visible; prompt still starts as bp["start"]
    prompt = start["instructions"] + _multiagent_note(bp)

    return {
        "type": "Settings",
        "audio": {
            "input": {"encoding": "linear16", "sample_rate": 24000},
            "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
        },
        "agent": {
            "language": "en",
            "listen": {"provider": {"type": "deepgram", "model": DEEPGRAM_LISTEN_MODEL}},
            "think": {
                "provider": {"type": "open_ai", "model": DEEPGRAM_THINK_MODEL},
                "prompt": prompt,
                "functions": functions,
            },
            "speak": {"provider": _speak_provider()},
            "greeting": DEEPGRAM_GREETING,
        },
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


async def run_session(industry_dir: str | Path, model: str, *, timeout: float = 12.0) -> None:
    """Connect, send Settings, print events until timeout. Text/audio-free smoke test."""
    from report import traced_run

    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise SystemExit("need DEEPGRAM_API_KEY")
    bp = load_blueprint(industry_dir)
    settings = settings_payload(bp, model)
    name = Path(industry_path(industry_dir)).name

    async with traced_run(f"mivas-{name}-{model}", model=model):
        async with websockets.connect(
            WS_URL, additional_headers={"Authorization": f"Token {key}"}
        ) as ws:
            await ws.send(json.dumps(settings))
            print("sent Settings", flush=True)
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                remaining = deadline - loop.time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    print(f"audio {len(msg)}", flush=True)
                    continue
                try:
                    event = json.loads(msg)
                except ValueError:
                    print(f"raw {msg[:200]}", flush=True)
                    continue
                etype = event.get("type")
                print(etype, flush=True)
                if etype in {"SettingsApplied", "Error"}:
                    break
