"""Nemotron VoiceChat (full-duplex S2S) — OpenAI Realtime-compatible WebSocket client.

Wire protocol: https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/
voicechat_realtime_instructions/api-reference.md

Multi-agent is hard (Pipecat S2S switcher style): one VoiceChat session per
blueprint agent, each with that agent's pack instructions + tools only. The
CHIRP bridge keeps all sockets open and rewires audio to the active agent on
handoff. Idle sessions get no input audio.

Industry-agnostic: pack owns prompts/tool policy. Hosted NVCF accepts session.tools
but does not apply the NIM jinja that injects NVIDIA's <AVAILABLE_TOOLS>/<TOOLCALL>
protocol, so session_update_for_agent appends that trained wire format from the
same catalog decls. Speak-first is env-configurable (`VOICECHAT_SPEAKS_FIRST`).
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

RUNTIME = "nemotron-voicechat"
MODEL = "nvidia/nemotron-voicechat"
# Hosted NVCF Realtime (ai-nemotron-voicechat). Override VOICECHAT_WS_URL for a
# local NIM (ws://127.0.0.1:9000/v1/realtime) or other remote.
DEFAULT_WS_URL = "wss://grpc.nvcf.nvidia.com/v1/realtime"
DEFAULT_FUNCTION_ID = "42c86b5f-545a-4b2f-a83b-90fd71da9912"
SAMPLE_RATE = 24_000  # wire format both ways; server resamples to 16k / 22.05k


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


def _tool_decl(spec: dict, *, handoff: bool = False) -> dict[str, Any]:
    """Build a VoiceChat tool object from the industry catalog entry."""
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
    elif handoff:
        # Hosted NVCF rarely emits FC events; catch spoken transfer / gatekeeping lines.
        out["ack_messages"] = [
            "One moment.",
            "let me transfer",
            "transfer you",
            "connect you",
            "check our availability",
            "speak with our scheduler",
            "get the scheduler",
            "hand you over",
            # Model often books itself instead of handing off — still rewire.
            "go ahead and book",
            "book that time",
            "appointment is set",
        ]
        out["handoff"] = True
    elif spec.get("name") == "schedule_appointment":
        # Hosted NVCF often speaks these instead of emitting function-call events.
        out["ack_messages"] = [
            "appointment is scheduled",
            "appointment is set",
            "I've scheduled",
            "booking confirmed",
            "scheduled your",
            "book that time",
            "go ahead and book",
            "you're all set",
        ]
    return out


def _is_handoff_tool(bp: dict[str, Any], agent: str, name: str) -> bool:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == name and t.get("handoff"):
            return True
    return False


# Official Nemotron VoiceChat / Nemotron Nano v2 function-calling protocol.
# Local NIM jinja appends this when session.tools is set; hosted NVCF does not.
_FC_PROTOCOL = (
    "\n\nCall a tool ONLY when the user's request matches one of the tools listed "
    "in <AVAILABLE_TOOLS> below. For every other request, do not call any tool - "
    "just answer from your knowledge. Never invent or call a tool name that is not "
    "literally in <AVAILABLE_TOOLS>.\n"
    "If a tool has required parameters, emit the <TOOLCALL> with those parameters "
    "BEFORE you speak any confirmation to the caller. Do not claim a booking or "
    "handoff succeeded unless you have emitted the matching <TOOLCALL>.\n"
    "<AVAILABLE_TOOLS>{tools}</AVAILABLE_TOOLS>\n\n"
    "If you decide to call any tool(s), use the following format:\n"
    '<TOOLCALL>[{{"name": "tool_name1", "arguments": {{"param": "value"}}}}]</TOOLCALL>\n\n'
    "The user will execute tool-calls and return responses from tool(s) in this format:\n"
    "<TOOL_RESPONSE>[{{tool_response1}}]</TOOL_RESPONSE>\n"
)
_TOOLCALL_RE = re.compile(r"<TOOLCALL>(.*?)</TOOLCALL>", re.DOTALL | re.IGNORECASE)
_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
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
_DATE_MONTH_RE = re.compile(
    r"\b("
    + "|".join(_MONTHS)
    + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_DAY_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "twenty first": 21,
    "twenty-first": 21,
    "twenty second": 22,
    "twenty-second": 22,
    "twenty third": 23,
    "twenty-third": 23,
    "twenty fourth": 24,
    "twenty-fourth": 24,
    "twenty fifth": 25,
    "twenty-fifth": 25,
    "twenty sixth": 26,
    "twenty-sixth": 26,
    "twenty seventh": 27,
    "twenty-seventh": 27,
    "twenty eighth": 28,
    "twenty-eighth": 28,
    "twenty ninth": 29,
    "twenty-ninth": 29,
    "thirtieth": 30,
    "thirty first": 31,
    "thirty-first": 31,
}
_DATE_MONTH_WORD_RE = re.compile(
    r"\b("
    + "|".join(_MONTHS)
    + r")\s+("
    + "|".join(sorted((_DAY_WORDS.keys()), key=len, reverse=True))
    + r")(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_BOOKING_CONFIRM_RE = re.compile(
    r"(appointment\s+is\s+scheduled|appointment\s+is\s+set|i(?:'| ha)?ve\s+scheduled|"
    r"booking\s+confirmed|scheduled\s+(?:your|the)\s+(?:repair\s+)?appointment|"
    r"booked\s+(?:your|the)|book\s+that\s+time|go\s+ahead\s+and\s+book|"
    r"you(?:'| a)re\s+(?:all\s+)?set(?:\s+for)?|confirmed\s+for|"
    r"appointment\s+has\s+been\s+confirmed)",
    re.IGNORECASE,
)
# Model often skips the confirm line and jumps to wrap-up after the caller said
# "please book" — still recover schedule_appointment from the offered date.
_BOOKING_WRAP_RE = re.compile(
    r"(you(?:'| a)re\s+welcome|anything\s+else|have\s+a\s+(?:great|good)\s+day|"
    r"see\s+you\s+on)",
    re.IGNORECASE,
)
_SLOT_OFFER_RE = re.compile(
    r"(opening|slot|does\s+that\s+work|two\s+thirty|three\s+thirty|\d{1,2}:\d{2}\s*pm)",
    re.IGNORECASE,
)
_GREET_RE = re.compile(
    r"(hello|hi|hey)([!.?]|\b).{0,60}(how can i help|what can i (do|help))",
    re.IGNORECASE | re.DOTALL,
)


def looks_like_open_greeting(text: str) -> bool:
    """True for cold openers we must not play mid-call after handoff."""
    t = " ".join((text or "").split())
    if not t:
        return False
    if _GREET_RE.search(t):
        return True
    low = t.lower().strip()
    if low in {"hello", "hello.", "hello?", "hi", "hi.", "hey"}:
        return True
    # Variants like "Yes, I'm here. How can I help you today?"
    if re.search(r"how can i help( you)?( today)?\??$", low):
        return len(t) < 80
    return False
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
_BARE_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE
)
_TODAY_RE = re.compile(r"\btoday\b", re.IGNORECASE)
_TOMORROW_RE = re.compile(r"\btomorrow\b", re.IGNORECASE)


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


def match_ack_tool(tools: list[dict[str, Any]], text: str) -> str | None:
    """If transcript contains exactly one tool's catalog ack_messages, return that name.

    Hosted NVCF speaks ack_messages when the model triggers a tool, but often never
    emits response.function_call_arguments.done / <TOOLCALL>.
    """
    if not text or not tools:
        return None
    blob = text.lower()
    hits: list[str] = []
    for t in tools:
        name = t.get("name")
        if not isinstance(name, str) or not name:
            continue
        for ack in t.get("ack_messages") or []:
            needle = str(ack).strip().lower().rstrip(".!")
            if needle and needle in blob:
                if name not in hits:
                    hits.append(name)
                break
    return hits[0] if len(hits) == 1 else None


def extract_appointment_date(text: str, *, default_year: int | None = None) -> str | None:
    """Best-effort MM/DD/YYYY; prefers the last concrete date mentioned in `text`."""
    import datetime as _dt

    if not text:
        return None
    year_default = int(default_year or _dt.date.today().year)
    # (score, end_index, mm/dd/yyyy) — higher score wins; last among ties.
    # Prevents goodbye "…help with today?" from overriding "next Tuesday".
    hits: list[tuple[int, int, str]] = []

    for m in _DATE_NUMERIC_RE.finditer(text):
        mm, dd, yyyy = m.group(1).split("/")
        hits.append((50, m.end(), f"{int(mm):02d}/{int(dd):02d}/{int(yyyy)}"))
    for m in _DATE_MONTH_RE.finditer(text):
        month = _MONTHS.index(m.group(1).lower()) + 1
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else year_default
        hits.append((50, m.end(), f"{month:02d}/{day:02d}/{year}"))
    for m in _DATE_MONTH_WORD_RE.finditer(text):
        month = _MONTHS.index(m.group(1).lower()) + 1
        day = _DAY_WORDS[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else year_default
        hits.append((50, m.end(), f"{month:02d}/{day:02d}/{year}"))
    for m in _NEXT_WEEKDAY_RE.finditer(text):
        target = _WEEKDAYS.index(m.group(1).lower())
        today = _dt.date.today()
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        day = today + _dt.timedelta(days=delta)
        hits.append((55, m.end(), day.strftime("%m/%d/%Y")))
    # Bare weekday → upcoming occurrence, never "today" (on Tuesday, "Tuesday"
    # in a booking line means next Tuesday — "today" is spoken as today).
    for m in _BARE_WEEKDAY_RE.finditer(text):
        start = m.start()
        if start >= 5 and text[start - 5 : start].lower() == "next ":
            continue
        target = _WEEKDAYS.index(m.group(1).lower())
        today = _dt.date.today()
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        day = today + _dt.timedelta(days=delta)
        hits.append((30, m.end(), day.strftime("%m/%d/%Y")))
    for m in _TOMORROW_RE.finditer(text):
        day = _dt.date.today() + _dt.timedelta(days=1)
        hits.append((20, m.end(), day.strftime("%m/%d/%Y")))
    for m in _TODAY_RE.finditer(text):
        hits.append((10, m.end(), _dt.date.today().strftime("%m/%d/%Y")))

    if not hits:
        return None
    best = max(h[0] for h in hits)
    cands = [h for h in hits if h[0] == best]
    cands.sort(key=lambda x: x[1])
    return cands[-1][2]


def time_year() -> int:
    import datetime as _dt

    return _dt.date.today().year


def _tool_required(tool: dict[str, Any]) -> list[str]:
    params = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {}
    req = params.get("required") if isinstance(params, dict) else None
    return [str(x) for x in req] if isinstance(req, list) else []


def _tool_has_date_param(tool: dict[str, Any]) -> bool:
    params = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {}
    props = params.get("properties") if isinstance(params, dict) else None
    return isinstance(props, dict) and "date" in props


def parse_toolcalls(text: str) -> list[dict[str, Any]]:
    """Parse native <TOOLCALL>[...]</TOOLCALL> blocks from VoiceChat text."""
    out: list[dict[str, Any]] = []
    if not text:
        return out
    for match in _TOOLCALL_RE.finditer(text):
        raw = (match.group(1) or "").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            args = item.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"raw": args}
            out.append({"name": str(item["name"]), "arguments": args or {}})
    return out


def infer_tool_calls(tools: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """TOOLCALL blocks, then ack_messages, then booking-confirm+date.

    Hosted NVCF often speaks a confirmation / on-hold line without emitting a
    function-call event; this recovers the tool for Bluejay execute_tool spans.
    Ack/handoff is preferred over schedule inference so the receptionist rewires
    before a verbal booking is treated as schedule_appointment.
    """
    found = parse_toolcalls(text)
    if found:
        return found
    if not text or not tools:
        return []

    ack = match_ack_tool(tools, text)
    if ack:
        tool = next((t for t in tools if t.get("name") == ack), None)
        if tool is not None:
            required = _tool_required(tool)
            if not required:
                return [{"name": ack, "arguments": {}}]
            if "date" in required:
                date = extract_appointment_date(text)
                if date:
                    return [{"name": ack, "arguments": {"date": date}}]

    if _BOOKING_CONFIRM_RE.search(text):
        # Only look near the confirm phrase so trailing "…help today?" cannot win.
        m = _BOOKING_CONFIRM_RE.search(text)
        assert m is not None
        window = text[max(0, m.start() - 40) : min(len(text), m.end() + 100)]
        date = extract_appointment_date(window) or extract_appointment_date(text)
        if date:
            for t in tools:
                if t.get("name") == "schedule_appointment" or _tool_has_date_param(t):
                    return [{"name": str(t["name"]), "arguments": {"date": date}}]

    # Wrap-up without an explicit "I've scheduled" — still book if a slot was offered.
    if _BOOKING_WRAP_RE.search(text) and _SLOT_OFFER_RE.search(text):
        date = extract_appointment_date(text)
        if date:
            for t in tools:
                if t.get("name") == "schedule_appointment" or _tool_has_date_param(t):
                    return [{"name": str(t["name"]), "arguments": {"date": date}}]

    return []


def session_update_for_agent(bp: dict[str, Any], agent: str) -> dict[str, Any]:
    """Pack instructions + that agent's tools only + NVIDIA FC wire format."""
    if agent not in bp["agents"]:
        raise KeyError(f"unknown agent {agent!r}")
    tools: list[dict[str, Any]] = []
    for name in tool_names(bp, agent):
        tools.append(
            _tool_decl(bp["catalog"][name], handoff=_is_handoff_tool(bp, agent, name))
        )
    pack = _ascii(bp["agents"][agent]["instructions"])
    instructions = pack
    if tools:
        instructions = pack + _FC_PROTOCOL.format(tools=_available_tools_json(tools))
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


def handoff_continue_events(*, prior_agent_said: str = "") -> list[dict[str, Any]]:
    """Seed mid-call history for a cold dual-session target (no response.create).

    `response.create` on a fresh VoiceChat session open-greets ("Hello, how can I
    help?") even with transfer instructions. Chirp waits for the DH to go quiet,
    then optionally nudges — history alone is usually enough once user audio flows.
    """
    prior = " ".join((prior_agent_said or "").split()).strip()
    prior_line = prior[:280] if prior else (
        "One moment — I'm transferring you to our scheduler now."
    )
    notice = (
        "SYSTEM: Live mid-call transfer. The caller already asked to schedule a "
        "repair appointment. You are the scheduler taking over an active call. "
        "FORBIDDEN: hello, hi, welcome, 'how can I help', or any open greeting. "
        "REQUIRED: continue booking immediately (ask for or confirm a concrete date)."
    )
    return [
        {
            "type": "conversation.item.create",
            "event_id": _event_id(),
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": notice}],
            },
        },
        {
            "type": "conversation.item.create",
            "event_id": _event_id(),
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": prior_line}],
            },
        },
        {
            "type": "conversation.item.create",
            "event_id": _event_id(),
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Yes — I want to schedule a repair appointment. "
                            "Please continue; do not greet me."
                        ),
                    }
                ],
            },
        },
    ]


def handoff_nudge_event() -> dict[str, Any]:
    """Bare response.create — only after the DH is quiet post-transfer."""
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
    else:
        args = dict(arguments or {})

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
    """Offline blueprint/tool-shape check (no network)."""
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
    assert parse_toolcalls(
        '<TOOLCALL>[{"name": "handoff_to_scheduler", "arguments": {}}]</TOOLCALL>'
    ) == [{"name": "handoff_to_scheduler", "arguments": {}}]
    recv_tools = session_update_for_agent(bp, start)["session"]["tools"]
    assert match_ack_tool(recv_tools, "One moment please") == "handoff_to_scheduler"
    assert match_ack_tool(recv_tools, "Hello welcome") is None
    assert extract_appointment_date("Confirmed for 03/18/2026 at 1pm") == "03/18/2026"
    assert extract_appointment_date("March 18th, 2026 works") == "03/18/2026"
    # "next <weekday>" outranks a later month phrase (model often invents months).
    assert extract_appointment_date(
        "next Tuesday then March tenth at two thirty"
    ) == extract_appointment_date("next Tuesday")
    assert extract_appointment_date("March tenth at two thirty").startswith("03/")
    sched_tools = session_update_for_agent(bp, "scheduler")["session"]["tools"]
    assert infer_tool_calls(
        sched_tools, "Your appointment is scheduled for March 18, 2026."
    ) == [{"name": "schedule_appointment", "arguments": {"date": "03/18/2026"}}]
    tue = extract_appointment_date(
        "I've scheduled your appointment for Tuesday at four PM."
    )
    assert tue and len(tue) == 10
    assert infer_tool_calls(
        sched_tools,
        "You're welcome! I've scheduled your appointment for Tuesday at four PM.",
    ) == [{"name": "schedule_appointment", "arguments": {"date": tue}}]
    nxt = extract_appointment_date(
        "Alright, I've scheduled your appointment for next Tuesday at three thirty PM. "
        "Is there anything else I can help with today?"
    )
    assert nxt == extract_appointment_date("next Tuesday")
    assert nxt != extract_appointment_date("today")
    assert infer_tool_calls(recv_tools, "One moment.") == [
        {"name": "handoff_to_scheduler", "arguments": {}}
    ]
    assert infer_tool_calls(
        recv_tools, "Let me check our availability real quick"
    ) == [{"name": "handoff_to_scheduler", "arguments": {}}]
    nxt = extract_appointment_date("next Tuesday at two thirty")
    assert nxt and len(nxt) == 10
    cont = handoff_continue_events(prior_agent_said="One moment while I transfer you.")
    assert all(c["type"] == "conversation.item.create" for c in cont)
    roles = [c["item"]["role"] for c in cont]
    assert roles == ["user", "assistant", "user"]
    notice = cont[0]["item"]["content"][0]["text"]
    assert "FORBIDDEN" in notice and "hello" in notice.lower()
    assert handoff_nudge_event()["type"] == "response.create"
    assert extract_appointment_date("appointment is set for Tuesday") == extract_appointment_date(
        "next Tuesday"
    )
    wrap = infer_tool_calls(
        sched_tools,
        "We have an opening at two thirty PM on Tuesday. Does that work for you? "
        "You're welcome. Is there anything else I can assist you with today?",
    )
    assert wrap and wrap[0]["name"] == "schedule_appointment"
    if len(bp["agents"]) > 1 and all_names - set(start_tools):
        assert set(start_tools) != all_names
    assert looks_like_open_greeting("Hello! How can I help you today?")
    assert not looks_like_open_greeting(
        "Sure thing. Let me check our availability for next Tuesday."
    )
    print(
        f"voicechat self-check ok start={start} tools={start_tools} "
        f"agents={list(bp['agents'])} speaks_first={speaks_first()} ws={ws_url()}"
    )


if __name__ == "__main__":
    demo()
