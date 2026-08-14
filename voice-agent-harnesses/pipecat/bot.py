"""Pipecat Cloud entrypoint for the MIVAS control-industry agent.

One deployed Pipecat Cloud service serves all three runtimes; which one runs is
read from the start `body`, i.e. from the Bluejay agent's
`pipecat_agent_configuration`:

    {"runtime": "cascaded" | "openai-realtime-2.1" | "gemini-flash-live-3.1",
     "tool_server_url": "https://<tunnel>",
     "simulation_id": 30xxx}

`simulation_id` is there because Bluejay's Pipecat dispatch passes no per-run
metadata — `report.resolve_simulation_result_id` turns it into the live
simulation_result_id.

The receptionist → scheduler handoff is a real agent switch in all three
runtimes; see `harness` for which Pipecat mechanism each one uses and why.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndTaskFrame,
    Frame,
    FunctionCallResultProperties,
    LLMRunFrame,
    ManuallySwitchServiceFrame,
    TextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.filters.function_filter import FunctionFilter
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.runner.utils import create_transport
from pipecat.transports.daily.transport import DailyParams

import harness
import report

INDUSTRY = os.environ.get("INDUSTRY", "control-industry")


async def _not_text_frame(frame: Frame) -> bool:
    """Gate for the greeting-only TTS: everything except text it would re-speak."""
    return not isinstance(frame, TextFrame)


def _not_greeting_text_filter(greeting: str):
    """Keep the injected opener out of the LLM context.

    The greeting TTS emits a TTSTextFrame with `append_to_context` left true, so
    `LLMAssistantAggregator` appends it as an assistant message before the caller
    has said anything — and Gemini Live then answers nothing and closes the socket
    with 1008. Gemini's own speech frames must still reach the aggregator exactly
    as they did before the TTS existed, so match only the line we injected.
    """

    async def _not_greeting_text(frame: Frame) -> bool:
        return not (isinstance(frame, TextFrame) and frame.text.strip() == greeting)

    return _not_greeting_text


# `end_call` must not tear the pipeline down the instant the tool returns. A
# cascaded LLM has already generated its farewell by then, so the EndFrame drain
# plays it out; a realtime model only generates that audio *after* it sees the
# tool result, so an immediate EndTaskFrame cuts it off mid-thought and the caller
# hears silence (pipecat 711945 vs livekit 710923, same gpt-realtime-2.1). LiveKit
# solves this with a flat `HANGUP_GRACE_S = 4.0`; here we can do better and wait
# for the farewell to actually finish, with the flat delay as the lead-in.
END_CALL_LEAD_IN_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "4.0"))
END_CALL_MAX_WAIT_S = float(os.environ.get("MIVAS_END_CALL_MAX_WAIT_S", "20.0"))


async def _await_farewell(observer: "SpeechSpanObserver") -> None:
    """Give the model room to say goodbye, then wait for it to stop speaking."""
    await asyncio.sleep(END_CALL_LEAD_IN_S)
    deadline = time.monotonic() + END_CALL_MAX_WAIT_S
    # ponytail: 200 ms poll on an asyncio.Event, not an edge-triggered wait —
    # the farewell may start during the lead-in, and polling cannot miss that.
    while not observer.agent_idle.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.2)


class SpeechSpanObserver(BaseObserver):
    """Bracket agent.speech / customer.speech on real turn frames.

    Frames are pushed by several processors, so each start/stop pair is
    deduplicated on frame id and by "a span is already open".
    """

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[int] = set()
        self._spans: dict[str, object] = {}
        self._n = 0
        # set whenever the agent is not mid-utterance; `_await_farewell` waits on it
        self.agent_idle = asyncio.Event()
        self.agent_idle.set()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if isinstance(
            frame,
            (
                BotStartedSpeakingFrame,
                BotStoppedSpeakingFrame,
                UserStartedSpeakingFrame,
                UserStoppedSpeakingFrame,
            ),
        ):
            if frame.id in self._seen:
                return
            self._seen.add(frame.id)
        else:
            return

        if isinstance(frame, (BotStartedSpeakingFrame, UserStartedSpeakingFrame)):
            speaker = "agent" if isinstance(frame, BotStartedSpeakingFrame) else "customer"
            if speaker == "agent":
                self.agent_idle.clear()
            if self._spans.get(speaker) is not None:
                return
            self._n += 1
            self._spans[speaker] = report.start_speech_span(
                f"{speaker}-{self._n}", speaker=speaker
            )
        else:
            speaker = "agent" if isinstance(frame, BotStoppedSpeakingFrame) else "customer"
            if speaker == "agent":
                self.agent_idle.set()
            report.end_speech_span(self._spans.pop(speaker, None))

    def close(self) -> None:
        for span in self._spans.values():
            report.end_speech_span(span)
        self._spans.clear()


async def run_bot(transport, runtime: str) -> None:
    bp = harness.load_blueprint(INDUSTRY)
    state = {"agent": bp["start"]}
    s2s = runtime in harness.S2S_RUNTIMES

    observer = SpeechSpanObserver()
    worker: PipelineWorker | None = None
    llm = None            # the pipeline's LLM stage: one service, or an LLMSwitcher
    agent_llms: dict = {}  # S2S only: one model session per blueprint agent
    ending = False
    end_task: asyncio.Task | None = None

    async def _end_call() -> None:
        await _await_farewell(observer)
        # Push from the service that is actually speaking; behind a switcher the
        # inactive branch's filter would swallow it.
        src = llm.active_llm if isinstance(llm, LLMSwitcher) else llm
        await src.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    def schedule_end(stop: bool) -> None:
        """Hang up once the farewell has played.

        The wait runs off to the side rather than inside the tool handler: Flows
        does not deliver a function result until its handler returns, so blocking
        here would stop the model ever generating the farewell we are waiting for.

        The wait also leaves the model free to call `end_call` again while we hold
        the pipeline open; honour only the first one (run 712222 hung up twice,
        61915 ms and 83421 ms, and ran 90 s instead of ~70 s).
        """
        nonlocal ending, end_task
        if not stop or ending:
            return
        ending = True
        end_task = asyncio.create_task(_end_call())  # noqa: RUF006 — held on `end_task`

    async def cancel_end_task() -> None:
        nonlocal end_task
        if end_task is not None and not end_task.done():
            end_task.cancel()
            await asyncio.gather(end_task, return_exceptions=True)
        end_task = None

    if s2s:
        # Each blueprint agent is its own speech-to-speech session, opened with
        # that agent's prompt and that agent's tools. Handing off swaps which
        # session the call is wired to — the receptionist's session is never told
        # `schedule_appointment` exists.
        async def on_tool(params) -> None:
            result, stop = await harness.run_tool(
                params.function_name,
                dict(params.arguments or {}),
                bp,
                state,
                call_id=params.tool_call_id,
            )
            target = result.get("role")
            if target not in agent_llms:
                await params.result_callback(result)
                schedule_end(stop)
                return

            async def _switch() -> None:
                logger.info(
                    "handoff → {} ({} session, tools={})",
                    target, harness.RUNTIMES[runtime], harness.tool_names(bp, target),
                )
                frames = [
                    ManuallySwitchServiceFrame(service=agent_llms[target]),
                    LLMRunFrame(),
                ]
                # Gemini Live will not speak until the caller does, so a handoff
                # target needs a scripted first line — derived from *that*
                # agent's prompt, never a leftover receptionist greeting.
                if runtime in harness.GREETING_TTS_RUNTIMES:
                    frames.append(TTSSpeakFrame(harness.agent_opener(bp, target)))
                await worker.queue_frames(frames)

            # Switch from `on_context_updated`, not inline: the handoff call and
            # its result have to be in the shared context *before* the incoming
            # agent first sees that context. Otherwise the result lands after,
            # looks newly-completed to a session that never made the call, and
            # OpenAI Realtime kills it with
            # `invalid_tool_call_id: Tool call ID '...' not found in conversation`
            # — which takes the receive loop down with it (run 712652).
            # run_llm=False also keeps the outgoing agent from answering a tool
            # result meant for its replacement.
            await params.result_callback(
                result,
                properties=FunctionCallResultProperties(
                    run_llm=False, on_context_updated=_switch
                ),
            )
            schedule_end(stop)

        agent_llms = harness.build_agent_llms(runtime, bp, on_tool)
        # Order matters: ServiceSwitcher starts on services[0], the receptionist.
        llm = LLMSwitcher(llms=list(agent_llms.values()))
    else:
        # Cascaded is a text LLM with function calling, which is exactly what
        # Pipecat Flows is for. One node per blueprint agent, each with its own
        # task_messages and its own functions; the consolidated handler returns
        # (result, next_node) and FlowManager swaps context and advertised tools.
        async def on_tool(name, args, _flow_manager):
            result, stop = await harness.run_tool(name, dict(args or {}), bp, state)
            schedule_end(stop)
            target = result.get("role")
            if target in bp["agents"]:
                logger.info(
                    "handoff → {} node (tools={})", target, harness.tool_names(bp, target)
                )
                return result, harness.flows_node(bp, target, on_tool)
            return result, None

        llm = harness.build_llm(runtime, "", None)

    stt, tts = harness.build_stt_tts(runtime)

    # No system message and no tools on the shared context: prompts and tool sets
    # are per-agent, carried by the Flows node or by the S2S session itself. A
    # context tool set would override the S2S services' own (see
    # `OpenAIRealtimeLLMService._send_session_update`) and hand the receptionist
    # the scheduler's tools.
    context = LLMContext()
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )

    stages = [transport.input()]
    if stt:
        stages.append(stt)
    stages.append(aggregators.user())
    stages.append(llm)
    if tts:
        stages.append(tts)
        stages += [transport.output(), aggregators.assistant()]
    elif runtime in harness.GREETING_TTS_RUNTIMES:
        # Greeting-only TTS, gated on both sides. Ahead of it: the S2S model
        # narrates its own speech as LLMTextFrame + TTSTextFrame and this TTS would
        # happily say all of it a second time (TTSSpeakFrame is not a TextFrame, so
        # the opener still gets through). Behind it: the opener's own text frame
        # must not reach the assistant aggregator, or it lands in the context as an
        # assistant message before the caller has spoken and Gemini drops the
        # session with 1008.
        greeting = harness.pack_greeting(bp)
        stages.append(FunctionFilter(_not_text_frame))
        stages.append(harness.build_tts())
        stages += [
            transport.output(),
            FunctionFilter(_not_greeting_text_filter(greeting)),
            aggregators.assistant(),
        ]
    else:
        stages += [transport.output(), aggregators.assistant()]

    worker = PipelineWorker(
        Pipeline(stages),
        params=PipelineParams(enable_metrics=True),
        # observers is a PipelineWorker kwarg — PipelineParams silently drops it,
        # which is how the speech spans went missing from the first proof runs.
        observers=[observer],
    )

    flow_manager = None
    if not s2s:
        from pipecat.flows import FlowManager

        flow_manager = FlowManager(
            worker=worker, llm=llm, context_aggregator=aggregators, transport=transport
        )

    async def _kickoff(_transport, *_args):
        logger.info("client connected — kicking off greeting")
        if flow_manager is not None:
            # initialize() sets the receptionist node — prompt, tools, LLMRunFrame.
            await flow_manager.initialize(harness.flows_node(bp, bp["start"], on_tool))
            return
        # Every S2S runtime still gets the LLMRunFrame: Gemini will not speak first
        # regardless, but the frame is what primes its turn handling — without it
        # the service answers no user turn at all and the socket eventually closes
        # with 1008. The scripted opener rides behind it on the greeting TTS.
        frames = [LLMRunFrame()]
        if runtime in harness.GREETING_TTS_RUNTIMES:
            frames.append(TTSSpeakFrame(harness.pack_greeting(bp)))
        await worker.queue_frames(frames)

    async def _hangup(_transport, *_args):
        logger.info("client disconnected")
        await cancel_end_task()
        await worker.cancel()

    # Daily Cloud uses on_client_*; LiveKitTransport uses participant/room events.
    if transport.__class__.__name__.startswith("Daily"):
        transport.event_handler("on_client_connected")(_kickoff)
        transport.event_handler("on_client_disconnected")(_hangup)
    else:
        transport.event_handler("on_first_participant_joined")(_kickoff)
        transport.event_handler("on_participant_disconnected")(_hangup)
        transport.event_handler("on_disconnected")(_hangup)

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
        await cancel_end_task()
        observer.close()


async def bot(args) -> None:
    """Pipecat Cloud entrypoint."""
    body = dict(getattr(args, "body", None) or {})
    runtime = body.get("runtime") or harness.DEFAULT_RUNTIME
    if runtime not in harness.RUNTIMES:
        logger.warning("unknown runtime %r — falling back to %s", runtime, harness.DEFAULT_RUNTIME)
        runtime = harness.DEFAULT_RUNTIME
    if body.get("tool_server_url"):
        os.environ["TOOL_SERVER_URL"] = str(body["tool_server_url"])

    sim_result_id = await report.resolve_simulation_result_id(body.get("simulation_id"))
    harness.set_call_id(sim_result_id)
    harness.begin_session(sim_result_id, session_key=str(sim_result_id or "job"))
    logger.info(
        "pipecat bot runtime={} model={} sim_result_id={} tool_server={}",
        runtime, harness.RUNTIMES[runtime], sim_result_id, harness.tool_server_url(),
    )

    transport = await create_transport(
        args,
        {
            "daily": lambda: DailyParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                vad_analyzer=SileroVADAnalyzer(),
            ),
        },
    )

    async with report.traced_run(
        f"mivas-{INDUSTRY}-{runtime}",
        simulation_result_id=sim_result_id,
        model=harness.RUNTIMES[runtime],
    ):
        try:
            await run_bot(transport, runtime)
        finally:
            # end_session freezes the call DB to S3; it does blocking HTTP.
            await asyncio.to_thread(
                harness.end_session, str(sim_result_id or "job")
            )


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
