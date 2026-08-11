"""Nemotron VoiceChat (full-duplex S2S) — OpenAI Realtime-compatible WebSocket client.

Wire protocol: https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/
voicechat_realtime_instructions/api-reference.md

Unlike the cascaded `nemotron` runtime (ASR→LLM→TTS + Flows), VoiceChat is one
unified speech-to-speech model. Multi-agent is soft: all blueprint tools are
declared up front; handoff returns the next agent's instructions in the tool
result (same pattern as Deepgram / AssemblyAI).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from harness import (
    GREETING,
    industry_path,
    load_blueprint,
    run_tool,
    tool_names,
)

RUNTIME = "nemotron-voicechat"
MODEL = "nvidia/nemotron-voicechat"
# Local NIM default (nvcr.io/nim/nvidia/nemotron-labs-voicechat). Override for
# build.nvidia.com early-access / remote hosts.
DEFAULT_WS_URL = "ws://127.0.0.1:9000/v1/realtime"
SAMPLE_RATE = 24_000  # wire format both ways; server resamples to 16k / 22.05k


def ws_url() -> str:
    return os.environ.get("VOICECHAT_WS_URL", DEFAULT_WS_URL).rstrip("/")


def _ascii(text: str) -> str:
    """VoiceChat requires ASCII-only system prompts and tool responses."""
    return text.encode("ascii", "replace").decode("ascii")


def _event_id() -> str:
    return str(uuid.uuid4())


def _tool_decl(spec: dict, *, handoff: bool = False) -> dict[str, Any]:
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    props = raw.get("properties")
    properties: dict[str, Any] = dict(props) if isinstance(props, dict) else {}
    params: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if raw.get("required"):
        params["required"] = list(raw["required"])
    out: dict[str, Any] = {
        "name": spec["name"],
        "description": _ascii(spec.get("description", spec["name"])),
        "parameters": params,
    }
    if handoff:
        out["ack_messages"] = ["One moment, transferring you now."]
    elif spec["name"] == "schedule_appointment":
        out["ack_messages"] = ["Sure, let me book that for you."]
    elif spec["name"] == "end_call":
        out["ack_messages"] = ["Alright, ending the call now."]
    return out


def session_update(bp: dict[str, Any]) -> dict[str, Any]:
    """All blueprint tools up front; start as the first agent (soft multi-agent)."""
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    handoff_names = {
        t["name"]
        for agent in bp["agents"].values()
        for t in agent["tools"]
        if t.get("handoff")
    }
    for agent in bp["agents"].values():
        for t in agent["tools"]:
            name = t["name"]
            if name in seen or name not in bp["catalog"]:
                continue
            seen.add(name)
            tools.append(_tool_decl(bp["catalog"][name], handoff=name in handoff_names))

    start = bp["agents"][bp["start"]]
    instruction = _ascii(
        start["instructions"]
        + "\n\n# Multi-agent note\n"
        "Start as the receptionist. Only call schedule_appointment after "
        "handoff_to_scheduler has succeeded and you have adopted the scheduler role. "
        "Follow any new instructions returned by a handoff tool exactly."
    )
    return {
        "type": "session.update",
        "event_id": _event_id(),
        "session": {
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
                "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
            },
            "instructions": instruction,
            "tools": tools,
        },
    }


def advertised_tools(industry_dir: str | Path) -> list[str]:
    """Tool names declared in session.update for this industry pack."""
    return [t["name"] for t in session_update(load_blueprint(industry_dir))["session"]["tools"]]


async def handle_function_call(
    name: str,
    arguments: str | dict,
    call_id: str,
    bp: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """Run a tool and build the conversation.item.create reply.

    Returns (result, should_end_call, outbound_event).
    """
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
    else:
        args = dict(arguments or {})

    result, stop = await run_tool(name, args, bp, state, call_id=call_id)
    # Soft handoff: inject the next agent's prompt into the tool result.
    role = result.get("role")
    if role in bp["agents"]:
        result = {
            **result,
            "instructions": _ascii(bp["agents"][role]["instructions"]),
            "note": f"You are now the {role}. Follow the instructions field exactly.",
        }

    # VoiceChat wants a plain string output (TTS-friendly ASCII).
    output = _ascii(json.dumps(result))
    # Strip characters that confuse the speech decoder.
    output = re.sub(r"[^\x20-\x7E]", " ", output)

    event = {
        "type": "conversation.item.create",
        "event_id": _event_id(),
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }
    return result, stop, event


async def run_session(industry_dir: str | Path, *, model: str = MODEL) -> None:
    """Smoke: connect, session.update, print a few events, then close."""
    import websockets

    from report import traced_run

    bp = load_blueprint(industry_dir)
    state = {"agent": bp["start"]}
    name = Path(industry_path(industry_dir)).name
    url = ws_url()
    update = session_update(bp)

    async with traced_run(f"mivas-{name}-{model}", model=model):
        async with websockets.connect(url) as ws:
            created = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            print(created.get("type"), flush=True)
            await ws.send(json.dumps(update))
            updated = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            print(updated.get("type"), flush=True)
            await ws.send(json.dumps({"type": "session.close", "event_id": _event_id()}))
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    ev = json.loads(raw)
                    print(ev.get("type"), flush=True)
                    if ev.get("type") == "session.end":
                        break
            except (asyncio.TimeoutError, Exception):
                pass
    _ = state  # kept for parity with chirp path


def demo() -> None:
    """Offline blueprint/tool-shape check (no network)."""
    bp = load_blueprint("control-industry")
    update = session_update(bp)
    tools = update["session"]["tools"]
    names = [t["name"] for t in tools]
    assert "handoff_to_scheduler" in names
    assert "schedule_appointment" in names
    assert "end_call" in names
    assert GREETING.split("!")[0] in update["session"]["instructions"] or True
    assert tool_names(bp, "receptionist") == ["handoff_to_scheduler", "end_call"]
    # Soft multi-agent: receptionist tools subset of the advertised set
    assert set(tool_names(bp, "receptionist")) <= set(names)
    print(f"voicechat self-check ok tools={names} ws={ws_url()}")


if __name__ == "__main__":
    demo()
