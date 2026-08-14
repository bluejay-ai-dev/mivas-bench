"""Pipecat pipeline: Nemotron ASR → LLM → Magpie TTS with Flows multi-agent.

Used by the CHIRP adapter (FastAPI websocket) and by `nemotron/agent.py --check`.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndTaskFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
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
from pipecat.transports.base_transport import BaseTransport

import harness
import report

END_CALL_LEAD_IN_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "4.0"))
END_CALL_MAX_WAIT_S = float(os.environ.get("MIVAS_END_CALL_MAX_WAIT_S", "20.0"))


async def _await_farewell(observer: "SpeechSpanObserver") -> None:
    await asyncio.sleep(END_CALL_LEAD_IN_S)
    deadline = time.monotonic() + END_CALL_MAX_WAIT_S
    while not observer.agent_idle.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.2)


class SpeechSpanObserver(BaseObserver):
    """Bracket agent.speech / customer.speech on real turn frames."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[int] = set()
        self._spans: dict[str, object] = {}
        self._n = 0
        self.parent = None
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
                f"{speaker}-{self._n}", speaker=speaker, parent=self.parent
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


async def run_bot(
    transport: BaseTransport,
    industry: str,
    *,
    simulation_result_id: str | None = None,
) -> None:
    harness.install_io_executor()
    harness.set_call_id(simulation_result_id)
    harness.begin_session(simulation_result_id, session_key=str(simulation_result_id or "job"))
    bp = harness.load_blueprint(industry)
    state: dict[str, Any] = {"agent": bp["start"]}
    observer = SpeechSpanObserver()
    ending = False
    end_task: asyncio.Task | None = None

    stt = harness.build_stt()
    llm = harness.build_llm()
    tts = harness.build_tts()

    context = LLMContext()
    vad = await asyncio.to_thread(SileroVADAnalyzer)
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=vad)
    )

    worker = PipelineWorker(
        Pipeline(
            [
                transport.input(),
                stt,
                aggregators.user(),
                llm,
                tts,
                transport.output(),
                aggregators.assistant(),
            ]
        ),
        params=PipelineParams(enable_metrics=True),
        observers=[observer],
    )

    async def _end_call() -> None:
        await _await_farewell(observer)
        await llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    def schedule_end(stop: bool) -> None:
        nonlocal ending, end_task
        if not stop or ending:
            return
        ending = True
        end_task = asyncio.create_task(_end_call())

    async def cancel_end_task() -> None:
        nonlocal end_task
        if end_task is not None and not end_task.done():
            end_task.cancel()
            await asyncio.gather(end_task, return_exceptions=True)
        end_task = None

    async def on_tool(name, args, _flow_manager):
        result, stop = await harness.run_tool(
            name, dict(args or {}), bp, state, call_id=simulation_result_id
        )
        schedule_end(stop)
        target = result.get("role")
        if target in bp["agents"]:
            logger.info(
                "handoff → {} node (tools={})",
                target,
                harness.tool_names(bp, target),
            )
            # Pack greeting already played. RESET + respond_immediately made the
            # specialist cold-open ("Hello. I am ROBIN…") on Gloria 727614.
            return result, harness.flows_node(
                bp, target, on_tool, respond_immediately=False
            )
        return result, None

    from pipecat.flows import FlowManager

    flow_manager = FlowManager(
        worker=worker, llm=llm, context_aggregator=aggregators, transport=transport
    )

    async def _speak_pack_greeting(text: str) -> None:
        try:
            ready = getattr(tts, "ready", None)
            if ready is not None:
                await ready.wait()
            logger.info("speaking pack greeting ({} chars)", len(text))
            await tts.queue_frame(TTSSpeakFrame(text))
        except Exception:
            logger.exception("pack greeting failed")

    async def _init_start_node() -> None:
        try:
            # Pack greeting already plays via TTSSpeakFrame. An opening LLMRun
            # (Flows default) fired 6 NIM completions at once; #1 then timed
            # out and never answered the caller (run 230706, 726209).
            has_greeting = bool((bp.get("greeting") or "").strip())
            logger.info(
                "client connected — initializing {} node (respond_immediately={})",
                bp["start"],
                not has_greeting,
            )
            await flow_manager.initialize(
                harness.flows_node(
                    bp,
                    bp["start"],
                    on_tool,
                    respond_immediately=not has_greeting,
                )
            )
        except Exception:
            logger.exception("flow initialize failed")

    @transport.event_handler("on_client_connected")
    async def _connected(_transport, _client):
        # Do not await initialize() here. Flows' opening LLMRun blocks until the
        # LLM returns; six concurrent pipelines starved that call and the
        # greeting never left (runs 230627 / 230659, all NO_ANSWER). Speak the
        # pack opener as soon as TTS is up; initialize in the background.
        greeting = (bp.get("greeting") or "").strip()
        if greeting:
            asyncio.create_task(_speak_pack_greeting(greeting))
        asyncio.create_task(_init_start_node())

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_transport, _client):
        logger.info("client disconnected")
        await cancel_end_task()
        await worker.cancel()

    name = f"mivas-{harness.industry_path(industry).name}-{harness.RUNTIME}"
    async with report.traced_run(
        name,
        simulation_result_id=simulation_result_id,
        model=harness.MODEL,
    ) as otel_root:
        # Pipecat runs tool handlers on a different task than this block, so
        # ContextVar / process-global _active_root is the last call on the pod.
        # Pin the per-call root on state (and the speech observer) the same way
        # nemotron-voicechat does.
        state["_otel_root"] = otel_root
        observer.parent = otel_root
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        try:
            await runner.run()
        finally:
            await cancel_end_task()
            observer.close()
            # end_session freezes the call DB to S3; it does blocking HTTP.
            await asyncio.to_thread(
                harness.end_session, str(simulation_result_id or "job")
            )


def check_pipeline(industry: str = "control-industry") -> None:
    """Offline construction check — no sockets opened."""
    bp = harness.load_blueprint(industry)
    assert bp["start"] == "receptionist" or industry != "control-industry"

    async def _noop(*_a, **_k):  # pragma: no cover
        raise AssertionError("tool handler must not run during build check")

    for agent in bp["agents"]:
        node = harness.flows_node(bp, agent, _noop)
        assert node["name"] == agent
        assert [f.name for f in node["functions"]] == harness.tool_names(bp, agent)
        pack = harness.instructions(bp, agent)
        content = node["task_messages"][0]["content"]
        assert content.startswith(pack)
        assert "<AVAILABLE_TOOLS>" in content

    if industry == "control-industry" or industry.endswith("control-industry"):
        assert harness.tool_names(bp, "receptionist") == [
            "handoff_to_scheduler",
            "end_call",
        ]
        assert "schedule_appointment" not in harness.tool_names(bp, "receptionist")

    # Service construction needs NVIDIA_API_KEY but does not dial until StartFrame.
    if os.environ.get("NVIDIA_API_KEY"):
        stt = harness.build_stt()
        llm = harness.build_llm()
        tts = harness.build_tts()
        Pipeline([stt, llm, tts])
        print(
            f"ok {harness.industry_path(industry).name} × {harness.RUNTIME} "
            f"model={harness.MODEL} agents={list(bp['agents'])} "
            f"stages={[type(s).__name__ for s in (stt, llm, tts)]}"
        )
    else:
        print(
            f"ok {harness.industry_path(industry).name} × {harness.RUNTIME} "
            f"agents={list(bp['agents'])} (skip Nvidia service build — no NVIDIA_API_KEY)"
        )
