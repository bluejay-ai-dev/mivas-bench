"""Blueprint → xAI Speech-to-Speech (Grok Voice) session helpers.

Industry tools map onto the industry state API (`TOOL_SERVER_URL`).
Handoff tools (`handoff: true`) switch the active blueprint agent.
Session tools (`session: true`, e.g. end_call) hang up.

Multi-agent is soft: one Grok Realtime WebSocket for the call. Handoff is
`session.update` on that socket (target instructions + tools); history stays.

Industry-agnostic: pack owns prompts/tool policy. No harness greeting strings.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
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
from call_id import call_session, headers as tool_headers, set_call_id  # noqa: E402

# Verbal booking confirm without a function-call event (same failure mode as
# hosted VoiceChat). Recover schedule_appointment for tool-server + OTel.
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

RUNTIME = "voice"
MODEL = os.environ.get("GROK_VOICE_MODEL", "grok-voice-latest")
DEFAULT_WS_URL = "wss://api.x.ai/v1/realtime"
SAMPLE_RATE = 24_000
VOICE = os.environ.get("GROK_VOICE", "eve")
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
        "greeting": str(blueprint.get("greeting") or "").strip(),
        "agents": agents,
        "catalog": catalog,
    }


def api_key() -> str:
    key = (
        os.environ.get("GROK_API_KEY", "").strip()
        or os.environ.get("XAI_API_KEY", "").strip()
    )
    if not key:
        raise SystemExit("need GROK_API_KEY or XAI_API_KEY")
    return key


def ws_url(model: str | None = None) -> str:
    base = os.environ.get("GROK_WS_URL", DEFAULT_WS_URL).rstrip("/")
    return f"{base}?model={model or MODEL}"


def ws_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}"}


def connect_grok(model: str | None = None):
    """Return a websockets connect CM for the xAI Voice Realtime API."""
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
    """xAI custom function tool from an industry catalog entry."""
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    raw.pop("additionalProperties", None)
    props = raw.get("properties")
    properties: dict[str, Any] = dict(props) if isinstance(props, dict) else {}
    params: dict[str, Any] = {"type": "object", "properties": properties}
    if raw.get("required"):
        params["required"] = list(raw["required"])
    return {
        "type": "function",
        "name": spec["name"],
        "description": spec.get("description", spec["name"]),
        "parameters": params,
    }


def today_context_line(today: _dt.date | None = None) -> str:
    """Clock fact for relative dates — Grok Voice has no built-in 'today'."""
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


def session_update_for_agent(
    bp: dict[str, Any], agent: str, *, mid_call: bool = False
) -> dict[str, Any]:
    """Pack instructions + that agent's tools only. No harness prompt stuffing.

    Mid-call updates swap instructions/tools only — voice, VAD, and PCM format
    stay from the first update so the live socket is not reset.
    """
    if agent not in bp["agents"]:
        raise KeyError(f"unknown agent {agent!r}")
    tools = [_tool_decl(bp["catalog"][name]) for name in tool_names(bp, agent)]
    instructions = with_today_context(bp["agents"][agent]["instructions"])
    # The reception prompt assumes the branded greeting was already spoken, so a
    # bare response.create opens with "What can I help you with?" — no brand, no
    # AI disclosure. Only the pack's own greeting string fills that in.
    if not mid_call and bp.get("greeting"):
        instructions += (
            "\n\nThe call just connected and nothing has been said yet. "
            f'Speak first, with exactly: "{bp["greeting"]}"'
        )
    session: dict[str, Any] = {
        "instructions": instructions,
        "tools": tools,
    }
    if not mid_call:
        silence_ms = int(os.environ.get("GROK_VAD_SILENCE_MS", "700"))
        prefix_ms = int(os.environ.get("GROK_VAD_PREFIX_MS", "400"))
        threshold = float(os.environ.get("GROK_VAD_THRESHOLD", "0.7"))
        session.update(
            {
                "voice": VOICE,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": threshold,
                    "silence_duration_ms": silence_ms,
                    "prefix_padding_ms": prefix_ms,
                },
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "transcription": {
                            "model": "grok-transcribe",
                            "language_hint": "en",
                        },
                    },
                    "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
                },
            }
        )
    return {
        "type": "session.update",
        "event_id": _event_id(),
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
    """Run a blueprint tool. Returns (result, should_end_call)."""
    # A session.update swaps the advertised tools but not what is already in
    # Grok's context, so it re-calls the stage it just left (transfer_to_identity
    # while in identity). Executing those hits the tool server off-stage and
    # writes phantom actuals, so refuse anything outside the active stage.
    if name not in tool_names(bp, state["agent"]):
        return {
            "success": False,
            "error": f"{name} is not available to {state['agent']}",
        }, False

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
        # Human-transfer session tools POST (so Bluejay records actual) then
        # hang up — there is no human to join.
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


def handoff_nudge_event() -> dict[str, Any]:
    """Continue on the same socket after session.update — not a fresh greeting."""
    return nudge_greeting()


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


async def configure_session(
    ws, agent: str, bp: dict[str, Any], *, timeout: float = 60.0, mid_call: bool = False
) -> dict:
    """Send session.update and wait for session.updated. Returns the event."""
    import asyncio

    await ws.send(json.dumps(session_update_for_agent(bp, agent, mid_call=mid_call)))
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


async def run_session(industry_dir: str | Path, *, model: str = MODEL) -> None:
    """Smoke: one socket, session.update each agent on it, then close."""
    import asyncio
    import contextlib

    from report import traced_run

    bp = load_blueprint(industry_dir)
    name = Path(industry_path(industry_dir)).name

    async with traced_run(f"mivas-{name}-{model}", model=model):
        async with connect_grok(model) as grok:
            created = json.loads(await asyncio.wait_for(grok.recv(), timeout=30))
            print(f"session {created.get('type')}", flush=True)
            for i, agent in enumerate(bp["agents"]):
                updated = await configure_session(grok, agent, bp, mid_call=i > 0)
                n = len((updated.get("session") or {}).get("tools") or [])
                print(f"{agent} {updated.get('type')} tools={n} ok", flush=True)
            with contextlib.suppress(Exception):
                await grok.close()


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
        assert [t["name"] for t in session["tools"]] == names
        assert all(t.get("type") == "function" for t in session["tools"])
        assert session["turn_detection"]["type"] == "server_vad"
        assert "# Multi-agent note" not in session["instructions"]
        assert "you MUST call" not in session["instructions"].lower()
        leaked = all_names - set(names)
        for n in leaked:
            # other agents' tools must not be advertised on this session
            assert n not in [t["name"] for t in session["tools"]], f"{agent} leaked {n}"
    # Relative-date recovery uses the real clock, not model priors (was Mar 2025).
    nxt = extract_appointment_date("next Tuesday afternoon")
    assert nxt and nxt.endswith(f"/{_dt.date.today().year}")
    assert infer_schedule_appointment("Booking confirmed for March 18.") == {
        "date": f"03/18/{_dt.date.today().year}"
    }
    assert handoff_nudge_event()["type"] == "response.create"
    mid = session_update_for_agent(bp, start, mid_call=True)["session"]
    assert "turn_detection" not in mid and "voice" not in mid
    assert "instructions" in mid and "tools" in mid
    # greeting only on the opening session.update, never on a handoff
    hc = load_blueprint("healthcare")
    assert hc["greeting"], "healthcare pack lost its greeting string"
    open_i = session_update_for_agent(hc, hc["start"])["session"]["instructions"]
    mid_i = session_update_for_agent(hc, hc["start"], mid_call=True)["session"][
        "instructions"
    ]
    assert hc["greeting"] in open_i and hc["greeting"] not in mid_i
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
        # re-calling the tool it just left is off-stage now → refused, no re-handoff
        if handoff_name not in tool_names(bp, target):
            again, stop2 = asyncio.run(run_tool(handoff_name, {}, bp, state))
            assert not again.get("success") and not stop2, again
            assert state["agent"] == target
    print(
        f"grok self-check ok start={start} tools={start_tools} "
        f"agents={list(bp['agents'])} model={MODEL} ws={ws_url()}"
    )


if __name__ == "__main__":
    demo()
