"""Blueprint -> Rime Prism realtime session helpers.

Prism is Rime's speech-to-speech model behind one WebSocket
(wss://prism.api.rime.ai/v1/realtime). The wire protocol uses the OpenAI
Realtime beta event names with Prism-specific metadata: every requested reply
needs `response.metadata.prism_cause`, and a tool round is
`response.function_call_arguments.done` -> `conversation.item.create`
(function_call_output) -> wait for `response.done` plus one
`conversation.item.created` per result -> `response.create` with
`prism_cause: tool_continuation`.

Industry tools POST to `{TOOL_SERVER_URL}/tools/{name}`. Handoff tools
(`handoff: true`) soft-switch the active blueprint agent on the same socket
via a later `session.update` (instructions + tools are replaceable mid-call;
voice and turn timing stay). `end_call` is harness-local; other session tools
(`transfer_to_human`, `escalate_to_human`) POST so the industry records the
row, then the socket closes after the farewell.

Docs: https://docs.rime.ai/api-reference/prism/websocket
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import headers as tool_headers, log_ws_accept, set_call_id  # noqa: E402
from pack_clock import with_pack_clock  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")

RUNTIME = "prism"
MODEL = os.environ.get("RIME_MODEL", "prism")
VOICE = os.environ.get("RIME_VOICE", "astra")
DEFAULT_WS_URL = "wss://prism.api.rime.ai/v1/realtime"
INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
# Prism speaks a filler line ("One moment.") while a tool runs unless told not
# to. Tool POSTs hit a local server in milliseconds and the packs forbid
# narrating handoffs, so the default is silence. RIME_TOOL_FILLER=auto restores
# Prism's own line for industry tools; handoffs and session tools stay silent.
TOOL_FILLER = (os.environ.get("RIME_TOOL_FILLER", "none").strip().lower() or "none")


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
    catalog = {
        t["name"]: t
        for t in json.loads((industry_dir / "tools.json").read_text())["tools"]
    }
    agents = {
        entry["name"]: {
            "name": entry["name"],
            "instructions": (industry_dir / entry["system_prompt"]).read_text(),
            "tools": entry["tools"],
        }
        for entry in blueprint["agents"]
    }
    return {
        "industry_dir": industry_dir,
        "start": blueprint["agents"][0]["name"],
        "greeting": str(blueprint.get("greeting") or "").strip(),
        "agents": agents,
        "catalog": catalog,
    }


def api_key() -> str:
    key = os.environ.get("RIME_API_KEY", "").strip()
    if not key:
        raise SystemExit("need RIME_API_KEY")
    return key


def ws_url() -> str:
    return os.environ.get("RIME_WS_URL", "").strip().rstrip("/") or DEFAULT_WS_URL


def ws_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}"}


def connect_prism():
    import websockets

    # Prism rejects frames over 1 MiB and sends audio faster than realtime;
    # a large read limit keeps the client from closing on a burst.
    return websockets.connect(
        ws_url(), additional_headers=ws_headers(), max_size=4 * 1024 * 1024, open_timeout=10
    )


def tool_server_url() -> str:
    return os.environ.get("TOOL_SERVER_URL", TOOL_SERVER_URL).rstrip("/")


def tool_names(bp: dict[str, Any], agent: str) -> list[str]:
    return [t["name"] for t in bp["agents"][agent]["tools"] if t["name"] in bp["catalog"]]


def tool_entry(bp: dict[str, Any], agent: str, tool: str) -> dict[str, Any] | None:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == tool:
            return t
    return None


def handoff_target(bp: dict[str, Any], agent: str, tool: str) -> str | None:
    entry = tool_entry(bp, agent, tool)
    if entry and entry.get("handoff"):
        target = entry.get("handoff_to")
        return target if target in bp["agents"] else None
    return None


def is_session_tool(bp: dict[str, Any], agent: str, tool: str) -> bool:
    entry = tool_entry(bp, agent, tool)
    return bool(entry and entry.get("session"))


def handoff_role(result: dict[str, Any], bp: dict[str, Any]) -> str | None:
    role = result.get("role")
    return role if isinstance(role, str) and role in bp["agents"] else None


def _event_id(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _tool_decl(spec: dict[str, Any], entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prism flat tool shape: {type, name, description, parameters, prism_filler}."""
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    raw.pop("additionalProperties", None)
    props = raw.get("properties")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": dict(props) if isinstance(props, dict) else {},
    }
    if raw.get("required"):
        parameters["required"] = list(raw["required"])
    quiet = TOOL_FILLER == "none" or bool(entry and (entry.get("handoff") or entry.get("session")))
    return {
        "type": "function",
        "name": spec["name"],
        "description": spec.get("description", spec["name"]),
        "parameters": parameters,
        "prism_filler": {"type": "none"} if quiet else {"type": "auto"},
    }


def session_update_for_agent(
    bp: dict[str, Any],
    agent: str,
    *,
    mid_call: bool = False,
    sim_id: str | None = None,
) -> dict[str, Any]:
    if agent not in bp["agents"]:
        raise KeyError(f"unknown agent {agent!r}")
    tools = [
        _tool_decl(bp["catalog"][name], tool_entry(bp, agent, name))
        for name in tool_names(bp, agent)
    ]
    session: dict[str, Any] = {
        "instructions": with_pack_clock(
            bp["agents"][agent]["instructions"], bp.get("industry_dir")
        ),
        "tools": tools,
    }
    # voice, turn timing and client caps are first-update settings; eagerness
    # and the end-of-turn timeout are locked for the rest of the conversation.
    if not mid_call:
        turn: dict[str, Any] = {"interrupt_response": True}
        eagerness = os.environ.get("RIME_EAGERNESS", "").strip().lower()
        if eagerness:
            turn["eagerness"] = eagerness
        eot = os.environ.get("RIME_END_OF_TURN_TIMEOUT_MS", "").strip()
        if eot:
            turn["prism_end_of_turn_timeout_ms"] = int(eot)
        session.update({
            "voice": VOICE,
            "turn_detection": turn,
            "prism_client_caps": ["playback_drained"],
        })
        if sim_id:
            session["prism_response_metadata"] = {"sim_id": str(sim_id)[:512]}
    return {
        "type": "session.update",
        "event_id": _event_id("upd"),
        "session": session,
    }


def advertised_tools(industry_dir: str | Path, agent: str | None = None) -> list[str]:
    bp = load_blueprint(industry_dir)
    name = agent or bp["start"]
    return [t["name"] for t in session_update_for_agent(bp, name)["session"]["tools"]]


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    target = handoff_target(bp, state["agent"], name)
    if target:
        state["agent"] = target
        return {"success": True, "role": target}, False

    if name == "end_call":
        return {"success": True}, True

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{tool_server_url()}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        result = resp.json()
        # Human-transfer session tools POST (so hangup GET /state has the row)
        # then hang up: there is no human to join.
        if is_session_tool(bp, state["agent"], name):
            return result, True
        return result, False


async def run_tool(
    name: str,
    args: dict[str, Any],
    bp: dict[str, Any],
    state: dict[str, Any],
    *,
    call_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    from report import finish_tool_span, tool_span

    parent = state.get("_otel_root")
    with tool_span(name, args, call_id=call_id, parent=parent) as span:
        try:
            result, stop = await _execute_tool(name, args, bp, state)
            # tool servers answer {"ok": true}; only transfer_*/end_call use "success"
            ok = bool(result.get("ok") or result.get("success"))
        except Exception as e:  # noqa: BLE001
            result, stop, ok = (
                {"success": False, "error": f"{type(e).__name__}: {e}"},
                False,
                False,
            )
        finish_tool_span(span, result, ok=ok)
        return result, stop


def function_call_output(call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "conversation.item.create",
        "event_id": f"result_{call_id}",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result),
        },
    }


def greeting_nudge(bp: dict[str, Any]) -> dict[str, Any]:
    """Prism speaks first only when asked. The stage prompts assume the branded
    greeting was already spoken, so the nudge carries the pack's own line."""
    greeting = bp.get("greeting") or ""
    if greeting:
        instruction = (
            "The call just connected and the caller has said nothing yet. "
            f'Speak first. Your first words are exactly: "{greeting}" '
            "Say it once, then stop and wait for the caller."
        )
    else:
        instruction = "The call just connected. Greet the caller and ask how you can help."
    return {
        "type": "response.create",
        "event_id": _event_id("nudge"),
        "response": {
            "prism_instruction": instruction,
            "metadata": {"prism_cause": "proactive"},
        },
    }


def tool_continuation(parent_response_id: str) -> dict[str, Any]:
    return {
        "type": "response.create",
        "event_id": _event_id("cont"),
        "response": {
            "metadata": {
                "prism_cause": "tool_continuation",
                "prism_parent_response_id": parent_response_id,
            }
        },
    }


async def handle_function_call(
    name: str,
    arguments: str | dict,
    call_id: str,
    bp: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
    else:
        args = dict(arguments or {})
    if not isinstance(args, dict):
        args = {}
    result, stop = await run_tool(name, args, bp, state, call_id=call_id)
    return result, stop, function_call_output(call_id, result)


async def configure_session(
    ws, agent: str, bp: dict[str, Any], *, timeout: float = 10.0, sim_id: str | None = None,
    mid_call: bool = False,
) -> dict:
    import asyncio

    update = session_update_for_agent(bp, agent, mid_call=mid_call, sim_id=sim_id)
    await ws.send(json.dumps(update))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(raw, bytes):
            continue
        ev = json.loads(raw)
        et = ev.get("type")
        if et == "session.updated":
            return ev
        if et == "error":
            raise RuntimeError(f"session.update failed for {agent}: {ev.get('error')}")


async def run_session(industry_dir: str | Path, *, model: str = MODEL) -> None:
    """Connectivity check: open one Prism session and push every stage's config."""
    import asyncio
    import contextlib

    from report import traced_run

    bp = load_blueprint(industry_dir)
    name = Path(industry_path(industry_dir)).name

    async with traced_run(f"mivas-{name}-{model}", model=model):
        async with connect_prism() as prism:
            created = json.loads(await asyncio.wait_for(prism.recv(), timeout=10))
            print(f"session {created.get('type')}", flush=True)
            first = True
            for agent in bp["agents"]:
                updated = await configure_session(prism, agent, bp, mid_call=not first)
                first = False
                n = len((updated.get("session") or {}).get("tools") or [])
                print(f"{agent} {updated.get('type')} tools={n} ok", flush=True)
            with contextlib.suppress(Exception):
                await prism.close()


def demo() -> None:
    bp = load_blueprint("control-industry")
    start = bp["start"]
    start_tools = advertised_tools("control-industry", start)
    assert tool_names(bp, start) == start_tools
    all_names = {n for a in bp["agents"] for n in tool_names(bp, a)}
    for agent, names in ((a, tool_names(bp, a)) for a in bp["agents"]):
        update = session_update_for_agent(bp, agent, sim_id="sim1")
        session = update["session"]
        pack = bp["agents"][agent]["instructions"]
        assert session["instructions"].startswith(pack.rstrip())
        assert "CALENDAR: Today is" in session["instructions"]
        assert [t["name"] for t in session["tools"]] == names
        assert all(t.get("type") == "function" and "parameters" in t for t in session["tools"])
        assert all(t["prism_filler"]["type"] in ("none", "auto") for t in session["tools"])
        assert session["turn_detection"]["interrupt_response"] is True
        assert session["voice"] == VOICE
        assert session["prism_response_metadata"] == {"sim_id": "sim1"}
        for n in all_names - set(names):
            assert n not in [t["name"] for t in session["tools"]], f"{agent} leaked {n}"
    mid = session_update_for_agent(bp, start, mid_call=True)["session"]
    assert "turn_detection" not in mid and "voice" not in mid
    assert "instructions" in mid and "tools" in mid
    nudge = greeting_nudge(bp)
    assert nudge["response"]["metadata"]["prism_cause"] == "proactive"
    if bp["greeting"]:
        assert bp["greeting"] in nudge["response"]["prism_instruction"]
    cont = tool_continuation("resp_1")
    assert cont["response"]["metadata"]["prism_parent_response_id"] == "resp_1"
    out = function_call_output("call_1", {"ok": True})
    assert out["item"]["type"] == "function_call_output" and out["event_id"] == "result_call_1"
    state = {"agent": start}
    target = handoff_name = None
    for name in start_tools:
        cand = handoff_target(bp, start, name)
        if cand:
            target, handoff_name = cand, name
            break
    if target and handoff_name:
        import asyncio

        decl = next(t for t in session_update_for_agent(bp, start)["session"]["tools"]
                    if t["name"] == handoff_name)
        assert decl["prism_filler"] == {"type": "none"}, "handoffs must be silent"
        res, stop = asyncio.run(run_tool(handoff_name, {}, bp, state))
        assert res.get("success") and res.get("role") == target and not stop
        assert state["agent"] == target
    print(
        f"rime self-check ok start={start} tools={start_tools} "
        f"agents={list(bp['agents'])} model={MODEL} voice={VOICE}"
    )


if __name__ == "__main__":
    demo()
