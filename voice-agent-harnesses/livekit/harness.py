"""industry blueprint → LiveKit Agents, shared by all three runtimes.

LiveKit runs our code, so unlike the Vapi/Retell/Bland/Cartesia harnesses there is
no tool webhook and no tunnel: every tool body runs in this process and is wrapped
directly in an `execute_tool` span. Industry tools dispatch generically to the
tool server's POST /tools/{name} route. Multi-agent handoff is in-framework — a
handoff tool returns the target `BlueprintAgent` instance — so the handoff is a
real, timed tool call rather than a provider-internal jump.

Transport is Bluejay `connection_type=SIP` into this LiveKit Cloud project.
Bluejay dials `sip:<number>@<project>.sip.livekit.cloud`; an inbound trunk plus
dispatch rule create the room and dispatch our `agent_name`. Audio is the stock
LiveKit SIP mix — no CHIRP bridge and no custom RoomIO patching.
`X-Simulation-Result-Id` arrives on the SIP INVITE (GetRemoteHeaders / participant
attributes), not on LiveKit job metadata.

All three runtimes use the same handoff, including the speech-to-speech ones. The
`mutable_*` capability flags only gate *mutating an existing* realtime session, so
a runtime whose model cannot be mutated (Gemini 3.1 Live) gives each `Agent` its
own `llm=` instead: `AgentActivity._detach_reusable_resources` only reuses the
realtime session when `self.llm is new_activity.llm`, so a distinct model instance
forces `llm.session()` — a second, independently configured provider session with
the new agent's prompt and only the new agent's tools.
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
from livekit.agents import (
    NOT_GIVEN,
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobExecutorType,
    NotGivenOr,
    cli,
    function_tool,
)

import report

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import begin_session, end_session, headers as tool_headers, set_call_id  # noqa: E402

logger = logging.getLogger("mivas.livekit")

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
INDUSTRY = os.environ.get("INDUSTRY", "control-industry")
# Fallback only for packs that omit `greeting` (control-industry). Healthcare
# and the other industry packs put the spoken opener on agent_blueprint.json;
# generate_reply/say must use that, not this repair-shop line.
GREETING = "Welcome to Bluejay's Repair Services!"
# Matches *step 1* of an agent's own flow, e.g.
# `1. Ask: "Hey, when do you want to schedule your repair appointment?"` in
# system-prompts/scheduler.md. Anchored to step 1 specifically (not any numbered
# `Ask:`/`Say:` line) so this doesn't pick up an unrelated mid-prompt instruction,
# e.g. a later "call 911, then say ..." escalation step. Used to give handoff
# targets a target-specific opener on runtimes whose model rejects
# generate_reply() (see BlueprintAgent.on_enter and `_derive_opener` below); every
# other turn is model-generated.
_OPENER_RE = re.compile(r'(?:^|\n)\s*1\.\s*(?:Ask|Say):\s*"([^"]+)"', re.IGNORECASE)
# Last-resort opener when an agent's prompt has no quoted `Ask:`/`Say:` line to pull
# from. Deliberately industry- and blueprint-neutral so it never announces another
# agent's task (e.g. a scheduling line) on a target that doesn't do that job.
GENERIC_OPENER = "Okay, I can help you with that."


def _derive_opener(instructions: str) -> str:
    """Pull a natural, spoken opening line out of `instructions` for `session.say()`.

    Every `BlueprintAgent` derives its *own* opener from its *own* prompt, so a
    handoff target never inherits another agent's scripted line (e.g. Gemini Live
    used to open every handoff with the control-industry scheduler's "when do you
    want to schedule your repair appointment?" line regardless of which agent, or
    industry, it had actually landed on).
    """
    match = _OPENER_RE.search(instructions)
    return match.group(1) if match else GENERIC_OPENER


# Seconds of *silence* the agent must reach after `end_call` before we tear the room
# down. This was a flat sleep, which deleted the room mid-farewell on gpt-realtime-2.1
# (see README "Hanging up waits for silence"). Same env knobs as the pipecat harness.
HANGUP_QUIET_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "4.0"))
# hard cap so a model that never stops talking cannot hold the room forever
HANGUP_MAX_WAIT_S = float(os.environ.get("MIVAS_END_CALL_MAX_WAIT_S", "20.0"))


# ── blueprint ────────────────────────────────────────────────────────────────


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    return (REPO_ROOT / "industries" / name).resolve()


def load_blueprint(industry_dir: str | Path = INDUSTRY) -> dict[str, Any]:
    industry_dir = industry_path(industry_dir)
    blueprint = json.loads((industry_dir / "agent_blueprint.json").read_text())
    catalog = {
        t["name"]: t for t in json.loads((industry_dir / "tools.json").read_text())["tools"]
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
        "greeting": (blueprint.get("greeting") or "").strip(),
    }


def pack_greeting(bp: dict[str, Any] | None = None) -> str:
    """Spoken opener for this pack. Blueprint wins; GREETING is control-industry."""
    if bp is not None and (bp.get("greeting") or "").strip():
        return str(bp["greeting"]).strip()
    return GREETING


def today_clock() -> str:
    d = _dt.date.today()
    return f"Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}."


def with_clock(instructions: str) -> str:
    """gpt-4.1 (and the S2S models) have no 'today'; relative dates invent years."""
    clock = today_clock()
    if clock in instructions:
        return instructions
    return f"{instructions.rstrip()}\n\n{clock}"


def resolve_agent_name(default: str) -> str:
    """LiveKit dispatch name. Unique per k8s slug so two industries cannot collide.

    Local/dev (no MIVAS_SLUG) keeps the runtime default (`mivas-livekit-cascaded`).
    LIVEKIT_AGENT_NAME wins when set.
    """
    explicit = os.environ.get("LIVEKIT_AGENT_NAME", "").strip()
    if explicit:
        return explicit
    slug = os.environ.get("MIVAS_SLUG", "").strip()
    if slug:
        return f"mivas-{slug}"
    return default


# ── tools (in-process, each under an execute_tool span) ──────────────────────


async def _execute(name: str, args: dict[str, Any], *, local: bool) -> dict[str, Any]:
    if local:
        # harness-native (handoff / session): no industry state to mutate,
        # the span is the artifact
        return {"success": True}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        return r.json()


async def run_tool(name: str, args: dict[str, Any], *, local: bool = False) -> dict[str, Any]:
    """Run one blueprint tool under an `execute_tool` span. Never raises.

    `local=True` marks harness-native tools (handoffs, `end_call`); everything
    else dispatches to POST {TOOL_SERVER_URL}/tools/{name} and returns the
    server's envelope verbatim. Human-transfer session tools POST, then hang up.
    """
    offset = report.call_offset_ms()
    with report.tool_span(name, args) as span:
        try:
            result = await _execute(name, args, local=local)
            # default to False, not True: a 404/error envelope from POST /tools/{name}
            # (e.g. an unknown tool name) has neither `ok` nor `success`, and treating
            # that as a successful call would hide the failure from the trace.
            ok = bool(result.get("ok", result.get("success", False)))
        except Exception as e:  # soft-fail: the call (and its trace) must still finish
            result, ok = {"success": False, "error": f"{type(e).__name__}: {e}"}, False
        report.finish_tool_span(span, result, ok=ok)
    logger.info("tool %s args=%s -> %s (+%dms)", name, args, result, offset)
    return result


# ── agents ───────────────────────────────────────────────────────────────────


def _param_schema(spec: dict[str, Any]) -> dict[str, Any]:
    raw = dict(spec.get("inputSchema") or {})
    schema: dict[str, Any] = {"type": "object", "properties": dict(raw.get("properties") or {})}
    if raw.get("required"):
        schema["required"] = list(raw["required"])
    return schema


def _blueprint_tools(
    bp: dict[str, Any],
    agent_name: str,
    hangup: asyncio.Event,
    llm_factory: Callable[[str], Any] | None,
    scripted_opener: bool,
) -> list[Any]:
    """One agent's blueprint tools as LiveKit raw function tools.

    Handoffs return the target `BlueprintAgent` and nothing else: any non-Agent
    output sets reply_required, which makes the outgoing agent announce the
    transfer on top of the incoming agent's own opener — the blueprint forbids
    that, and the two overlapping turns let the caller's "okay, thank you" land
    on the new agent as "I'm done" -> end_call before anything is booked.

    `scripted_opener` only says whether *this runtime's model* rejects
    generate_reply() and therefore needs its handoff targets to speak a scripted
    first line at all; it carries no agent-specific text. Each target derives its
    own opener from its own prompt (see `BlueprintAgent.on_enter`/`_derive_opener`)
    so a handoff never announces another agent's task.
    """
    tools: list[Any] = []
    for t in bp["agents"][agent_name]["tools"]:
        name = t["name"]
        spec = bp["catalog"].get(name) or {}
        raw = {
            "name": name,
            "description": spec.get(
                "description",
                f"Hand off to the {t.get('handoff_to')} agent." if t.get("handoff") else name,
            ),
            "parameters": _param_schema(spec),
        }

        if t.get("handoff"):
            def _make_handoff(tool_name: str, target: str):
                async def _handoff(raw_arguments: dict[str, Any]) -> Agent:
                    await run_tool(tool_name, dict(raw_arguments), local=True)
                    return BlueprintAgent(
                        bp, target, hangup,
                        llm_factory=llm_factory,
                        scripted_opener=scripted_opener,
                        entered_by_handoff=True,
                    )
                return _handoff

            tools.append(function_tool(_make_handoff(name, t["handoff_to"]), raw_schema=raw))
        elif t.get("session"):
            def _make_session(tool_name: str):
                async def _session_tool(raw_arguments: dict[str, Any]) -> dict[str, Any]:
                    # end_call is local; human-transfer session tools still POST.
                    result = await run_tool(
                        tool_name, dict(raw_arguments), local=tool_name == "end_call"
                    )
                    hangup.set()
                    return result
                return _session_tool

            tools.append(function_tool(_make_session(name), raw_schema=raw))
        else:
            def _make_industry(tool_name: str):
                async def _industry(raw_arguments: dict[str, Any]) -> dict[str, Any]:
                    return await run_tool(tool_name, dict(raw_arguments))
                return _industry

            tools.append(function_tool(_make_industry(name), raw_schema=raw))
    return tools


class BlueprintAgent(Agent):
    """One blueprint agent: its own prompt, its own tools, generic dispatch.

    `llm_factory(agent_name)` gives each agent its own model instance, which is
    what makes a handoff a real switch on models that cannot be mutated
    mid-session: LiveKit only carries the realtime session across a handoff when
    the two activities share the *same* model object, so a fresh instance means
    a fresh provider session configured with this agent's prompt and tools.

    `scripted_opener=True` means this runtime's model rejects generate_reply()
    (mutable_chat_context=False), so a handoff target must speak a scripted first
    line via `session.say()` instead; everyone else model-generates the opener.
    That scripted line is always derived from *this* agent's own instructions
    (`_derive_opener`), never inherited from whichever agent handed off to it — a
    flat, runtime-wide literal here would put another agent's task in this one's
    mouth on any blueprint with more than one possible handoff target. The start
    agent's greeting is `run_call`'s job, not this one's.
    """

    def __init__(
        self,
        bp: dict[str, Any],
        name: str,
        hangup: asyncio.Event,
        *,
        llm_factory: Callable[[str], Any] | None = None,
        scripted_opener: bool = False,
        entered_by_handoff: bool = False,
    ):
        instructions = with_clock(bp["agents"][name]["instructions"])
        llm: NotGivenOr[Any] = llm_factory(name) if llm_factory else NOT_GIVEN
        super().__init__(
            instructions=instructions,
            llm=llm,
            tools=_blueprint_tools(bp, name, hangup, llm_factory, scripted_opener),
        )
        self.agent_name = name
        self._opener = _derive_opener(instructions) if scripted_opener else None
        self._entered_by_handoff = entered_by_handoff

    async def on_enter(self) -> None:
        if not self._entered_by_handoff:
            return  # the call-opening greeting is run_call's job
        if self._opener:
            self.session.say(self._opener)
        else:
            self.session.generate_reply()


# ── job plumbing ─────────────────────────────────────────────────────────────


def sip_host() -> str:
    """LiveKit Cloud SIP hostname (`<id>.sip.livekit.cloud`). Not the wss project name."""
    return os.environ.get("LIVEKIT_SIP_HOST", "").strip().removeprefix("sip:")


def sip_number(default: str = "+15551230000") -> str:
    """Routing key on the inbound trunk. Must match the Bluejay `sip_uri` user part."""
    return os.environ.get("LIVEKIT_SIP_NUMBER", "").strip() or default


def sip_uri(*, number: str | None = None) -> str | None:
    """Bluejay agent `sip_uri`: `sip:<number>@<LIVEKIT_SIP_HOST>`."""
    host = sip_host()
    if not host:
        return None
    return f"sip:{number or sip_number()}@{host}"


def sim_result_id_from_job_metadata(raw: Any) -> str | None:
    """Bluejay puts X-Simulation-Result-Id on LiveKit job metadata JSON."""
    if not raw:
        return None
    meta = raw if isinstance(raw, dict) else None
    if meta is None:
        try:
            meta = json.loads(str(raw))
        except Exception:
            logger.warning("job.metadata is not valid JSON: %s", raw)
            return None
    if not isinstance(meta, dict):
        return None
    for key in (
        "X-Simulation-Result-Id",
        "x-simulation-result-id",
        "simulation_result_id",
        "simulationResultId",
    ):
        val = meta.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def sim_result_id_from_participant(participant: Any) -> str | None:
    """SIP inbound stamps the sim id on participant attributes, not job metadata."""
    if participant is None:
        return None
    attrs = dict(getattr(participant, "attributes", None) or {})
    for key, val in attrs.items():
        kl = str(key).lower().replace("_", "-")
        if "simulation-result-id" in kl or kl.endswith("simulation-result-id"):
            if val is not None and str(val).strip():
                return str(val).strip()
    return sim_result_id_from_job_metadata(attrs) or sim_result_id_from_job_metadata(
        getattr(participant, "metadata", None)
    )


async def sim_result_id_from_sip(ctx: JobContext, participant: Any) -> str | None:
    """Read X-Simulation-Result-Id from the SIP INVITE (RPC, then attributes)."""
    if participant is None:
        return None
    try:
        from livekit import rtc

        if getattr(participant, "kind", None) == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            response = await ctx.room.local_participant.perform_rpc(
                destination_identity=participant.identity,
                method="lk.sip.GetRemoteHeaders",
                payload="{}",
            )
            headers = (json.loads(response) or {}).get("headers") or {}
            sid = sim_result_id_from_job_metadata(headers)
            if sid:
                return sid
    except Exception as e:
        logger.warning("GetRemoteHeaders: %s", e)
    return sim_result_id_from_participant(participant)


async def await_farewell(session: AgentSession, disconnected: asyncio.Event) -> None:
    """Hold the room until the agent has gone quiet for HANGUP_QUIET_S.

    Polled, not edge-triggered: the farewell can start anywhere in this window and a
    one-shot wait would miss the transition. The quiet timer restarts on every busy
    sample, so the pause before the goodbye does not count as "done talking".
    """
    loop = asyncio.get_running_loop()
    t0 = quiet_since = loop.time()
    while not disconnected.is_set() and loop.time() - t0 < HANGUP_MAX_WAIT_S:
        # "thinking" counts: it is the gap between the end_call tool result and the
        # farewell audio, exactly where a speaking-only check fires early.
        if session.agent_state in ("thinking", "speaking"):
            quiet_since = loop.time()
        elif loop.time() - quiet_since >= HANGUP_QUIET_S:
            break
        await asyncio.sleep(0.2)
    logger.info("farewell wait %.1fs (state=%s)", loop.time() - t0, session.agent_state)


def wire_speech_spans(session: AgentSession) -> None:
    """agent.speech / customer.speech from real turn events (no silence heuristic)."""
    spans: dict[str, Any] = {"agent": None, "customer": None}
    counter = {"n": 0}

    def toggle(who: str, speaking: bool) -> None:
        if speaking and spans[who] is None:
            counter["n"] += 1
            spans[who] = report.start_speech_span(f"{who}-{counter['n']}", speaker=who)
        elif not speaking and spans[who] is not None:
            report.end_speech_span(spans[who])
            spans[who] = None

    @session.on("agent_state_changed")
    def _agent(ev: Any) -> None:
        toggle("agent", str(ev.new_state) == "speaking")

    @session.on("user_state_changed")
    def _user(ev: Any) -> None:
        toggle("customer", str(ev.new_state) == "speaking")

    @session.on("close")
    def _close(_ev: Any) -> None:
        toggle("agent", False)
        toggle("customer", False)


async def run_call(
    ctx: JobContext,
    *,
    build_session: Callable[[dict[str, Any]], AgentSession],
    build_agent: Callable[[dict[str, Any], asyncio.Event], Agent],
    model: str,
    greet: str = "generate_reply",
) -> None:
    """Shared entrypoint body: trace the call, run it, then link the trace.

    The POST lives here rather than in a shutdown callback: `traced_run` waits for
    a *final* simulation status before its single POST (posting during EVALUATING
    and relinking double-counts every tool), and the entrypoint gets
    `AgentServer.session_end_timeout` (300 s) to finish while shutdown callbacks
    only get `shutdown_process_timeout` (10 s).
    """
    sim_result_id = sim_result_id_from_job_metadata(ctx.job.metadata)
    participant = None
    try:
        participant = await ctx.wait_for_participant()
    except Exception as e:
        logger.warning("wait_for_participant: %s", e)
    if not sim_result_id:
        sim_result_id = await sim_result_id_from_sip(ctx, participant)
    logger.info("job start room=%s sim_result_id=%s model=%s", ctx.room.name, sim_result_id, model)
    set_call_id(sim_result_id)
    session_key = getattr(ctx.room, "name", None) or "job"
    begin_session(sim_result_id, session_key=session_key)
    # Shutdown callback, not the traced_run finally: this fires on error and on
    # LiveKit's entrypoint cancel too. The freeze is a local GET + one S3 PUT, so
    # it fits the 10 s shutdown_process_timeout that the enrichment POST cannot.
    ctx.add_shutdown_callback(
        lambda *_: asyncio.to_thread(end_session, session_key)
    )

    bp = load_blueprint()
    async with report.traced_run(
        f"mivas-{Path(bp['industry_dir']).name}",
        simulation_result_id=sim_result_id,
        model=model,
    ):
        hangup = asyncio.Event()
        disconnected = asyncio.Event()

        @ctx.room.on("disconnected")
        def _on_disconnect(*_: Any) -> None:
            disconnected.set()

        session = build_session(bp)
        wire_speech_spans(session)
        await session.start(room=ctx.room, agent=build_agent(bp, hangup))

        greeting = pack_greeting(bp)
        if greet == "say":
            # realtime models with mutable_chat_context=False reject generate_reply;
            # a TTS on the session lets say() deliver the scripted opener instead.
            await session.say(greeting)
        elif greet == "generate_reply":
            session.generate_reply(instructions=f'Greet the caller with: "{greeting}"')

        done = [asyncio.create_task(hangup.wait()), asyncio.create_task(disconnected.wait())]
        await asyncio.wait(done, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.cancel()

        if hangup.is_set() and not disconnected.is_set():
            await await_farewell(session, disconnected)
            try:
                await ctx.delete_room()
            except Exception as e:
                logger.warning("delete_room failed: %s", e)
        logger.info("call finished room=%s hangup=%s", ctx.room.name, hangup.is_set())


def serve(
    agent_name: str,
    *,
    build_session: Callable[[dict[str, Any]], AgentSession],
    build_agent: Callable[[dict[str, Any], asyncio.Event], Agent],
    model: str,
    greet: str = "generate_reply",
) -> None:
    """Register one runtime with LiveKit and hand control to the agents CLI."""
    agent_name = resolve_agent_name(agent_name)
    logger.info("registering livekit agent_name=%s", agent_name)

    async def entrypoint(ctx: JobContext) -> None:
        await run_call(
            ctx,
            build_session=build_session,
            build_agent=build_agent,
            model=model,
            greet=greet,
        )

    # THREAD, not the default PROCESS: spawn pickles the entrypoint by reference and
    # this one is a closure over the runtime's session/agent factories. Threads also
    # keep the trace-linking POST on a loop that outlives the individual job.
    server = AgentServer(job_executor_type=JobExecutorType.THREAD)
    server.rtc_session(agent_name=agent_name)(entrypoint)

    # the agents CLI installs the root handler; just keep the noisy libs down
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("livekit.plugins.silero").setLevel(logging.ERROR)
    logger.setLevel(logging.INFO)

    cli.run_app(server)
