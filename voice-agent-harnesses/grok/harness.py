"""Blueprint → xAI Speech-to-Speech (Grok Voice) session helpers.

Industry tools map onto the industry state API (`TOOL_SERVER_URL`).
Handoff tools (`handoff: true`) switch the active blueprint agent.
Session tools (`session: true`, e.g. end_call) hang up.

Multi-agent is hard isolation: one Grok Realtime WebSocket per blueprint agent,
each with that agent's pack instructions + tools only. The CHIRP bridge keeps
all sockets open and rewires audio to the active agent on handoff.

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


def session_update_for_agent(bp: dict[str, Any], agent: str) -> dict[str, Any]:
    """Pack instructions + that agent's tools only. No harness prompt stuffing."""
    if agent not in bp["agents"]:
        raise KeyError(f"unknown agent {agent!r}")
    tools = [_tool_decl(bp["catalog"][name]) for name in tool_names(bp, agent)]
    # Telephony-friendly VAD: more prefix padding so the first syllable isn't
    # clipped, longer silence so mid-sentence pauses don't end the turn early.
    silence_ms = int(os.environ.get("GROK_VAD_SILENCE_MS", "700"))
    prefix_ms = int(os.environ.get("GROK_VAD_PREFIX_MS", "400"))
    threshold = float(os.environ.get("GROK_VAD_THRESHOLD", "0.7"))
    return {
        "type": "session.update",
        "event_id": _event_id(),
        "session": {
            "voice": VOICE,
            "instructions": with_today_context(bp["agents"][agent]["instructions"]),
            "turn_detection": {
                "type": "server_vad",
                "threshold": threshold,
                "silence_duration_ms": silence_ms,
                "prefix_padding_ms": prefix_ms,
            },
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    # Emit conversation.item.input_audio_transcription.* so the
                    # bridge can log what Grok actually heard from the DH.
                    "transcription": {"model": "grok-transcribe", "language_hint": "en"},
                },
                "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
            },
            "tools": tools,
        },
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


def _text_item(role: str, text: str) -> dict[str, Any]:
    """conversation.item.create for a text turn (OpenAI/xAI Realtime shape)."""
    content_type = "input_text" if role == "user" else "text"
    return {
        "type": "conversation.item.create",
        "event_id": _event_id(),
        "item": {
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": text}],
        },
    }


def handoff_seed_events(
    *,
    user_said: str = "",
    prior_agent_said: str = "",
) -> list[dict[str, Any]]:
    """Seed a cold dual-session target with prior call context (then nudge).

    OpenAI soft-handoff keeps one conversation; Grok hard-isolation opens a
    blank WS. Replaying the last user/assistant turns into the target is the
    closest equivalent — industry-agnostic (no pack strings).
    """
    user = " ".join((user_said or "").split()).strip()[:500]
    prior = " ".join((prior_agent_said or "").split()).strip()[:280]
    events: list[dict[str, Any]] = [
        _text_item(
            "user",
            "SYSTEM: Mid-call handoff. You are taking over an active call. "
            "Continue from the conversation below — do not greet, welcome, "
            "or ask how you can help; pick up where it left off.",
        )
    ]
    if prior:
        events.append(_text_item("assistant", prior))
    if user:
        events.append(_text_item("user", user))
    else:
        events.append(
            _text_item(
                "user",
                "Please continue helping me with what I just asked. Do not greet me.",
            )
        )
    return events


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


async def run_session(industry_dir: str | Path, *, model: str = MODEL) -> None:
    """Smoke: open one session per agent, session.update each, then close."""
    import asyncio
    import contextlib

    from report import traced_run

    bp = load_blueprint(industry_dir)
    name = Path(industry_path(industry_dir)).name

    async with traced_run(f"mivas-{name}-{model}", model=model):
        for agent in bp["agents"]:
            async with connect_grok(model) as grok:
                created = json.loads(await asyncio.wait_for(grok.recv(), timeout=30))
                print(f"{agent} {created.get('type')}", flush=True)
                updated = await configure_session(grok, agent, bp)
                n = len((updated.get("session") or {}).get("tools") or [])
                print(
                    f"{agent} {updated.get('type')} tools={n} ok",
                    flush=True,
                )
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
    seed = handoff_seed_events(
        user_said="I'd like next Tuesday afternoon.",
        prior_agent_said="One moment while I transfer you.",
    )
    assert [e["item"]["role"] for e in seed] == ["user", "assistant", "user"]
    assert "next Tuesday" in seed[-1]["item"]["content"][0]["text"]
    assert "Mid-call handoff" in seed[0]["item"]["content"][0]["text"]
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
    print(
        f"grok self-check ok start={start} tools={start_tools} "
        f"agents={list(bp['agents'])} model={MODEL} ws={ws_url()}"
    )


if __name__ == "__main__":
    demo()
