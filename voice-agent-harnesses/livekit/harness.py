"""control-industry blueprint → LiveKit Agents, shared by all three runtimes.

LiveKit runs our code, so unlike the Vapi/Retell/Bland/Cartesia harnesses there is
no tool webhook and no tunnel: every tool body runs in this process and is wrapped
directly in an `execute_tool` span. Multi-agent handoff is in-framework — the
receptionist's `handoff_to_scheduler` returns the `Scheduler` agent instance — so
the handoff is a real, timed tool call rather than a provider-internal jump.

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
    RunContext,
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
# rejects generate_reply() (see Scheduler.on_enter); every other turn is model-generated.
SCHEDULER_OPENER = "Hey, when do you want to schedule your repair appointment?"
# How long the agent must be *quiet* after `end_call` before we tear the room down.
# This used to be a flat sleep, which was enough for cascaded/Gemini (they finish the
# farewell and the caller hangs up first) but not for gpt-realtime-2.1: it calls
# `end_call` in the same response turn as `schedule_appointment` (2.6-2.9 s apart)
# while still speaking, so a fixed 4 s deleted the room mid-sentence — the transcript
# cut at "scheduled for eighteighteentwenty" (713478/713612) and at "Your repair
# appointment is scheduled for" (713652). The digital human never heard a goodbye,
# never hung up, and the run burned the full 180 s cap. The timer now *restarts*
# whenever the agent is speaking or generating, so it measures 4 s of silence rather
# than 4 s of wall clock; a model with nothing left to say still waits exactly 4 s.
HANGUP_QUIET_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "4.0"))
# hard cap so a model that never stops talking cannot hold the room forever
HANGUP_MAX_WAIT_S = float(os.environ.get("MIVAS_END_CALL_MAX_WAIT_S", "20.0"))
# States where the agent still owes us audio. "thinking" is the gap between the
# end_call tool result and the farewell audio starting — exactly where a check that
# only looked for "speaking" fires early (713652 cut 1.7 s into that gap).
AGENT_BUSY_STATES = ("thinking", "speaking")


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


async def _execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "schedule_appointment":
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{TOOL_SERVER_URL}/appointments", json={"date": args["date"]})
            r.raise_for_status()
            return {"success": True, "date": r.json()["date"]}
    if name in ("handoff_to_scheduler", "end_call"):
        # harness-local: no industry state to mutate, the span is the artifact
        return {"success": True}
    return {"success": False, "error": f"unknown tool {name}"}


async def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one blueprint tool under an `execute_tool` span. Never raises."""
    offset = report.call_offset_ms()
    with report.tool_span(name, args) as span:
        try:
            result = await _execute(name, args)
            ok = True
        except Exception as e:  # soft-fail: the call (and its trace) must still finish
            result, ok = {"success": False, "error": f"{type(e).__name__}: {e}"}, False
        report.finish_tool_span(span, result, ok=ok)
    logger.info("tool %s args=%s -> %s (+%dms)", name, args, result, offset)
    return result


async def _end_call(reason: str, hangup: asyncio.Event) -> dict[str, Any]:
    result = await run_tool("end_call", {"reason": reason})
    hangup.set()
    return result


# ── agents ───────────────────────────────────────────────────────────────────


class Scheduler(Agent):
    """Books the appointment. Reached by handoff from the receptionist.

    `llm` is the per-agent model override. Passing a distinct instance is what makes
    the handoff a real switch on models that cannot be mutated mid-session: LiveKit
    only carries the realtime session across a handoff when the two activities share
    the *same* model object, so a fresh instance means a fresh provider session
    configured with this agent's prompt and this agent's tools.
    """

    def __init__(
        self,
        bp: dict[str, Any],
        hangup: asyncio.Event,
        *,
        llm: NotGivenOr[Any] = NOT_GIVEN,
        opener: str | None = None,
    ):
        super().__init__(instructions=bp["agents"]["scheduler"]["instructions"], llm=llm)
        self._hangup = hangup
        self._opener = opener

    async def on_enter(self) -> None:
        # generate_reply() is rejected by realtime models with mutable_chat_context=False
        # (google plugin, any "3.1" model); those runtimes pass `opener` and the session
        # TTS speaks the scheduler's scripted first line instead.
        if self._opener:
            self.session.say(self._opener)
        else:
            self.session.generate_reply()

    @function_tool
    async def schedule_appointment(self, context: RunContext, date: str) -> dict[str, Any]:
        """Schedule a repair appointment and store it in the database.

        Args:
            date: Repair appointment date in MM/DD/YYYY format
        """
        return await run_tool("schedule_appointment", {"date": date})

    @function_tool
    async def end_call(self, context: RunContext, reason: str) -> dict[str, Any]:
        """End the call once the caller is done, or immediately if it is spam or a
        wrong number. Say goodbye first.

        Args:
            reason: Why the call is ending
        """
        return await _end_call(reason, self._hangup)


class Receptionist(Agent):
    """Greets, then hands off in-framework by returning the Scheduler instance."""

    def __init__(
        self,
        bp: dict[str, Any],
        hangup: asyncio.Event,
        *,
        llm: NotGivenOr[Any] = NOT_GIVEN,
        make_scheduler: Callable[[], Agent] | None = None,
    ):
        super().__init__(instructions=bp["agents"]["receptionist"]["instructions"], llm=llm)
        self._hangup = hangup
        # runtimes whose model can't be mutated mid-session pass a factory that gives the
        # Scheduler its own model instance; everyone else shares the session's.
        self._make_scheduler = make_scheduler or (lambda: Scheduler(bp, hangup))

    @function_tool
    async def handoff_to_scheduler(self, context: RunContext) -> Agent:
        """Hand off the caller to the Bluejay's Repair Services scheduler agent."""
        await run_tool("handoff_to_scheduler", {})
        # return the Agent and nothing else: any non-Agent output sets
        # reply_required, which makes the receptionist announce the transfer
        # ("I'll connect you with our scheduler") on top of the scheduler's own
        # opener. The blueprint forbids that, and the two overlapping turns let the
        # caller's "okay, thank you" land on the scheduler as "I'm done" -> end_call
        # before anything is booked.
        return self._make_scheduler()

    @function_tool
    async def end_call(self, context: RunContext, reason: str) -> dict[str, Any]:
        """End the call once the caller is done, or immediately if it is spam or a
        wrong number. Say goodbye first.

        Args:
            reason: Why the call is ending
        """
        return await _end_call(reason, self._hangup)


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

    `AgentSession.agent_state` is the same signal `wire_speech_spans` brackets
    `agent.speech` on, polled rather than subscribed: the farewell can start at any
    point in this window and an edge-triggered wait would miss the transition. The
    quiet timer restarts on every busy sample, so a pause between the confirmation
    and the goodbye does not count as "done talking".
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + HANGUP_MAX_WAIT_S
    quiet_since = loop.time()
    while not disconnected.is_set() and loop.time() < deadline:
        if session.agent_state in AGENT_BUSY_STATES:
            quiet_since = loop.time()
        elif loop.time() - quiet_since >= HANGUP_QUIET_S:
            break
        await asyncio.sleep(0.2)
    logger.info(
        "farewell wait done after %.1fs (state=%s)",
        loop.time() - (deadline - HANGUP_MAX_WAIT_S),
        session.agent_state,
    )


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
