"""Blueprint → Twilio ConversationRelay + OpenAI GPT-4.1 helpers.

ConversationRelay owns STT/TTS over a Twilio phone call. This harness owns:
  - industry blueprint (per-agent tool archive)
  - GPT-4.1 chat.completions with tools
  - soft handoff (one conversation; swap system + tools)
  - today clock injection
  - booking-confirm inference when the model verbally books without a FC

Industry tools → POST {TOOL_SERVER_URL}/tools/{name}.
Session tools (end_call) hang up via ConversationRelay `{"type":"end"}`.
Handoff tools never hit the tool server.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import headers as tool_headers, set_call_id  # noqa: E402

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1] if len(HARNESS_DIR.parents) > 1 else HARNESS_DIR

RUNTIME = "conversationrelay-gpt4.1"
MODEL = os.environ.get("TWILIO_LLM_MODEL", "gpt-4.1")
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "4.0"))
DEFAULT_WELCOME = "Welcome to Bluejay's Repair Services!"

# Voice rendering note for TTS — transport concern, not pack policy.
_VOICE_RENDER_NOTE = (
    "This conversation is spoken aloud over the phone. Spell out numbers "
    "(say twenty not 20). Do not use emojis, bullet points, asterisks, or "
    "markdown. Keep replies concise and conversational."
)

_CLOCK_RULE = (
    "Relative dates: 'next <weekday>' means the next occurrence of that weekday "
    "STRICTLY AFTER today. If today is that weekday, jump +7 days. Never treat "
    "today as 'next <weekday>'."
)

_MID_CALL_SCHEDULER = (
    "SYSTEM: Mid-call handoff. You are taking over an active call. "
    "Continue from the prior turns — do not greet, welcome, or ask how you can "
    "help. If the caller already said when they want an appointment (including "
    "'next Tuesday afternoon' or similar), resolve it to a concrete MM/DD/YYYY "
    "using today's date and the relative-date rule, briefly confirm that date, "
    "call schedule_appointment, speak that the appointment is scheduled, then "
    "end_call. Do NOT ask 'when do you want to schedule' if a preference was "
    "already given."
)

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


def tool_server_url() -> str:
    return os.environ.get("TOOL_SERVER_URL", TOOL_SERVER_URL).rstrip("/")


def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise SystemExit("need OPENAI_API_KEY")
    return key


_PACK_WELCOME = {
    "control-industry": DEFAULT_WELCOME,
    "healthcare": "Thank you for calling Straus Dermatology.",
    "finance": "Thank you for calling Copperline Credit Union.",
    "legal": "Thank you for calling Halverson and Reed.",
    "travel": "Thank you for calling Kestrel Air.",
}


def _industry_name() -> str:
    named = os.environ.get("INDUSTRY", "").strip()
    if named:
        return named
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir:
        return Path(env_dir).name
    return "control-industry"


def welcome_greeting() -> str:
    raw = os.environ.get("TWILIO_WELCOME_GREETING", "").strip()
    pack = _PACK_WELCOME.get(_industry_name(), "Hello.")
    # k8s used to stamp the control-industry greeting on every industry.
    if not raw or (raw == DEFAULT_WELCOME and _industry_name() != "control-industry"):
        return pack
    return raw


TWILIO_SIP_HOST_SUFFIX = "sip.twilio.com"


def twilio_sip_domain(industry: str | None = None) -> str:
    """Twilio SIP Domain host for a pack: mivas-twilio-<industry>.sip.twilio.com."""
    name = (industry or _industry_name()).strip() or "control-industry"
    return f"mivas-twilio-{name}.{TWILIO_SIP_HOST_SUFFIX}"


def twilio_sip_uri(industry: str | None = None, *, user: str = "mivas") -> str:
    """Bluejay agent sip_uri: sip:mivas@mivas-twilio-<industry>.sip.twilio.com."""
    return f"sip:{user}@{twilio_sip_domain(industry)}"


def sim_id_from_mapping(mapping: dict[str, Any] | None) -> str:
    """Bluejay X-Simulation-Result-Id from a Twilio webhook form or CR customParameters.

    LiveKit puts the id on the SIP INVITE as X-Simulation-Result-Id. Twilio Programmable
    Voice forwards X-* INVITE headers as SipHeader_<Name> on the VoiceUrl POST.
    Nested JSON (SipHeader_X-Custom-Headers) is scanned too.
    """
    if not mapping:
        return ""
    exact = (
        "SipHeader_X-Simulation-Result-Id",
        "SipHeader_X-Simulation-Result-ID",
        "SipHeader_X-Simulation-Result-id",
        "X-Simulation-Result-Id",
        "x-simulation-result-id",
        "simulation_result_id",
        "Simulation-Result-Id",
    )
    for key in exact:
        val = mapping.get(key)
        if val not in (None, "") and not isinstance(val, (dict, list)):
            return str(val).strip()
    nested: list[dict[str, Any]] = []
    for key, val in mapping.items():
        if isinstance(val, dict):
            nested.append(val)
            continue
        if val in (None, "") or isinstance(val, list):
            continue
        norm = str(key).lower().replace("_", "-")
        if "simulation-result-id" in norm:
            return str(val).strip()
        text = str(val).strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                blob = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(blob, dict):
                nested.append(blob)
    for blob in nested:
        found = sim_id_from_mapping(blob)
        if found:
            return found
    return ""


def sip_header_keys(mapping: dict[str, Any] | None) -> list[str]:
    """Form/header names Twilio stamped from the SIP INVITE (for logs)."""
    if not mapping:
        return []
    return sorted(
        str(k)
        for k in mapping
        if str(k).lower().startswith("sipheader") or str(k).lower().startswith("x-")
    )


def public_base_url() -> str:
    """HTTPS public host for TwiML ConversationRelay url= (no trailing slash)."""
    for key in ("PUBLIC_URL", "HOST", "TWILIO_PUBLIC_URL"):
        raw = os.environ.get(key, "").strip().rstrip("/")
        if raw:
            return raw
    return ""


def ws_public_url(path: str = "/ws") -> str:
    base = public_base_url()
    if not base:
        raise SystemExit("need PUBLIC_URL or HOST (https://… tunnel) for ConversationRelay")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + path
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + path
    if base.startswith("wss://") or base.startswith("ws://"):
        return base.rstrip("/") + path
    return f"wss://{base}{path}"


def today_context_line(today: _dt.date | None = None) -> str:
    d = today or _dt.date.today()
    return f"Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}."


def with_today_context(instructions: str, today: _dt.date | None = None) -> str:
    line = today_context_line(today)
    text = (instructions or "").rstrip()
    if line in text:
        return text
    return f"{text}\n\n{line}"


def system_prompt_for_agent(
    bp: dict[str, Any],
    agent: str,
    *,
    mid_call: bool = False,
    today: _dt.date | None = None,
) -> str:
    if agent not in bp["agents"]:
        raise KeyError(f"unknown agent {agent!r}")
    parts = [
        with_today_context(bp["agents"][agent]["instructions"], today),
        _VOICE_RENDER_NOTE,
        _CLOCK_RULE,
    ]
    if agent == "receptionist":
        parts.append(
            "Never invent or confirm a calendar date yourself. As soon as the "
            "caller wants to schedule, call handoff_to_scheduler immediately."
        )
    if mid_call:
        parts.append(_MID_CALL_SCHEDULER)
    return "\n\n".join(parts)


def transcript_blob(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
    return "\n".join(parts)


def spoken_date_for_tts(date_mmddyyyy: str) -> str:
    """08/18/2026 → 'August eighteenth, twenty twenty-six' (approx, readable)."""
    try:
        mm, dd, yyyy = [int(x) for x in date_mmddyyyy.split("/")]
        d = _dt.date(yyyy, mm, dd)
    except (ValueError, TypeError):
        return date_mmddyyyy
    day_words = {
        1: "first",
        2: "second",
        3: "third",
        4: "fourth",
        5: "fifth",
        6: "sixth",
        7: "seventh",
        8: "eighth",
        9: "ninth",
        10: "tenth",
        11: "eleventh",
        12: "twelfth",
        13: "thirteenth",
        14: "fourteenth",
        15: "fifteenth",
        16: "sixteenth",
        17: "seventeenth",
        18: "eighteenth",
        19: "nineteenth",
        20: "twentieth",
        21: "twenty first",
        22: "twenty second",
        23: "twenty third",
        24: "twenty fourth",
        25: "twenty fifth",
        26: "twenty sixth",
        27: "twenty seventh",
        28: "twenty eighth",
        29: "twenty ninth",
        30: "thirtieth",
        31: "thirty first",
    }
    year = d.year
    if 2000 <= year < 2100:
        rest = year % 100
        tens, ones = divmod(rest, 10)
        ones_w = {
            0: "",
            1: "one",
            2: "two",
            3: "three",
            4: "four",
            5: "five",
            6: "six",
            7: "seven",
            8: "eight",
            9: "nine",
        }
        teens = {
            10: "ten",
            11: "eleven",
            12: "twelve",
            13: "thirteen",
            14: "fourteen",
            15: "fifteen",
            16: "sixteen",
            17: "seventeen",
            18: "eighteen",
            19: "nineteen",
        }
        tens_w = {
            2: "twenty",
            3: "thirty",
            4: "forty",
            5: "fifty",
            6: "sixty",
            7: "seventy",
            8: "eighty",
            9: "ninety",
        }
        if rest < 10:
            rest_spoken = ones_w[rest] or "oh oh"
        elif rest < 20:
            rest_spoken = teens[rest]
        else:
            rest_spoken = (
                f"{tens_w[tens]}-{ones_w[ones]}" if ones else tens_w[tens]
            )
        year_spoken = f"twenty {rest_spoken}"
    else:
        year_spoken = str(year)
    return f"{d.strftime('%B')} {day_words.get(d.day, str(d.day))}, {year_spoken}"


def booking_confirm_line(date_mmddyyyy: str) -> str:
    return (
        f"Your repair appointment is scheduled for "
        f"{spoken_date_for_tts(date_mmddyyyy)}. "
        f"Thank you for booking with us. Goodbye!"
    )


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


def _tool_decl(spec: dict) -> dict[str, Any]:
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    raw.pop("additionalProperties", None)
    props = raw.get("properties")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": dict(props) if isinstance(props, dict) else {},
    }
    if raw.get("required"):
        parameters["required"] = list(raw["required"])
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec.get("description", spec["name"]),
            "parameters": parameters,
        },
    }


def openai_tools_for_agent(bp: dict[str, Any], agent: str) -> list[dict[str, Any]]:
    return [_tool_decl(bp["catalog"][name]) for name in tool_names(bp, agent)]


def advertised_tools(industry_dir: str | Path, agent: str | None = None) -> list[str]:
    bp = load_blueprint(industry_dir)
    name = agent or bp["start"]
    return tool_names(bp, name)


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


def extract_appointment_date(text: str, *, default_year: int | None = None) -> str | None:
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
    if not text or not _BOOKING_CONFIRM_RE.search(text):
        return None
    m = _BOOKING_CONFIRM_RE.search(text)
    assert m is not None
    window = text[max(0, m.start() - 40) : min(len(text), m.end() + 100)]
    date = extract_appointment_date(window) or extract_appointment_date(text)
    if not date:
        return None
    return {"date": date}


async def dispatch_industry_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{tool_server_url()}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        return resp.json()


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Run a blueprint tool. Returns (result, should_end_call)."""
    target = handoff_target(bp, state["agent"], name)
    if target:
        state["agent"] = target
        state["mid_call"] = True
        return {"success": True, "role": target}, False

    if is_session_tool(bp, state["agent"], name) or name == "end_call":
        # Scheduler must book before hanging up when a date was discussed.
        if state.get("agent") == "scheduler" and not state.get("scheduled"):
            return {
                "success": False,
                "error": "Call schedule_appointment with a concrete MM/DD/YYYY before end_call.",
            }, False
        return {"success": True}, True

    if name in bp["catalog"]:
        result = await dispatch_industry_tool(name, args)
        if name == "schedule_appointment" and isinstance(result, dict) and result.get("success"):
            state["scheduled"] = True
            if isinstance(args, dict) and args.get("date"):
                state["scheduled_date"] = str(args["date"])
            elif isinstance(result, dict) and result.get("date"):
                state["scheduled_date"] = str(result["date"])
        return result, False

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
            ok = bool(result.get("success", True)) if isinstance(result, dict) else True
        except Exception as e:  # noqa: BLE001
            result, stop, ok = (
                {"success": False, "error": f"{type(e).__name__}: {e}"},
                False,
                False,
            )
        finish_tool_span(span, result, ok=ok)
        return result, stop


def apply_system_for_active_agent(messages: list[dict[str, Any]], bp: dict[str, Any], state: dict[str, Any]) -> None:
    """Ensure messages[0] is the active agent's system prompt (soft handoff)."""
    content = system_prompt_for_agent(
        bp, state["agent"], mid_call=bool(state.get("mid_call"))
    )
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = content
    else:
        messages.insert(0, {"role": "system", "content": content})


def truncate_assistant_on_interrupt(
    messages: list[dict[str, Any]], utterance_until_interrupt: str
) -> None:
    """Trim the last assistant text to what was spoken before barge-in."""
    spoken = (utterance_until_interrupt or "").strip()
    if not spoken:
        return
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant" and isinstance(messages[i].get("content"), str):
            messages[i]["content"] = spoken
            return


async def maybe_infer_booking(
    text: str, bp: dict[str, Any], state: dict[str, Any]
) -> None:
    """If the model verbally confirmed a booking without FC, book + OTel once."""
    if state.get("scheduled") or state.get("agent") != "scheduler":
        return
    args = infer_schedule_appointment(text)
    if not args:
        return
    await run_tool("schedule_appointment", args, bp, state, call_id="inferred")


def twiml_connect(
    ws_url: str | None = None,
    greeting: str | None = None,
    parameters: dict[str, str] | None = None,
) -> str:
    """TwiML that connects the call to ConversationRelay."""
    url = ws_url or ws_public_url("/ws")
    greet = greeting if greeting is not None else welcome_greeting()
    # Escape XML attribute values lightly.
    url_esc = (
        url.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
    )
    greet_esc = (
        greet.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
    )
    voice = os.environ.get("TWILIO_TTS_VOICE", "en-US-Journey-O").strip()
    language = os.environ.get("TWILIO_LANGUAGE", "en-US").strip()
    tts_provider = os.environ.get("TWILIO_TTS_PROVIDER", "google").strip()
    transcription = os.environ.get("TWILIO_TRANSCRIPTION_PROVIDER", "deepgram").strip()
    attrs = [
        f'url="{url_esc}"',
        f'welcomeGreeting="{greet_esc}"',
        f'language="{language}"',
        f'ttsProvider="{tts_provider}"',
        f'voice="{voice}"',
        f'transcriptionProvider="{transcription}"',
    ]
    params_xml = ""
    for name, value in (parameters or {}).items():
        if not name or value is None or value == "":
            continue
        n = (
            str(name)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
        )
        v = (
            str(value)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
        )
        params_xml += f'\n      <Parameter name="{n}" value="{v}" />'
    inner = (
        f'    <ConversationRelay {" ".join(attrs)}>{params_xml}\n    </ConversationRelay>\n'
        if params_xml
        else f'    <ConversationRelay {" ".join(attrs)} />\n'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f"{inner}"
        "  </Connect>\n"
        "</Response>\n"
    )


def demo(industry: str | Path | None = None) -> None:
    """Offline smoke used by agent.py --check. Industry-agnostic plus control asserts."""
    bp = load_blueprint(industry or os.environ.get("INDUSTRY") or "control-industry")
    start = bp["start"]
    assert start in bp["agents"], start
    start_tools = tool_names(bp, start)
    assert start_tools, start
    tools = openai_tools_for_agent(bp, start)
    assert [t["function"]["name"] for t in tools] == start_tools
    assert today_context_line()
    inferred = infer_schedule_appointment(
        "Your appointment is scheduled for 08/18/2026."
    )
    assert inferred == {"date": "08/18/2026"}, inferred
    xml = twiml_connect(ws_url="wss://example.trycloudflare.com/ws")
    assert "ConversationRelay" in xml and "wss://example.trycloudflare.com/ws" in xml
    if start == "receptionist" and "scheduler" in bp["agents"]:
        assert start_tools == ["handoff_to_scheduler", "end_call"]
        assert tool_names(bp, "scheduler") == ["schedule_appointment", "end_call"]
        assert handoff_target(bp, "receptionist", "handoff_to_scheduler") == "scheduler"
