"""Offline construction check for one runtime: blueprint → services → pipeline.

Needs the runtime's API keys in the environment (nothing is dialled — the
services only open sockets once the pipeline starts).
"""

from __future__ import annotations

import harness


def check_greeting_gate() -> None:
    """The greeting-only TTS must say the opener and nothing the S2S model says."""
    import asyncio

    from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame, TTSTextFrame
    from pipecat.utils.text.base_text_aggregator import AggregationType

    from bot import _not_greeting_text, _not_text_frame

    def tts_text(s):
        return TTSTextFrame(s, aggregated_by=AggregationType.SENTENCE)

    # ahead of the TTS: the opener passes, the model's own text does not
    assert asyncio.run(_not_text_frame(TTSSpeakFrame(harness.GREETING))) is True
    assert asyncio.run(_not_text_frame(LLMTextFrame("hi"))) is False
    assert asyncio.run(_not_text_frame(tts_text("hi"))) is False

    # behind it: the opener is kept out of the context, everything else gets through
    assert asyncio.run(_not_greeting_text(tts_text(harness.GREETING))) is False
    assert asyncio.run(_not_greeting_text(tts_text(f" {harness.GREETING} "))) is False
    assert asyncio.run(_not_greeting_text(tts_text("How can I help?"))) is True
    assert asyncio.run(_not_greeting_text(LLMTextFrame("How can I help?"))) is True
    print("greeting gate ok")


def check_runtime(runtime: str, industry: str = "control-industry") -> None:
    from pipecat.pipeline.pipeline import Pipeline

    bp = harness.load_blueprint(industry)
    assert runtime in harness.RUNTIMES, runtime

    async def _noop(params):  # pragma: no cover - never called here
        raise AssertionError("tool handler must not run during the build check")

    tools = harness.function_schemas(bp, _noop)
    assert [t.name for t in tools] == harness.tool_names(bp)

    llm = harness.build_llm(runtime, harness.system_prompt(bp), tools)
    stt, tts = harness.build_stt_tts(runtime)
    assert (stt is None) == (runtime != "cascaded")

    stages = [s for s in (stt, llm, tts) if s is not None]
    Pipeline(stages)
    print(f"{runtime}: model={harness.RUNTIMES[runtime]} "
          f"stages={[type(s).__name__ for s in stages]} tools={[t.name for t in tools]} ok")
