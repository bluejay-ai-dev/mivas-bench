"""industry blueprint → LiveKit Agents, shared by all three runtimes.

LiveKit runs our code, so unlike the Vapi/Retell/Bland/Cartesia harnesses there is
no tool webhook and no tunnel: every tool body runs in this process and is wrapped
directly in an `execute_tool` span. Industry tools dispatch generically to the
tool server's POST /tools/{name} route. Multi-agent handoff is in-framework — a
handoff tool returns the target `BlueprintAgent` instance — so the handoff is a
real, timed tool call rather than a provider-internal jump.

Transport is native Bluejay `LIVEKIT` dispatch: Bluejay creates the room, mints a
token with a `RoomAgentDispatch` for our `agent_name`, and puts
`X-Simulation-Result-Id` on the job metadata (see livekit_agent
`src/agent_bootstrap/hydration.py`). There is no CHIRP bridge.

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
import json
import logging
import os
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

logger = logging.getLogger("mivas.livekit")

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
INDUSTRY = os.environ.get("INDUSTRY", "control-industry")
GREETING = "Welcome to Bluejay's Repair Services!"
# step 1 of system-prompts/scheduler.md, verbatim. Only used by runtimes whose model
# rejects generate_reply() (see BlueprintAgent.on_enter); every other turn is
# model-generated.
SCHEDULER_OPENER = "Hey, when do you want to schedule your repair appointment?"
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
    }


# ── tools (in-process, each under an execute_tool span) ──────────────────────


async def _execute(name: str, args: dict[str, Any], *, local: bool) -> dict[str, Any]:
    if local:
        # harness-native (handoff / session): no industry state to mutate,
        # the span is the artifact
        return {"success": True}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{TOOL_SERVER_URL}/tools/{name}", json={"arguments": args})
        return r.json()


async def run_tool(name: str, args: dict[str, Any], *, local: bool = False) -> dict[str, Any]:
    """Run one blueprint tool under an `execute_tool` span. Never raises.

    `local=True` marks harness-native tools (handoffs, session tools); everything
    else dispatches to POST {TOOL_SERVER_URL}/tools/{name} and returns the
    server's envelope verbatim.
    """
    offset = report.call_offset_ms()
    with report.tool_span(name, args) as span:
        try:
            result = await _execute(name, args, local=local)
            ok = bool(result.get("ok", result.get("success", True)))
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
    opener: str | None,
) -> list[Any]:
    """One agent's blueprint tools as LiveKit raw function tools.

    Handoffs return the target `BlueprintAgent` and nothing else: any non-Agent
    output sets reply_required, which makes the outgoing agent announce the
    transfer on top of the incoming agent's own opener — the blueprint forbids
    that, and the two overlapping turns let the caller's "okay, thank you" land
    on the new agent as "I'm done" -> end_call before anything is booked.
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
                        llm_factory=llm_factory, opener=opener, entered_by_handoff=True,
                    )
                return _handoff

            tools.append(function_tool(_make_handoff(name, t["handoff_to"]), raw_schema=raw))
        elif t.get("session"):
            def _make_session(tool_name: str):
                async def _session_tool(raw_arguments: dict[str, Any]) -> dict[str, Any]:
                    result = await run_tool(tool_name, dict(raw_arguments), local=True)
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

    `opener` is a scripted first line for handoff targets on runtimes whose
    model rejects generate_reply() (mutable_chat_context=False); everyone else
    model-generates the opener. The start agent's greeting is `run_call`'s job.
    """

    def __init__(
        self,
        bp: dict[str, Any],
        name: str,
        hangup: asyncio.Event,
        *,
        llm_factory: Callable[[str], Any] | None = None,
        opener: str | None = None,
        entered_by_handoff: bool = False,
    ):
        llm: NotGivenOr[Any] = llm_factory(name) if llm_factory else NOT_GIVEN
        super().__init__(
            instructions=bp["agents"][name]["instructions"],
            llm=llm,
            tools=_blueprint_tools(bp, name, hangup, llm_factory, opener),
        )
        self.agent_name = name
        self._opener = opener
        self._entered_by_handoff = entered_by_handoff

    async def on_enter(self) -> None:
        if not self._entered_by_handoff:
            return  # the call-opening greeting is run_call's job
        if self._opener:
            self.session.say(self._opener)
        else:
            self.session.generate_reply()


# ── job plumbing ─────────────────────────────────────────────────────────────


def sim_result_id_from_job_metadata(raw: Any) -> str | None:
    """Bluejay puts X-Simulation-Result-Id on the LiveKit job metadata JSON."""
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
        # farewell audio, exactly where a speaking-only check fires early (713652).
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
    logger.info("job start room=%s sim_result_id=%s model=%s", ctx.room.name, sim_result_id, model)

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

        if greet == "say":
            # realtime models with mutable_chat_context=False reject generate_reply;
            # a TTS on the session lets say() deliver the scripted opener instead.
            session.say(GREETING)
        elif greet == "generate_reply":
            session.generate_reply(instructions=f"Greet the caller with: '{GREETING}'")

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
