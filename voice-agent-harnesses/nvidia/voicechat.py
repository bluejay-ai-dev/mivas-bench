"""Nemotron VoiceChat (full-duplex S2S) — OpenAI Realtime-compatible WebSocket client.

Wire protocol: https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/
voicechat_realtime_instructions/api-reference.md

Multi-agent is soft (OpenAI/Grok style): one VoiceChat session for the whole
call. Handoff is a `session.update` to the target agent's pack + tools on the
SAME socket, so conversation history carries over and the specialist never
cold-opens. Each session.update advertises only the active agent's tools.

Tools are delivered in the model's own native format: `session.tools` plus the
`<AVAILABLE_TOOLS>`/`<TOOLCALL>` declaration a local NIM's template would inject
(hosted NVCF does not). That is the delivery mechanism, the equivalent of the
OpenAI Realtime `tools` field — NOT behavioural coaching. The pack prompt (shared
across every model harness) owns WHEN to use a tool. The harness does not infer
tool calls from spoken words: a tool fires only from a real `<TOOLCALL>` block or
a native function-call event. If the model narrates a booking without calling the
tool, nothing fires — that is a real model failure the benchmark should show.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from harness import (
    industry_path,
    load_blueprint,
    run_tool,
    tool_names,
)
from nvidia_fc import parse_toolcalls
from pack_clock import with_pack_clock

RUNTIME = "nemotron-voicechat"
MODEL = "nvidia/nemotron-voicechat"
# Hosted NVCF Realtime (ai-nemotron-voicechat). Override VOICECHAT_WS_URL for a
# local NIM (ws://127.0.0.1:9000/v1/realtime) or other remote.
DEFAULT_WS_URL = "wss://grpc.nvcf.nvidia.com/v1/realtime"
DEFAULT_FUNCTION_ID = ""  # hosted NVCF requires VOICECHAT_FUNCTION_ID
SAMPLE_RATE = 24_000  # wire format both ways; server resamples to 16k / 22.05k

# Bare native tool declaration — no behavioural coaching. A local NIM injects an
# equivalent block via its serving template; hosted NVCF does not, so we declare
# the same tools + call syntax and nothing more. This is tool *delivery*, on par
# with the OpenAI Realtime `tools` field; the pack prompt owns WHEN to call.
_TOOLS_DECL = (
    "\n\n<AVAILABLE_TOOLS>{tools}</AVAILABLE_TOOLS>\n\n"
    "To call a tool, output:\n"
    '<TOOLCALL>[{{"name": "tool_name", "arguments": {{"param": "value"}}}}]</TOOLCALL>\n'
    "Tool results are returned to you as <TOOL_RESPONSE>[{{...}}]</TOOL_RESPONSE>.\n"
)


def ws_url() -> str:
    return os.environ.get("VOICECHAT_WS_URL", DEFAULT_WS_URL).rstrip("/")


def speaks_first() -> bool:
    """Whether the active agent should open the call (speech-shaped kick + trail silence).

    Pure zero-PCM is not enough on hosted VoiceChat — it yields near-silent frames
    with an empty transcript. The CHIRP bridge kicks with a short speech-shaped WAV
    then feeds trailing silence only while the agent is producing audible audio.
    """
    return os.environ.get("VOICECHAT_SPEAKS_FIRST", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def ws_headers() -> dict[str, str]:
    """Auth for hosted NVCF; empty for unauthenticated local NIM."""
    url = ws_url()
    if "nvcf.nvidia.com" not in url:
        return {}
    key = (os.environ.get("NVIDIA_API_KEY") or os.environ.get("NGC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("NVIDIA_API_KEY required for hosted VoiceChat (wss://…nvcf…)")
    fid = (os.environ.get("VOICECHAT_FUNCTION_ID") or DEFAULT_FUNCTION_ID).strip()
    if not fid:
        raise RuntimeError("VOICECHAT_FUNCTION_ID required for hosted VoiceChat (wss://…nvcf…)")
    headers = {
        "Authorization": f"Bearer {key}",
        # NVCF gateway accepts this casing; NVCF-FUNCTION-ID alone is flaky.
        "function-id": fid,
    }
    vid = (os.environ.get("VOICECHAT_FUNCTION_VERSION_ID") or "").strip()
    if vid:
        headers["NVCF-FUNCTION-VERSION-ID"] = vid
    return headers


def connect_voicechat():
    """Return a websockets connect CM for VoiceChat (local or hosted)."""
    import websockets

    return websockets.connect(ws_url(), additional_headers=ws_headers())


def _ascii(text: str) -> str:
    """VoiceChat requires ASCII-only system prompts and tool responses."""
    return text.encode("ascii", "replace").decode("ascii")


def _event_id() -> str:
    return str(uuid.uuid4())


def _tool_decl(spec: dict) -> dict[str, Any]:
    """Bare VoiceChat tool object: name, description, parameters (+ any pack acks).

    No harness-injected ack phrases or handoff hints. The model calls tools
    natively via <TOOLCALL>; the pack prompt owns when. ``ack_messages`` is only
    passed through if the industry catalog itself defines it (spoken filler while
    a tool runs), which is pack policy, not a Nemotron crutch.
    """
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
    catalog_acks = spec.get("ack_messages")
    if isinstance(catalog_acks, list) and catalog_acks:
        out["ack_messages"] = [_ascii(str(a)) for a in catalog_acks if str(a).strip()]
    return out


def _available_tools_json(tools: list[dict[str, Any]]) -> str:
    slim = [
        {
            "name": t["name"],
            "description": t.get("description", t["name"]),
            "parameters": t.get("parameters") or {"type": "object", "properties": {}},
        }
        for t in tools
    ]
    return _ascii(json.dumps(slim, separators=(",", ":")))


def session_update_for_agent(bp: dict[str, Any], agent: str) -> dict[str, Any]:
    """Pack instructions + that agent's tools only + native tool declaration."""
    if agent not in bp["agents"]:
        raise KeyError(f"unknown agent {agent!r}")
    tools: list[dict[str, Any]] = []
    for name in tool_names(bp, agent):
        tools.append(_tool_decl(bp["catalog"][name]))
    pack = _ascii(bp["agents"][agent]["instructions"])
    instructions = with_pack_clock(pack, bp.get("industry_dir"))
    if tools:
        instructions = instructions + _TOOLS_DECL.format(tools=_available_tools_json(tools))
    return {
        "type": "session.update",
        "event_id": _event_id(),
        "session": {
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
                "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
            },
            "instructions": instructions,
            "tools": tools,
        },
    }


def advertised_tools(industry_dir: str | Path, agent: str | None = None) -> list[str]:
    """Tool names for one agent (default: start agent)."""
    bp = load_blueprint(industry_dir)
    name = agent or bp["start"]
    return [t["name"] for t in session_update_for_agent(bp, name)["session"]["tools"]]


def handoff_role(result: dict[str, Any], bp: dict[str, Any]) -> str | None:
    """Return the next agent name if this tool result is a handoff."""
    role = result.get("role")
    return role if isinstance(role, str) and role in bp["agents"] else None


def handoff_nudge_event() -> dict[str, Any]:
    """Bare response.create after same-session session.update (call history intact)."""
    return {"type": "response.create", "event_id": _event_id()}


async def handle_function_call(
    name: str,
    arguments: str | dict,
    call_id: str,
    bp: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """Run a tool and build the conversation.item.create reply.

    Returns (result, should_end_call, outbound_event).
    `run_tool` updates `state["agent"]` on handoff.
    """
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
    elif isinstance(arguments, dict):
        args = dict(arguments)
    else:
        args = {}

    allowed = tool_names(bp, state["agent"])
    if name not in allowed:
        result: dict[str, Any] = {
            "success": False,
            "error": f"tool {name!r} not available to agent {state['agent']!r}",
        }
        stop = False
    else:
        result, stop = await run_tool(name, args, bp, state, call_id=call_id)

    output = _ascii(f"<TOOL_RESPONSE>[{json.dumps(result, separators=(',', ':'))}]</TOOL_RESPONSE>")
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
    """Smoke: open one session per agent, session.update each, then close."""
    from report import traced_run

    bp = load_blueprint(industry_dir)
    name = Path(industry_path(industry_dir)).name

    async with traced_run(f"mivas-{name}-{model}", model=model):
        for agent in bp["agents"]:
            async with connect_voicechat() as vc:
                created = json.loads(await asyncio.wait_for(vc.recv(), timeout=30))
                print(f"{agent} {created.get('type')}", flush=True)
                await vc.send(json.dumps(session_update_for_agent(bp, agent)))
                updated = json.loads(await asyncio.wait_for(vc.recv(), timeout=30))
                n = len((updated.get("session") or {}).get("tools") or [])
                print(f"{agent} {updated.get('type')} tools={n}", flush=True)
                await vc.send(
                    json.dumps({"type": "session.close", "event_id": _event_id()})
                )
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    while True:
                        raw = await asyncio.wait_for(vc.recv(), timeout=5)
                        if json.loads(raw).get("type") == "session.end":
                            break


def demo() -> None:
    """Offline blueprint/tool-shape check (no network). No inference: tools fire
    only from native <TOOLCALL> / function-call events."""
    bp = load_blueprint("control-industry")
    start = bp["start"]
    start_tools = advertised_tools("control-industry", start)
    assert tool_names(bp, start) == start_tools
    all_names = {n for a in bp["agents"] for n in tool_names(bp, a)}
    for agent, names in ((a, tool_names(bp, a)) for a in bp["agents"]):
        update = session_update_for_agent(bp, agent)
        instr = update["session"]["instructions"]
        pack = _ascii(bp["agents"][agent]["instructions"])
        assert instr.startswith(pack)
        assert [t["name"] for t in update["session"]["tools"]] == names
        assert "<AVAILABLE_TOOLS>" in instr
        for n in names:
            assert n in instr
        for n in all_names - set(names):
            assert n not in instr, f"{agent} instructions leaked {n}"
        assert "# Tool calling" not in instr
        assert "# Multi-agent note" not in instr
        # No behavioural coaching injected by the harness.
        assert "BEFORE you speak" not in instr
        assert "Do not claim" not in instr
        # No harness-injected ack-phrase crutch on any tool (control-industry
        # catalog defines none, so no ack_messages key should appear).
        for t in update["session"]["tools"]:
            assert "ack_messages" not in t, f"{agent}/{t['name']} carries ack phrases"
            assert set(t) <= {"name", "description", "parameters", "ack_messages"}
    # Native tool protocol still parses.
    assert parse_toolcalls(
        '<TOOLCALL>[{"name": "handoff_to_scheduler", "arguments": {}}]</TOOLCALL>'
    ) == [{"name": "handoff_to_scheduler", "arguments": {}}]
    assert parse_toolcalls(
        '<TOOLCALL>[{"name": "schedule_appointment", '
        '"arguments": {"date": "08/18/2026"}}]</TOOLCALL>'
    ) == [{"name": "schedule_appointment", "arguments": {"date": "08/18/2026"}}]
    # Plain spoken confirmation without a <TOOLCALL> yields NO tool call.
    assert parse_toolcalls("Your appointment is scheduled for March 18, 2026.") == []
    assert handoff_nudge_event()["type"] == "response.create"
    if len(bp["agents"]) > 1 and all_names - set(start_tools):
        assert set(start_tools) != all_names
    print(
        f"voicechat self-check ok start={start} tools={start_tools} "
        f"agents={list(bp['agents'])} speaks_first={speaks_first()} ws={ws_url()}"
    )


if __name__ == "__main__":
    demo()
