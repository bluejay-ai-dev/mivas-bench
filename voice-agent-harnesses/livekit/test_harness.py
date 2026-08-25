"""Self-check: `python test_harness.py` (needs the harness venv on sys.path).

Covers the three things that silently break a whole run: the job-metadata parse
(wrong => no trace ever links to a simulation result), the blueprint load, and the
two-agent split (wrong => a "handoff" that is really one agent holding both prompts).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from livekit.agents import llm as lk_llm

import harness
from harness import (
    BlueprintAgent,
    load_blueprint,
    sip_uri,
    sim_result_id_from_job_metadata,
    sim_result_id_from_participant,
)


def test_sim_result_id() -> None:
    # what Bluejay actually sends: a JSON string on job.metadata
    assert (
        sim_result_id_from_job_metadata(
            json.dumps({"X-Simulation-Result-Id": "710922", "X-Agent-Id": "30519"})
        )
        == "710922"
    )
    assert sim_result_id_from_job_metadata({"simulation_result_id": 710922}) == "710922"
    for empty in (None, "", "{}", "not json", json.dumps({"X-Agent-Id": "1"}), "[]"):
        assert sim_result_id_from_job_metadata(empty) is None, empty
    sip = SimpleNamespace(
        attributes={"sip.h.x-simulation-result-id": "736069"},
        metadata="",
    )
    assert sim_result_id_from_participant(sip) == "736069"


def test_blueprint() -> None:
    bp = load_blueprint("control-industry")
    assert bp["start"] == "receptionist"
    assert set(bp["agents"]) == {"receptionist", "scheduler"}
    assert set(bp["catalog"]) == {"handoff_to_scheduler", "schedule_appointment", "end_call"}


def _tool_names(agent) -> set[str]:
    return set(lk_llm.ToolContext(agent.tools).function_tools)


def test_real_handoff() -> None:
    """The handoff must yield a *different* agent with the scheduler's own prompt and
    tools — the receptionist must not be able to book, and vice versa."""
    bp = load_blueprint("control-industry")
    hangup = asyncio.Event()

    models: dict[str, object] = {}  # stand-ins for per-agent RealtimeModel instances

    def llm_factory(name: str) -> object:
        return models.setdefault(name, object())

    receptionist = BlueprintAgent(
        bp, "receptionist", hangup, llm_factory=llm_factory, scripted_opener=True
    )
    assert _tool_names(receptionist) == {"handoff_to_scheduler", "end_call"}
    assert bp["agents"]["receptionist"]["instructions"] in receptionist.instructions
    assert harness.today_clock() in receptionist.instructions

    handoff = lk_llm.ToolContext(receptionist.tools).get_function_tool("handoff_to_scheduler")
    scheduler = asyncio.run(handoff(raw_arguments={}))
    assert isinstance(scheduler, BlueprintAgent)
    assert scheduler.agent_name == "scheduler"
    assert _tool_names(scheduler) == {"schedule_appointment", "end_call"}
    assert bp["agents"]["scheduler"]["instructions"] in scheduler.instructions
    assert harness.today_clock() in scheduler.instructions
    # scripted_opener propagates as a capability flag, but the actual spoken line is
    # derived from the *target's own* prompt, not inherited verbatim from the caller.
    assert scheduler._opener == "Hey, when do you want to schedule your repair appointment?"
    # each agent carries its own model => LiveKit cannot reuse the realtime session
    assert receptionist.llm is models["receptionist"]
    assert scheduler.llm is models["scheduler"]
    assert scheduler.llm is not receptionist.llm


def test_generic_industries() -> None:
    """Every shipped industry builds without per-tool harness handlers, and each
    agent only carries its own blueprint tools."""
    for industry in ("healthcare", "legal", "customer-support"):
        bp = load_blueprint(industry)
        hangup = asyncio.Event()
        start = BlueprintAgent(bp, bp["start"], hangup)
        expected = {t["name"] for t in bp["agents"][bp["start"]]["tools"]}
        assert _tool_names(start) == expected, (industry, _tool_names(start) ^ expected)


def test_pack_greeting_and_agent_name() -> None:
    bp = load_blueprint("healthcare")
    assert "Straus Dermatology" in bp["greeting"]
    assert harness.pack_greeting(bp).startswith("Thank you for calling Straus")
    assert harness.pack_greeting(load_blueprint("control-industry")) == harness.GREETING

    import os

    os.environ.pop("LIVEKIT_AGENT_NAME", None)
    os.environ.pop("MIVAS_SLUG", None)
    assert harness.resolve_agent_name("mivas-livekit-cascaded") == "mivas-livekit-cascaded"
    os.environ["MIVAS_SLUG"] = "livekit-cascaded-healthcare"
    assert harness.resolve_agent_name("mivas-livekit-cascaded") == (
        "mivas-livekit-cascaded-healthcare"
    )
    os.environ["LIVEKIT_AGENT_NAME"] = "explicit-name"
    assert harness.resolve_agent_name("mivas-livekit-cascaded") == "explicit-name"
    os.environ.pop("LIVEKIT_AGENT_NAME", None)
    os.environ.pop("MIVAS_SLUG", None)

    prev_host, prev_num = os.environ.pop("LIVEKIT_SIP_HOST", None), os.environ.pop(
        "LIVEKIT_SIP_NUMBER", None
    )
    try:
        assert sip_uri() is None
        os.environ["LIVEKIT_SIP_HOST"] = "example-project.sip.livekit.cloud"
        os.environ["LIVEKIT_SIP_NUMBER"] = "+15551230000"
        assert sip_uri() == "sip:+15551230000@example-project.sip.livekit.cloud"
    finally:
        os.environ.pop("LIVEKIT_SIP_HOST", None)
        os.environ.pop("LIVEKIT_SIP_NUMBER", None)
        if prev_host is not None:
            os.environ["LIVEKIT_SIP_HOST"] = prev_host
        if prev_num is not None:
            os.environ["LIVEKIT_SIP_NUMBER"] = prev_num


def test_await_farewell() -> None:
    """The hangup wait must outlast the agent's goodbye, but never run forever."""

    async def elapsed(start: str, states: list[tuple[float, str]], max_wait: float = 5.0) -> float:
        harness.HANGUP_QUIET_S, harness.HANGUP_MAX_WAIT_S = 0.3, max_wait
        loop = asyncio.get_running_loop()
        session = SimpleNamespace(agent_state=start)
        for at, state in states:
            loop.call_later(at, setattr, session, "agent_state", state)
        t0 = loop.time()
        await harness.await_farewell(session, asyncio.Event())
        return loop.time() - t0

    # nothing left to say -> exactly the quiet window, as the old flat sleep did
    assert 0.3 < asyncio.run(elapsed("listening", [])) < 0.6
    # quiet at the old sample point, then the farewell starts -> keep waiting
    assert 1.0 < asyncio.run(elapsed("listening", [(0.2, "thinking"), (1.0, "listening")])) < 1.6
    # still speaking after the quiet window -> wait for it to stop
    assert 0.7 < asyncio.run(elapsed("speaking", [(0.6, "listening")])) < 1.2
    # a model that never stops talking is cut off at the cap
    assert 0.5 < asyncio.run(elapsed("speaking", [], max_wait=0.6)) < 1.0


def test_usage_stamped_on_root() -> None:
    """LLM tokens + STT/TTS seconds accumulate per call and land on voice.call."""
    import report

    attrs: dict[str, object] = {}
    root = SimpleNamespace(
        get_span_context=lambda: SimpleNamespace(is_valid=True, span_id=42),
        set_attribute=lambda k, v: attrs.__setitem__(k, v),
    )
    # dispatch is by class name, exactly as livekit-agents names these types
    def metric(cls_name: str, **fields: float) -> object:
        return type(cls_name, (), fields)()

    report._active_root = root
    try:
        report.record_usage(
            metric("LLMMetrics", prompt_tokens=1200, completion_tokens=80, prompt_cached_tokens=1024)
        )
        report.record_usage(
            metric("LLMMetrics", prompt_tokens=1500, completion_tokens=40, prompt_cached_tokens=0)
        )
        report.record_usage(metric("STTMetrics", audio_duration=12.5))
        report.record_usage(metric("TTSMetrics", audio_duration=7.25, characters_count=310))
        # EOU latency is not usage and must not create attributes
        report.record_usage(metric("EOUMetrics", end_of_utterance_delay=0.4))

        report._stamp_usage(root)
    finally:
        report._active_root = None

    assert attrs["gen_ai.usage.input_tokens"] == 2700, attrs
    assert attrs["gen_ai.usage.input_tokens_text"] == 2700
    assert attrs["gen_ai.usage.output_tokens"] == 120
    assert attrs["gen_ai.usage.cached_tokens"] == 1024
    assert attrs["gen_ai.usage.total_tokens"] == 2820
    assert attrs["mivas.stt.audio_duration_s"] == 12.5
    assert attrs["mivas.tts.audio_duration_s"] == 7.25
    assert attrs["mivas.tts.characters"] == 310
    assert "mivas.audio.duration_s" in attrs
    # the bucket is dropped with the call, so a second call cannot inherit it
    assert 42 not in report._usage


if __name__ == "__main__":
    test_sim_result_id()
    test_blueprint()
    test_real_handoff()
    test_generic_industries()
    test_pack_greeting_and_agent_name()
    test_await_farewell()
    test_usage_stamped_on_root()
    print("ok")
