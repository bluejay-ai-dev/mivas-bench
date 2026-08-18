"""A Realtime response must land as a `chat` generation span with the same
telemetry LangSmith/Langfuse pull: token breakdown + time-to-first-token.

    uv run python voice-agent-harnesses/openai/test_llm_usage_span.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402

USAGE = {
    "input_tokens": 441,
    "output_tokens": 800,
    "total_tokens": 1241,
    "input_token_details": {"audio_tokens": 22, "text_tokens": 419, "cached_tokens": 7},
    "output_token_details": {"audio_tokens": 130, "text_tokens": 65, "reasoning_tokens": 605},
}


def _raw(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type="raw_model_event", data=payload)


def test_repeated_tool_calls_keep_request_output_pairs() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("tool-pair-test")
    root = tracer.start_span("realtime_session")
    event_tracer = report.RealtimeEventTracer(tracer, root)
    tool = SimpleNamespace(name="explain_charge")

    event_tracer.handle(
        SimpleNamespace(
            type="tool_start",
            tool=tool,
            arguments='{"line_item_id":"li_noshow"}',
        )
    )
    event_tracer.handle(
        SimpleNamespace(
            type="tool_start",
            tool=tool,
            arguments='{"line_item_id":"li_visit"}',
        )
    )
    event_tracer.handle(
        SimpleNamespace(
            type="tool_end",
            tool=tool,
            output='{"line_item_id":"li_noshow"}',
        )
    )
    event_tracer.handle(
        SimpleNamespace(
            type="tool_end",
            tool=tool,
            output='{"line_item_id":"li_visit"}',
        )
    )
    event_tracer.close()
    root.end()

    spans = [
        span
        for span in exporter.get_finished_spans()
        if span.name == "execute_tool explain_charge"
    ]
    assert len(spans) == 2
    pairs = [
        (
            span.attributes["gen_ai.tool.call.arguments"],
            span.attributes["gen_ai.tool.call.result"],
        )
        for span in spans
    ]
    assert pairs == [
        ('{"line_item_id":"li_noshow"}', '{"line_item_id":"li_noshow"}'),
        ('{"line_item_id":"li_visit"}', '{"line_item_id":"li_visit"}'),
    ]

    exporter2 = InMemorySpanExporter()
    provider2 = TracerProvider()
    provider2.add_span_processor(SimpleSpanProcessor(exporter2))
    tracer2 = provider2.get_tracer("tool-pair-reorder")
    root2 = tracer2.start_span("realtime_session")
    tracer2_events = report.RealtimeEventTracer(tracer2, root2)
    tracer2_events.handle(SimpleNamespace(type="tool_start", tool=tool, arguments='{"id":"a"}'))
    tracer2_events.handle(SimpleNamespace(type="tool_start", tool=tool, arguments='{"id":"b"}'))
    tracer2_events.handle(SimpleNamespace(type="tool_end", tool=tool, arguments='{"id":"b"}', output='{"ok":"b"}'))
    tracer2_events.handle(SimpleNamespace(type="tool_end", tool=tool, arguments='{"id":"a"}', output='{"ok":"a"}'))
    tracer2_events.close()
    root2.end()
    reordered = [
        (
            span.attributes["gen_ai.tool.call.arguments"],
            span.attributes["gen_ai.tool.call.result"],
        )
        for span in exporter2.get_finished_spans()
        if span.name == "execute_tool explain_charge"
    ]
    assert sorted(reordered) == [
        ('{"id":"a"}', '{"ok":"a"}'),
        ('{"id":"b"}', '{"ok":"b"}'),
    ]


def main() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    root = tracer.start_span("realtime_session")
    et = report.RealtimeEventTracer(tracer, root, model="gpt-realtime-2.1")

    et.handle(_raw({"type": "input_audio_buffer.speech_started"}))  # opens turn
    et.handle(_raw({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hi there"}))
    et.handle(_raw({"type": "response.created", "response": {"id": "resp_1"}}))
    et.handle(SimpleNamespace(type="audio", item_id="i1", content_index=0))  # → TTFT
    et.handle(_raw({"type": "response.output_audio_transcript.delta", "delta": "Hi", "item_id": "i1", "content_index": 0}))
    et.handle(_raw({"type": "response.output_audio_transcript.done", "transcript": "Hi, how can I help?"}))
    et.handle(_raw({"type": "response.done", "response": {"id": "resp_1", "status": "completed", "usage": USAGE}}))
    et.close()
    root.end()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    # LangSmith shape: realtime_session → turn → {user_message, model}
    assert "turn" in spans, f"no turn span; got {list(spans)}"
    assert "user_message" in spans, f"no user_message span; got {list(spans)}"
    chat = spans.get("model")
    assert chat is not None, f"no generation span; got {list(spans)}"
    a = chat.attributes
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.system"] == "openai"
    assert a["gen_ai.request.model"] == "gpt-realtime-2.1"
    assert a["gen_ai.response.id"] == "resp_1"
    assert a["gen_ai.usage.input_tokens"] == 441, a.get("gen_ai.usage.input_tokens")
    assert a["gen_ai.usage.output_tokens"] == 800
    assert a["gen_ai.usage.total_tokens"] == 1241
    assert a["gen_ai.usage.input_audio_tokens"] == 22
    assert a["gen_ai.usage.cached_tokens"] == 7
    assert a["gen_ai.usage.output_audio_tokens"] == 130
    assert a["gen_ai.usage.output_reasoning_tokens"] == 605
    assert "mivas.ttft_ms" in a and a["mivas.ttft_ms"] >= 0.0, a.get("mivas.ttft_ms")
    assert a["gen_ai.server.time_to_first_token"] >= 0.0
    assert "Hi, how can I help?" in a["gen_ai.output.messages"], a.get("gen_ai.output.messages")

    r = spans["realtime_session"].attributes
    assert r["gen_ai.usage.input_tokens"] == 441
    assert r["gen_ai.usage.output_tokens"] == 800
    assert r["gen_ai.usage.total_tokens"] == 1241
    assert r["mivas.response.count"] == 1
    assert r["mivas.event_count"] == 7
    print("ok")


if __name__ == "__main__":
    test_repeated_tool_calls_keep_request_output_pairs()
    main()
