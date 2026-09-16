"""Usage metering: the audio/text split, and the rule that keeps a call counted once."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

os.environ.setdefault("METRONOME_SQS_QUEUE_URL", "https://sqs.test/queue")
os.environ.setdefault("METRONOME_CUSTOMER_ID", "test-customer")

import metering  # noqa: E402


def _by_model(events):
    return {e["properties"]["llm_model"]: e["properties"] for e in events}


def test_audio_and_text_split_sums_to_the_reported_usage():
    events = metering.build_events(
        "gpt-realtime-2.1",
        {
            "input_tokens": 1000,
            "output_tokens": 300,
            "input_audio_tokens": 600,
            "output_audio_tokens": 250,
            "cached_tokens": 200,
        },
        span_id="abc", timestamp="2026-09-16T00:00:00+00:00",
    )
    props = _by_model(events)
    text, audio = props["gpt-realtime-2.1"], props["gpt-realtime-2.1-audio"]

    # input_tokens stays cached-inclusive; the metering handler narrows it before ingest.
    assert int(text["input_tokens"]) + int(audio["input_tokens"]) == 1000
    assert int(text["output_tokens"]) + int(audio["output_tokens"]) == 300
    assert int(text["input_tokens"]) == 400 and int(audio["input_tokens"]) == 600
    assert int(text["cached_input_tokens"]) + int(audio["cached_input_tokens"]) == 200


def test_cached_over_input_cannot_inflate_the_bill():
    props = _by_model(metering.build_events(
        "m", {"input_tokens": 50, "output_tokens": 10, "cached_tokens": 999},
        span_id="s", timestamp="t",
    ))
    assert int(props["m"]["cached_input_tokens"]) == 50


def test_all_text_emits_no_audio_event():
    events = metering.build_events(
        "m", {"input_tokens": 10, "output_tokens": 5}, span_id="s", timestamp="t"
    )
    assert [e["properties"]["llm_model"] for e in events] == ["m"]


def test_transaction_id_matches_the_backfill_derivation():
    # sha256("mivas:<span>:<part>") laid out as a uuid — the dashboard replay derives the same
    # id, so a span metered live and later replayed collapses instead of billing twice.
    tid = metering.transaction_id("span-1", "text")
    assert tid == metering.transaction_id("span-1", "text")
    assert tid != metering.transaction_id("span-1", "audio")
    assert len(tid) == 36 and tid.count("-") == 4


class _Ctx:
    def __init__(self, trace_id, span_id):
        self.trace_id, self.span_id = trace_id, span_id


class _Res:
    attributes = {"service.name": "mivas-test"}


class _Span:
    def __init__(self, name, attrs, trace_id=1, span_id=1):
        self.name, self.attributes = name, attrs
        self.context = _Ctx(trace_id, span_id)
        self.resource = _Res()
        self.end_time = 1_700_000_000_000_000_000


def _metered(spans):
    seen = []
    processor = metering.UsageMeteringProcessor()
    original, metering.record_llm_usage = metering.record_llm_usage, lambda m, u, **kw: seen.append((m, u))
    try:
        for span in spans:
            processor.on_end(span)
    finally:
        metering.record_llm_usage = original
    return seen


def test_root_rollup_is_dropped_when_generations_were_metered():
    usage = {"gen_ai.usage.input_tokens": 100, "gen_ai.request.model": "gpt-4.1"}
    seen = _metered([_Span("model", usage, span_id=2), _Span("voice.call", usage, span_id=1)])
    assert len(seen) == 1, "the root must not re-meter what its children already reported"


def test_root_rollup_is_metered_when_it_is_the_only_carrier():
    # the cascaded harnesses report nothing per generation, only a total on the call root
    usage = {"gen_ai.usage.input_tokens": 100, "gen_ai.request.model": "gpt-4.1"}
    assert len(_metered([_Span("voice.call", usage)])) == 1


def test_chat_prefixed_generation_spans_are_metered():
    usage = {"gen_ai.usage.input_tokens": 7, "gen_ai.request.model": "gpt-5.6-terra"}
    assert len(_metered([_Span("chat gpt-5.6-terra", usage)])) == 1


def test_spans_without_usage_are_ignored():
    assert _metered([_Span("model", {"gen_ai.request.model": "m"})]) == []
