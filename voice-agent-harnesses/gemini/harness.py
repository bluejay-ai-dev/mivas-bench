"""Gemini Live over LiveKit SIP.

Bluejay dials this project's SIP host. An inbound trunk plus dispatch rule
create the room and dispatch `agent_name`. Audio is LiveKit's SIP mix.

Each blueprint agent gets its own Gemini Live session (prompt + that agent's
tools only). Gemini Live cannot swap tools on an open socket.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
from google.genai import types as genai_types
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobExecutorType,
    RunContext,
    cli,
    function_tool,
)

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import begin_session, end_session, headers as tool_headers, set_call_id  # noqa: E402

import report  # noqa: E402

logger = logging.getLogger("mivas.gemini")

# Gemini kills the session (1007 CONTENT_TYPE_AUDIO not supported) whenever the
# plugin replays chat history containing audio items via client_content. The
# sync runs inside livekit after every tool call, so filter at the source.
from livekit.plugins.google.realtime import realtime_api as _grt  # noqa: E402

_orig_update_chat_ctx = _grt.RealtimeSession.update_chat_ctx


async def _text_only_update_chat_ctx(self: Any, chat_ctx: Any) -> None:
    ctx = chat_ctx.copy()  # incoming ctx may be read-only
    stripped = 0
    kept_items = []
    for item in ctx.items:
        if getattr(item, "type", "") == "message":
            kept = [c for c in item.content if isinstance(c, str)]
            stripped += len(item.content) - len(kept)
            if not kept:
                continue
            item.content = kept
        kept_items.append(item)
    ctx.items[:] = kept_items
    if stripped:
        logger.info("stripped %d non-text content parts from chat ctx", stripped)
    await _orig_update_chat_ctx(self, ctx)


_grt.RealtimeSession.update_chat_ctx = _text_only_update_chat_ctx

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
_OPENER = re.compile(r'(?:^|\n)\s*1\.\s*(?:Ask|Say):\s*"([^"]+)"', re.IGNORECASE)
_SCHEMA_KEYS = {"type", "description", "enum", "items", "properties", "required"}


def _prop(prop: dict[str, Any]) -> dict[str, Any]:
    """Keep the JSON Schema keys Gemini Live accepts, including array `items`."""
    out = {k: v for k, v in prop.items() if k in _SCHEMA_KEYS}
    if isinstance(out.get("items"), dict):
        out["items"] = _prop(out["items"]) or {"type": "string"}
    if isinstance(out.get("properties"), dict):
        out["properties"] = {
            k: _prop(v) for k, v in out["properties"].items() if isinstance(v, dict)
        }
    if out.get("type") == "array" and not out.get("items"):
        out["items"] = {"type": "string"}
    return out


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    return (REPO_ROOT / "industries" / name).resolve()


def load_blueprint(industry_dir: str | Path | None = None) -> dict[str, Any]:
    industry_dir = industry_path(industry_dir or os.environ.get("INDUSTRY", "control-industry"))
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
        "greeting": (blueprint.get("greeting") or "").strip(),
        "agents": agents,
        "catalog": catalog,
    }


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


def with_clock(instructions: str) -> str:
    today = _dt.date.today()
    return instructions.rstrip() + f"\n\nToday is {today:%A, %B} {today.day}, {today.year}."


def greeting(bp: dict[str, Any]) -> str:
    if bp.get("greeting"):
        return str(bp["greeting"])
    return os.environ.get("TWILIO_WELCOME_GREETING", "").strip() or "Hello."


def speak_first(instructions: str, line: str) -> str:
    return instructions.rstrip() + f'\n\nThe call just connected. Speak first. Greet the caller with: "{line}"'


def kick(session: AgentSession, text: str) -> None:
    """3.1 treats client_content as history only (initial_history_in_client_content),
    so completed turns never generate. Realtime text input does."""
    activity = getattr(session, "_activity", None)
    rt = getattr(activity, "_rt_session", None)
    send = getattr(rt, "_send_client_event", None)
    if send is None:
        logger.warning("kick: no gemini session")
        return
    send(genai_types.LiveClientRealtimeInput(text=text))
    logger.info("kicked gemini speak-first")


def opener(instructions: str) -> str | None:
    m = _OPENER.search(instructions)
    return m.group(1).strip() if m else None


def agent_name(default: str) -> str:
    explicit = os.environ.get("LIVEKIT_AGENT_NAME", "").strip()
    if explicit:
        return explicit
    slug = os.environ.get("MIVAS_SLUG", "").strip()
    return f"mivas-{slug}" if slug else default


def job_count_load(max_jobs: int) -> Callable[[Any], float]:
    cap = max(max_jobs, 1)

    def _load(server: Any) -> float:
        n = len(getattr(server, "active_jobs", None) or [])
        return min(n / cap, 1.0)

    return _load


def sim_id(ctx: JobContext, participant: Any) -> str | None:
    attrs = dict(getattr(participant, "attributes", None) or {})
    for key, val in attrs.items():
        if "simulation-result-id" in str(key).lower().replace("_", "-"):
            if val and str(val).strip():
                return str(val).strip()
    meta = getattr(ctx.job, "metadata", None)
    if not meta:
        return None
    raw = meta if isinstance(meta, dict) else None
    if raw is None:
        try:
            raw = json.loads(str(meta))
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    for key in ("X-Simulation-Result-Id", "x-simulation-result-id", "simulation_result_id"):
        val = raw.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


async def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        return resp.json()


def _tools(
    bp: dict[str, Any],
    name: str,
    hangup: asyncio.Event,
    make_llm: Callable[[str], Any],
    scripted: bool,
) -> list[Any]:
    out: list[Any] = []
    for spec in bp["agents"][name]["tools"]:
        catalog = bp["catalog"][spec["name"]]
        raw = dict(catalog.get("inputSchema") or {})
        raw.pop("additionalProperties", None)
        props = {
            k: _prop(dict(v))
            for k, v in (raw.get("properties") or {}).items()
            if isinstance(v, dict)
        }
        params: dict[str, Any] = {"type": "object", "properties": props}
        if raw.get("required"):
            params["required"] = list(raw["required"])
        schema = {
            "name": spec["name"],
            "description": catalog.get("description") or spec["name"],
            "parameters": params,
        }
        if spec.get("handoff"):
            target = spec["handoff_to"]

            async def _handoff(
                raw_arguments: dict[str, Any],
                context: RunContext,
                *,
                _target: str = target,
            ) -> dict[str, Any]:
                # new Gemini socket starts blank; carry the turns over so the next
                # stage hears the conversation (3.1 injects them as initial history)
                # audio items are stripped by the update_chat_ctx patch above
                prior = context.session.current_agent.chat_ctx.copy(
                    exclude_instructions=True, exclude_function_call=True
                )
                stage = Stage(
                    bp, _target, hangup, make_llm, scripted,
                    entered_by_handoff=True, chat_ctx=prior,
                )
                # swap explicitly instead of returning the Stage: Gemini cancels
                # in-flight tool calls on barge-in, and a swap riding that
                # generation pipeline dies with it, muting the call
                context.session.update_agent(stage)
                return {"ok": True, "transferred_to": _target}

            out.append(function_tool(_handoff, raw_schema=schema))
            continue

        async def _run(
            raw_arguments: dict[str, Any],
            *,
            _n: str = spec["name"],
            _stop: bool = spec["name"] == "end_call" or bool(spec.get("session")),
        ) -> dict[str, Any]:
            result = {"ok": True, "tool": _n}
            if _n != "end_call":
                result = await _dispatch(_n, dict(raw_arguments))
            if _stop:
                hangup.set()
            return result

        out.append(function_tool(_run, raw_schema=schema))
    return out


class Stage(Agent):
    def __init__(
        self,
        bp: dict[str, Any],
        name: str,
        hangup: asyncio.Event,
        make_llm: Callable[[str], Any],
        scripted: bool,
        *,
        entered_by_handoff: bool = False,
        chat_ctx: Any = None,
        speak_first_line: str | None = None,
    ):
        instructions = with_clock(bp["agents"][name]["instructions"])
        if speak_first_line:
            # must live on the Agent, not the RealtimeModel: livekit overrides
            # the model's instructions with the Agent's at activity start
            instructions = speak_first(instructions, speak_first_line)
        kwargs: dict[str, Any] = {}
        if chat_ctx is not None:
            kwargs["chat_ctx"] = chat_ctx
        super().__init__(
            instructions=instructions,
            llm=make_llm(name),
            tools=_tools(bp, name, hangup, make_llm, scripted),
            **kwargs,
        )
        self._opener = opener(instructions) if scripted else None
        # 3.1 (scripted) cannot generate_reply; every handoff entry needs a kick
        self._kick = scripted
        self._entered_by_handoff = entered_by_handoff

    async def on_enter(self) -> None:
        if not self._entered_by_handoff:
            return
        if self._kick:
            text = (
                f'Speak to the caller now: "{self._opener}"'
                if self._opener
                else "Speak to the caller now."
            )
            kick(self.session, text)
        else:
            self.session.generate_reply()


async def run_call(
    ctx: JobContext,
    *,
    build_session: Callable[[dict[str, Any]], AgentSession],
    make_llm: Callable[[str], Any],
    scripted: bool,
    greet: str,
    model: str,
) -> None:
    report.setup_otel()  # per job: livekit shuts the provider down at job end
    participant = None
    try:
        participant = await ctx.wait_for_participant()
    except Exception as e:
        logger.warning("wait_for_participant: %s", e)
    sid = sim_id(ctx, participant)
    logger.info("job start room=%s sim=%s model=%s", ctx.room.name, sid, model)
    set_call_id(sid)
    session_key = getattr(ctx.room, "name", None) or "job"
    begin_session(sid, session_key=session_key)
    ctx.add_shutdown_callback(lambda *_: asyncio.to_thread(end_session, session_key))

    bp = load_blueprint()
    hangup = asyncio.Event()
    disconnected = asyncio.Event()

    @ctx.room.on("disconnected")
    def _gone(*_: Any) -> None:
        disconnected.set()

    session = build_session(bp)

    @session.on("close")
    def _session_closed(ev: Any) -> None:
        # unrecoverable model error (e.g. Gemini 1007) otherwise leaves the
        # caller in dead air until their own hangup timer
        err = getattr(ev, "error", None)
        if err is not None:
            logger.error("session closed with error, ending call: %s", err)
            hangup.set()

    start = Stage(
        bp, bp["start"], hangup, make_llm, scripted, speak_first_line=greeting(bp)
    )
    await session.start(room=ctx.room, agent=start)
    tid = report.capture_trace()
    ctx.add_shutdown_callback(lambda *_: report.link(sid, tid))
    if greet == "kick":
        # the greeting is pinned in the connect-time instructions (speak_first);
        # quoting it here reads as the other party's line and the model answers
        # it instead of speaking it (role inversion)
        kick(session, ".")
    else:
        # greeting is pinned in the start agent's system prompt (speak_first)
        session.generate_reply()

    wait = [asyncio.create_task(hangup.wait()), asyncio.create_task(disconnected.wait())]
    await asyncio.wait(wait, return_when=asyncio.FIRST_COMPLETED)
    for t in wait:
        t.cancel()
    if hangup.is_set() and not disconnected.is_set():
        # end_call fires mid-farewell; let the playout finish before hanging up
        try:
            async with asyncio.timeout(15):
                while True:
                    speech = session.current_speech
                    if speech is not None and not speech.done():
                        await speech.wait_for_playout()
                        continue
                    # farewell can start after the end_call tool result lands
                    await asyncio.sleep(1.0)
                    if session.current_speech is None:
                        break
        except Exception:
            pass
        try:
            await ctx.delete_room()
        except Exception as e:
            logger.warning("delete_room: %s", e)
    logger.info("call finished room=%s hangup=%s", ctx.room.name, hangup.is_set())


def serve(
    default_name: str,
    *,
    build_session: Callable[[dict[str, Any]], AgentSession],
    make_llm: Callable[[str], Any],
    model: str,
    greet: str,
    scripted: bool,
) -> None:
    name = agent_name(default_name)
    logger.info("registering livekit agent_name=%s", name)
    report.setup_otel()

    async def entrypoint(ctx: JobContext) -> None:
        await run_call(
            ctx,
            build_session=build_session,
            make_llm=make_llm,
            scripted=scripted,
            greet=greet,
            model=model,
        )

    # One Gemini Live socket (+ a second on handoff) per process. CPU load is
    # idle at assign time, so count jobs instead of CPU or three rooms stack
    # on one replica and two stay silent.
    server = AgentServer(job_executor_type=JobExecutorType.THREAD, load_threshold=0.5)
    server.load_fnc = job_count_load(1)
    server.rtc_session(agent_name=name)(entrypoint)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger.setLevel(logging.INFO)
    if os.environ.get("LK_GOOGLE_DEBUG"):
        # the plugin's frame dumps log at DEBUG; INFO root swallows them
        logging.getLogger("livekit.plugins.google").setLevel(logging.DEBUG)
    cli.run_app(server)
