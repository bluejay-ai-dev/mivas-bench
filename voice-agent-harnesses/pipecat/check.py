"""Offline construction check for one runtime: blueprint → services → pipeline.

Also the proof that the handoff is a real agent switch and not a prompt trick:
for every runtime it asserts the receptionist's model is never given
`schedule_appointment`, at the level the model actually sees (the S2S session's
own tool set, or the Flows node's `functions`).

Needs the runtime's API keys in the environment (nothing is dialled — the
services only open sockets once the pipeline starts).
"""

from __future__ import annotations

import harness

EXPECTED = {
    "receptionist": ["handoff_to_scheduler", "end_call"],
    "scheduler": ["schedule_appointment", "end_call"],
}


def check_greeting_gate() -> None:
    """The greeting-only TTS must say the opener and nothing the S2S model says."""
    import asyncio

    from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame, TTSTextFrame
    from pipecat.utils.text.base_text_aggregator import AggregationType

    from bot import _not_greeting_text_filter, _not_text_frame

    def tts_text(s):
        return TTSTextFrame(s, aggregated_by=AggregationType.SENTENCE)

    greeting = harness.GREETING
    _not_greeting_text = _not_greeting_text_filter(greeting)

    # ahead of the TTS: the opener passes, the model's own text does not
    assert asyncio.run(_not_text_frame(TTSSpeakFrame(greeting))) is True
    assert asyncio.run(_not_text_frame(LLMTextFrame("hi"))) is False
    assert asyncio.run(_not_text_frame(tts_text("hi"))) is False

    # behind it: the opener is kept out of the context, everything else gets through
    assert asyncio.run(_not_greeting_text(tts_text(greeting))) is False
    assert asyncio.run(_not_greeting_text(tts_text(f" {greeting} "))) is False
    assert asyncio.run(_not_greeting_text(tts_text("How can I help?"))) is True
    assert asyncio.run(_not_greeting_text(LLMTextFrame("How can I help?"))) is True

    # pack greeting, not the control-industry fallback
    hc = harness.pack_greeting(harness.load_blueprint("healthcare"))
    hc_filter = _not_greeting_text_filter(hc)
    assert asyncio.run(hc_filter(tts_text(hc))) is False
    assert asyncio.run(hc_filter(tts_text(greeting))) is True
    print("greeting gate ok")


def check_runtime(runtime: str, industry: str = "control-industry") -> None:
    check_greeting_gate()
    from pipecat.pipeline.llm_switcher import LLMSwitcher
    from pipecat.pipeline.pipeline import Pipeline

    bp = harness.load_blueprint(industry)
    assert runtime in harness.RUNTIMES, runtime
    expected = {a: harness.tool_names(bp, a) for a in bp["agents"]}
    if industry == "control-industry":
        assert expected == EXPECTED

    async def _noop(*_a, **_k):  # pragma: no cover - never called here
        raise AssertionError("tool handler must not run during the build check")

    stt, tts = harness.build_stt_tts(runtime)
    assert (stt is None) == (runtime != "cascaded")

    if runtime in harness.S2S_RUNTIMES:
        agent_llms = harness.build_agent_llms(runtime, bp, _noop)
        assert list(agent_llms) == harness.agent_order(bp)
        switcher = LLMSwitcher(llms=list(agent_llms.values()))
        # ServiceSwitcher starts on services[0]; that must be the receptionist.
        assert switcher.active_llm is agent_llms[bp["start"]]

        for agent, llm in agent_llms.items():
            # what the session advertises to the model...
            advertised = [t.name for t in llm._service_tools().standard_tools]
            assert advertised == expected[agent], (agent, advertised)
            # ...and what Pipecat will route a call for. `None` (context tools
            # unset) is how the live pipeline calls this: the service falls back
            # to its own tools, which is what keeps the two sessions distinct.
            llm._sync_registered_tool_handlers(None)
            for name in expected[agent]:
                assert llm.has_function(name), (agent, name)
            for name in set().union(*expected.values()) - set(expected[agent]):
                assert not llm.has_function(name), (agent, name)

        start = bp["start"]
        assert agent_llms[start] is switcher.active_llm
        if industry == "control-industry":
            assert not agent_llms["receptionist"].has_function("schedule_appointment")
            assert agent_llms["receptionist"] is not agent_llms["scheduler"]
        stages = [switcher]
    else:
        for agent in bp["agents"]:
            node = harness.flows_node(bp, agent, _noop)
            assert node["name"] == agent
            assert [f.name for f in node["functions"]] == expected[agent], agent
            assert node["task_messages"][0]["content"] == harness.instructions(bp, agent)
        assert "schedule_appointment" not in str(
            harness.instructions(bp, "receptionist")
        )
        stages = [harness.build_llm(runtime, "", None)]

    Pipeline([s for s in ([stt] + stages + [tts]) if s is not None])
    print(
        f"{runtime}: model={harness.RUNTIMES[runtime]} "
        f"agents={ {a: harness.tool_names(bp, a) for a in bp['agents']} } "
        f"stages={[type(s).__name__ for s in stages]} ok"
    )
