"""Blueprint → Qwen-Audio Realtime session helpers.

Qwen-Audio Realtime (DashScope / Model Studio) is a raw WebSocket, not the
OpenAI Agents SDK. Industry tools POST to `{TOOL_SERVER_URL}/tools/{name}`.
Handoff tools (`handoff: true`) soft-switch the active blueprint agent on the
same socket via `session.update`. Session tools (`session: true`) hang up.

Docs: https://help.aliyun.com/en/model-studio/qwen-audio-realtime-user-guides
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
from call_id import headers as tool_headers, log_ws_accept, set_call_id  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "2.5"))

RUNTIME = "audio-realtime"
MODEL = os.environ.get("QWEN_AUDIO_MODEL", "qwen-audio-3.0-realtime-plus")
VOICE = os.environ.get("QWEN_AUDIO_VOICE", "longanqian")
INPUT_RATE = 16_000
OUTPUT_RATE = 24_000

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
        raise SystemExit("need DASHSCOPE_API_KEY or QWEN_API_KEY")
    return key


def ws_url(model: str | None = None) -> str:
    override = os.environ.get("QWEN_WS_URL", "").strip().rstrip("/")
    if override:
        base = override
    else:
        workspace = os.environ.get("QWEN_WORKSPACE_ID", "").strip()
        region = os.environ.get("QWEN_REGION", "us-east-1").strip() or "us-east-1"
        if not workspace:
            raise SystemExit("need QWEN_WS_URL or QWEN_WORKSPACE_ID")
        base = f"wss://{workspace}.{region}.maas.aliyuncs.com/api-ws/v1/realtime"
    return f"{base}?model={model or MODEL}"


def ws_headers() -> dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key()}"}
    workspace = os.environ.get("QWEN_WORKSPACE_ID", "").strip()
    if workspace:
        headers["X-DashScope-WorkSpace"] = workspace
    return headers


def connect_qwen(model: str | None = None):
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
    """Qwen-Audio tools use a nested `function` object (Model Studio docs)."""
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


def today_context_line(today: _dt.date | None = None) -> str:
    d = today or _dt.date.today()
    return f"Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}."


def with_today_context(instructions: str, today: _dt.date | None = None) -> str:
    line = today_context_line(today)
    text = (instructions or "").rstrip()
    if line in text:
        return text
    return f"{text}\n\n{line}"


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


def session_update_for_agent(
    bp: dict[str, Any], agent: str, *, mid_call: bool = False
) -> dict[str, Any]:
    if agent not in bp["agents"]:
        raise KeyError(f"unknown agent {agent!r}")
    tools = [_tool_decl(bp["catalog"][name]) for name in tool_names(bp, agent)]
    session: dict[str, Any] = {
        "instructions": with_today_context(bp["agents"][agent]["instructions"]),
        "tools": tools,
    }
    # turn_detection / voice / pcm format are IDLE-only (first update).
    if not mid_call:
        silence_ms = int(os.environ.get("QWEN_VAD_SILENCE_MS", "800"))
        threshold = float(os.environ.get("QWEN_VAD_THRESHOLD", "0.5"))
        session.update({
            "modalities": ["text", "audio"],
            "voice": VOICE,
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            "turn_detection": {
                "type": "server_vad",
                "threshold": threshold,
                "silence_duration_ms": silence_ms,
            },
        })
    return {
        "type": "session.update",
        "event_id": _event_id(),
        "session": session,
    }


def advertised_tools(industry_dir: str | Path, agent: str | None = None) -> list[str]:
    bp = load_blueprint(industry_dir)
    name = agent or bp["start"]
    return [
        t["function"]["name"]
        for t in session_update_for_agent(bp, name)["session"]["tools"]
    ]


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

    if name == "end_call" or is_session_tool(bp, state["agent"], name):
        return {"success": True}, True

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{tool_server_url()}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        result = resp.json()
        if name == "transfer_to_human":
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
        "event_id": _event_id(),
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result),
        },
    }


def speak_first_seed() -> dict[str, Any]:
    """Qwen-Audio rejects bare response.create on an empty conversation.

    Inject a user text item first so the model can open (pack owns greeting text).
    """
    return {
        "type": "conversation.item.create",
        "event_id": _event_id(),
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "The caller is connected."}],
        },
    }


def nudge_greeting() -> dict[str, Any]:
    return {
        "type": "response.create",
        "event_id": _event_id(),
        "response": {"modalities": ["audio", "text"]},
    }


def handoff_nudge_event() -> dict[str, Any]:
    return nudge_greeting()


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
    result, stop = await run_tool(name, args, bp, state, call_id=call_id)
    return result, stop, function_call_output(call_id, result)


async def configure_session(ws, agent: str, bp: dict[str, Any], *, timeout: float = 60.0) -> dict:
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
    import asyncio
    import contextlib

    from report import traced_run

    bp = load_blueprint(industry_dir)
    name = Path(industry_path(industry_dir)).name

    async with traced_run(f"mivas-{name}-{model}", model=model):
        async with connect_qwen(model) as qwen:
            created = json.loads(await asyncio.wait_for(qwen.recv(), timeout=30))
            print(f"session {created.get('type')}", flush=True)
            for agent in bp["agents"]:
                updated = await configure_session(qwen, agent, bp)
                n = len((updated.get("session") or {}).get("tools") or [])
                print(f"{agent} {updated.get('type')} tools={n} ok", flush=True)
            with contextlib.suppress(Exception):
                await qwen.close()


def demo() -> None:
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
        assert [t["function"]["name"] for t in session["tools"]] == names
        assert all(t.get("type") == "function" and "function" in t for t in session["tools"])
        assert session["turn_detection"]["type"] == "server_vad"
        assert session["input_audio_format"] == "pcm"
        assert session["output_audio_format"] == "pcm"
        leaked = all_names - set(names)
        advertised = [t["function"]["name"] for t in session["tools"]]
        for n in leaked:
            assert n not in advertised, f"{agent} leaked {n}"
    nxt = extract_appointment_date("next Tuesday afternoon")
    assert nxt and nxt.endswith(f"/{_dt.date.today().year}")
    assert infer_schedule_appointment("Booking confirmed for March 18.") == {
        "date": f"03/18/{_dt.date.today().year}"
    }
    assert handoff_nudge_event()["type"] == "response.create"
    seed = speak_first_seed()
    assert seed["type"] == "conversation.item.create"
    assert seed["item"]["role"] == "user"
    mid = session_update_for_agent(bp, start, mid_call=True)["session"]
    assert "turn_detection" not in mid and "voice" not in mid
    assert "instructions" in mid and "tools" in mid
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
        f"qwen self-check ok start={start} tools={start_tools} "
        f"agents={list(bp['agents'])} model={MODEL}"
    )


if __name__ == "__main__":
    demo()
