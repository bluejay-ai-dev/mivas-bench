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
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndTaskFrame,
    Frame,
    LLMRunFrame,
    TextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.filters.function_filter import FunctionFilter
from pipecat.observers.base_observer import BaseObserver, FramePushed
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


async def _not_greeting_text(frame: Frame) -> bool:
    """Keep the injected opener out of the LLM context.

    The greeting TTS emits a TTSTextFrame with `append_to_context` left true, so
    `LLMAssistantAggregator` appends it as an assistant message before the caller
    has said anything — and Gemini Live then answers nothing and closes the socket
    with 1008. Gemini's own speech frames must still reach the aggregator exactly
    as they did before the TTS existed, so match only the line we injected.

    ponytail: exact-text match, fine for a fixed one-sentence opener; revisit if
    the greeting ever becomes multi-sentence and the TTS splits it.
    """
    return not (
        isinstance(frame, TextFrame) and frame.text.strip() == harness.GREETING
    )


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
    instructions = harness.system_prompt(bp)

    ending = False

    async def on_tool(params) -> None:
        nonlocal ending
        result, stop = await harness.run_tool(
            params.function_name,
            dict(params.arguments or {}),
            bp,
            state,
            call_id=params.tool_call_id,
        )
        await params.result_callback(result)
        # The farewell wait leaves the model free to call `end_call` again while we
        # hold the pipeline open; honour only the first one (run 712222 hung up twice,
        # 61915 ms and 83421 ms, and ran 90 s instead of ~70 s).
        if stop and not ending:
            ending = True
            await _await_farewell(observer)
            await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    tools = harness.function_schemas(bp, on_tool)
    llm = harness.build_llm(runtime, instructions, tools)
    stt, tts = harness.build_stt_tts(runtime)

    # Gemini Live takes the prompt on the constructor; the others take it in context.
    messages = (
        [] if runtime == "gemini-flash-live-3.1"
        else [{"role": "system", "content": instructions}]
    )
    context = LLMContext(messages=messages, tools=tools)
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
        stages.append(FunctionFilter(_not_text_frame))
        stages.append(harness.build_tts())
        stages += [
            transport.output(),
            FunctionFilter(_not_greeting_text),
            aggregators.assistant(),
        ]
    else:
        stages += [transport.output(), aggregators.assistant()]

    observer = SpeechSpanObserver()
    worker = PipelineWorker(
        Pipeline(stages),
        params=PipelineParams(enable_metrics=True),
        # observers is a PipelineWorker kwarg — PipelineParams silently drops it,
        # which is how the speech spans went missing from the first proof runs.
        observers=[observer],
    )

    @transport.event_handler("on_client_connected")
    async def _connected(_transport, _client):
        logger.info("client connected — kicking off greeting")
        # Every runtime still gets the LLMRunFrame: Gemini will not speak first
        # regardless, but the frame is what primes its turn handling — without it
        # the service answers no user turn at all and the socket eventually closes
        # with 1008. The scripted opener rides behind it on the greeting TTS.
        frames = [LLMRunFrame()]
        if runtime in harness.GREETING_TTS_RUNTIMES:
            frames.append(TTSSpeakFrame(harness.GREETING))
        await worker.queue_frames(frames)

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_transport, _client):
        logger.info("client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
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
        await run_bot(transport, runtime)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
