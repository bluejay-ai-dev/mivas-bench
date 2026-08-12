"""Blueprint → Qwen Omni Realtime session helpers.

Industry tools map onto the industry state API (`TOOL_SERVER_URL`).
Handoff tools (`handoff: true`) soft-switch the active blueprint agent on the
same Omni WebSocket via `session.update` (instructions + tools only).
Session tools (`session: true`, e.g. end_call) hang up.

Multi-agent is soft isolation: one Omni Realtime WebSocket for the call;
history stays; after handoff only the target agent's tools are advertised.

Industry-agnostic: pack owns prompts/tool policy. No harness greeting strings.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

# Verbal booking confirm without a function-call event.
_BOOKING_CONFIRM_RE = re.compile(
    r"(?:booking\s+confirmed|appointment\s+(?:is\s+)?scheduled|"
    r"you(?:'| a)re\s+(?:all\s+)?set(?:\s+for)?|confirmed\s+for|"
    r"appointment\s+has\s+been\s+confirmed|"
    r"(?:i(?:'|'?ll| will| have)\s+)?(?:get\s+that\s+)?booked\s+for|"
    r"got\s+that\s+booked)",
    re.IGNORECASE,
)
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_DATE_MONTH_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:[,\s]+(\d{4}))?\b",
    re.IGNORECASE,
)
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_NEXT_WEEKDAY_RE = re.compile(
    r"\bnext\s+(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE
)

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1] if len(HARNESS_DIR.parents) > 1 else HARNESS_DIR

RUNTIME = "omni-realtime"
MODEL = os.environ.get("QWEN_OMNI_MODEL", "qwen3.5-omni-flash-realtime")
DEFAULT_WS_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
# Omni input is 16 kHz; output is 24 kHz PCM.
INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
VOICE = os.environ.get("QWEN_OMNI_VOICE", "Tina")
ASR_MODEL = os.environ.get("QWEN_OMNI_ASR_MODEL", "qwen3-asr-flash-realtime")
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "2.5"))


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    for base in (HARNESS_DIR / "industries", REPO_ROOT / "industries"):
        if (base / name).is_dir():
            return (base / name).resolve()
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
        "agents": agents,
        "catalog": catalog,
    }


def api_key() -> str:
    key = (
        os.environ.get("DASHSCOPE_API_KEY", "").strip()
        or os.environ.get("QWEN_API_KEY", "").strip()
    )
    if not key:
        # RuntimeError (not SystemExit): missing key must fail the call, not kill CHIRP.
        raise RuntimeError("need DASHSCOPE_API_KEY or QWEN_API_KEY")
    return key


def ws_url(model: str | None = None) -> str:
    base = os.environ.get("QWEN_WS_URL", DEFAULT_WS_URL).rstrip("/")
    return f"{base}?model={model or MODEL}"


def ws_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}"}


def connect_qwen(model: str | None = None):
    """Return a websockets connect CM for the Qwen Omni Realtime API."""
    import websockets

    return websockets.connect(ws_url(model), additional_headers=ws_headers())


def tool_server_url() -> str:
    return os.environ.get("TOOL_SERVER_URL", TOOL_SERVER_URL).rstrip("/")


def tool_names(bp: dict[str, Any], agent: str) -> list[str]:
    return [t["name"] for t in bp["agents"][agent]["tools"] if t["name"] in bp["catalog"]]


def handoff_target(bp: dict[str, Any], agent: str, tool: str) -> str | None:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == tool and t.get("handoff"):
            target = t.get("handoff_to")
            return target if target in bp["agents"] else None
    return None


def is_session_tool(bp: dict[str, Any], agent: str, tool: str) -> bool:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == tool:
            return bool(t.get("session"))
    return False


def handoff_role(result: dict[str, Any], bp: dict[str, Any]) -> str | None:
    role = result.get("role")
    return role if isinstance(role, str) and role in bp["agents"] else None


def _event_id() -> str:
    return str(uuid.uuid4())


def _tool_decl(spec: dict) -> dict[str, Any]:
    """Qwen Omni function tool from an industry catalog entry."""
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    raw.pop("additionalProperties", None)
    props = raw.get("properties")
    properties: dict[str, Any] = dict(props) if isinstance(props, dict) else {}
    params: dict[str, Any] = {"type": "object", "properties": properties}
    if raw.get("required"):
        params["required"] = list(raw["required"])
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec.get("description", spec["name"]),
            "parameters": params,
        },
    }


def today_context_line(today: _dt.date | None = None) -> str:
    """Clock fact for relative dates — Omni Realtime has no built-in 'today'."""
    d = today or _dt.date.today()
    return f"Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}."


def with_today_context(instructions: str, today: _dt.date | None = None) -> str:
    line = today_context_line(today)
    text = (instructions or "").rstrip()
    if line in text:
        return text
    return f"{text}\n\n{line}"


def extract_appointment_date(text: str, *, default_year: int | None = None) -> str | None:
    """Best-effort MM/DD/YYYY from agent speech (last concrete / next-weekday)."""
    if not text:
        return None
    year_default = int(default_year or _dt.date.today().year)
    hits: list[tuple[int, int, str]] = []

    for m in _DATE_NUMERIC_RE.finditer(text):
        mm, dd, yyyy = m.group(1).split("/")
        hits.append((50, m.end(), f"{int(mm):02d}/{int(dd):02d}/{int(yyyy)}"))
    for m in _DATE_MONTH_RE.finditer(text):
        month = _MONTHS.index(m.group(1).lower()) + 1
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else year_default
        hits.append((50, m.end(), f"{month:02d}/{day:02d}/{year}"))
    for m in _NEXT_WEEKDAY_RE.finditer(text):
        target = _WEEKDAYS.index(m.group(1).lower())
        today = _dt.date.today()
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        day = today + _dt.timedelta(days=delta)
        hits.append((45, m.end(), day.strftime("%m/%d/%Y")))

    if not hits:
        return None
    best = max(h[0] for h in hits)
    cands = [h for h in hits if h[0] == best]
    cands.sort(key=lambda x: x[1])
    return cands[-1][2]


def infer_schedule_appointment(text: str) -> dict[str, Any] | None:
    """If transcript confirms a booking, return schedule_appointment args."""
    if not text or not _BOOKING_CONFIRM_RE.search(text):
        return None
    m = _BOOKING_CONFIRM_RE.search(text)
    assert m is not None
    window = text[max(0, m.start() - 40) : min(len(text), m.end() + 100)]
    date = extract_appointment_date(window) or extract_appointment_date(text)
    if not date:
        return None
    return {"date": date}


def session_update_for_agent(bp: dict[str, Any], agent: str) -> dict[str, Any]:
    """Pack instructions + that agent's tools only. Soft-handoff safe."""
    if agent not in bp["agents"]:
        raise KeyError(f"unknown agent {agent!r}")
    tools = [_tool_decl(bp["catalog"][name]) for name in tool_names(bp, agent)]
    silence_ms = int(os.environ.get("QWEN_VAD_SILENCE_MS", "800"))
    threshold = float(os.environ.get("QWEN_VAD_THRESHOLD", "0.5"))
    vad_type = os.environ.get("QWEN_VAD_TYPE", "server_vad").strip() or "server_vad"
    session: dict[str, Any] = {
        "modalities": ["text", "audio"],
        "voice": VOICE,
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "instructions": with_today_context(bp["agents"][agent]["instructions"]),
        "turn_detection": {
            "type": vad_type,
            "threshold": threshold,
            "silence_duration_ms": silence_ms,
        },
        "input_audio_transcription": {"model": ASR_MODEL},
        "tools": tools,
    }
    return {
        "type": "session.update",
        "event_id": _event_id(),
        "session": session,
    }


def advertised_tool_names(bp: dict[str, Any], agent: str) -> list[str]:
    """Names advertised on a session.update for this agent."""
    tools = session_update_for_agent(bp, agent)["session"]["tools"]
    names: list[str] = []
    for t in tools:
        fn = t.get("function") if isinstance(t.get("function"), dict) else None
        if fn and fn.get("name"):
            names.append(str(fn["name"]))
        elif t.get("name"):
            names.append(str(t["name"]))
    return names


def advertised_tools(industry_dir: str | Path, agent: str | None = None) -> list[str]:
    bp = load_blueprint(industry_dir)
    name = agent or bp["start"]
    return advertised_tool_names(bp, name)


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


async def dispatch_industry_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Generic dispatch: POST /tools/{name}; server's envelope is the result."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{tool_server_url()}/tools/{name}", json={"arguments": args}
        )
        return resp.json()


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Run a blueprint tool. Returns (result, should_end_call)."""
    # Handoff tools are matched against the agent that *called* them — before
    # any prior state mutation. Soft handoff keeps one WS; chirp applies
    # session.update after this returns.
    from_agent = state["agent"]
    target = handoff_target(bp, from_agent, name)
    if target:
        state["agent"] = target
        return {"success": True, "role": target}, False

    if name == "end_call" or is_session_tool(bp, from_agent, name):
        return {"success": True}, True

    # Industry tools — never invent success if the tool server is down.
    if name in bp["catalog"] and not (
        any(
            t["name"] == name and (t.get("handoff") or t.get("session"))
            for t in bp["agents"][from_agent]["tools"]
        )
    ):
        return await dispatch_industry_tool(name, args), False

    return {"success": False, "error": f"unknown tool {name}"}, False


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
            ok = bool(result.get("success"))
        except Exception as e:  # noqa: BLE001 — dead tool must not kill the call
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
        "event_id": _event_id(),
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result),
        },
    }


def nudge_greeting() -> dict[str, Any]:
    """Bare response.create — greeting text comes from pack instructions only."""
    return {"type": "response.create", "event_id": _event_id()}


async def handle_function_call(
    name: str,
    arguments: str | dict,
    call_id: str,
    bp: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """Run a tool and build the conversation.item.create reply."""
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
    else:
        args = dict(arguments or {})

    result, stop = await run_tool(name, args, bp, state, call_id=call_id)
    return result, stop, function_call_output(call_id, result)


async def configure_session(ws, agent: str, bp: dict[str, Any], *, timeout: float = 60.0) -> dict:
    """Send session.update and wait for session.updated. Returns the event."""
    import asyncio

    await ws.send(json.dumps(session_update_for_agent(bp, agent)))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(raw, bytes):
            continue
        ev = json.loads(raw)
        et = ev.get("type")
        if et == "session.updated":
            return ev
        if et == "error":
            raise RuntimeError(f"session.update failed for {agent}: {ev}")


async def soft_handoff_session(
    ws, target: str, bp: dict[str, Any], *, timeout: float = 30.0
) -> dict:
    """Mid-call soft handoff: swap instructions+tools on the same WS."""
    return await configure_session(ws, target, bp, timeout=timeout)


async def run_session(industry_dir: str | Path, *, model: str = MODEL) -> None:
    """Smoke: open one Omni session, session.update start agent, then close."""
    import asyncio
    import contextlib

    from report import traced_run

    bp = load_blueprint(industry_dir)
    name = Path(industry_path(industry_dir)).name

    async with traced_run(f"mivas-{name}-{model}", model=model):
        async with connect_qwen(model) as omni:
            created = json.loads(await asyncio.wait_for(omni.recv(), timeout=30))
            print(f"start {created.get('type')}", flush=True)
            updated = await configure_session(omni, bp["start"], bp)
            n = len((updated.get("session") or {}).get("tools") or [])
            print(
                f"{bp['start']} {updated.get('type')} tools={n} ok",
                flush=True,
            )
            with contextlib.suppress(Exception):
                await omni.close()


def demo() -> None:
    """Offline blueprint/tool-shape check (no network)."""
    bp = load_blueprint("control-industry")
    start = bp["start"]
    start_tools = advertised_tools("control-industry", start)
    assert tool_names(bp, start) == start_tools
    all_names = {n for a in bp["agents"] for n in tool_names(bp, a)}
    today_line = today_context_line()
    for agent, names in ((a, tool_names(bp, a)) for a in bp["agents"]):
        update = session_update_for_agent(bp, agent)
        session = update["session"]
        pack = bp["agents"][agent]["instructions"]
        assert session["instructions"].startswith(pack.rstrip())
        assert today_line in session["instructions"]
        assert advertised_tool_names(bp, agent) == names
        assert all(t.get("type") == "function" for t in session["tools"])
        assert all(
            isinstance(t.get("function"), dict) and t["function"].get("name")
            for t in session["tools"]
        )
        assert session["turn_detection"]["type"] in {"server_vad", "semantic_vad"}
        assert "# Multi-agent note" not in session["instructions"]
        assert "you MUST call" not in session["instructions"].lower()
        leaked = all_names - set(names)
        for n in leaked:
            assert n not in advertised_tool_names(bp, agent), f"{agent} leaked {n}"
    nxt = extract_appointment_date("next Tuesday afternoon")
    assert nxt and nxt.endswith(f"/{_dt.date.today().year}")
    assert infer_schedule_appointment("Booking confirmed for March 18.") == {
        "date": f"03/18/{_dt.date.today().year}"
    }
    # Soft handoff: after receptionist handoff tool, scheduler session must not
    # advertise receptionist-only tools.
    if len(bp["agents"]) > 1 and all_names - set(start_tools):
        assert set(start_tools) != all_names
    state = {"agent": start}
    target = None
    handoff_name = None
    for name in start_tools:
        cand = handoff_target(bp, start, name)
        if cand:
            target, handoff_name = cand, name
            break
    if target and handoff_name:
        import asyncio

        res, stop = asyncio.run(run_tool(handoff_name, {}, bp, state))
        assert res.get("success") and res.get("role") == target and not stop
        assert state["agent"] == target
        sched_tools = advertised_tool_names(bp, target)
        assert "schedule_appointment" in sched_tools
        assert handoff_name not in sched_tools
        soft = session_update_for_agent(bp, target)
        assert advertised_tool_names(bp, target) == [
            (t.get("function") or {}).get("name") for t in soft["session"]["tools"]
        ]
    print(
        f"qwen self-check ok start={start} tools={start_tools} "
        f"agents={list(bp['agents'])} model={MODEL} ws={ws_url()}"
    )


if __name__ == "__main__":
    demo()
