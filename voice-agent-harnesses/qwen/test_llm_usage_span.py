"""A Qwen response must land as a `model` generation span with token breakdown +
TTFT, under realtime_session → turn, the same LangSmith shape as the OpenAI harness.

    uv run python voice-agent-harnesses/qwen/test_llm_usage_span.py
"""

from __future__ import annotations

import sys
from pathlib import Path

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
    "output_token_details": {"audio_tokens": 130, "text_tokens": 65},
}


def main() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    root = tracer.start_span("realtime_session")
    et = report.QwenEventTracer(tracer, root, model="qwen3-omni-flash-realtime")

    et.handle_raw("input_audio_buffer.speech_started", {})  # opens turn
    et.handle_raw(
        "conversation.item.input_audio_transcription.completed",
        {"transcript": "Hi there"},
    )
    et.handle_raw("response.created", {"response": {"id": "resp_1"}})
    et.handle_raw("response.audio.delta", {"delta": "AAAA"})  # → TTFT
    et.handle_raw("response.audio_transcript.done", {"transcript": "Hi, how can I help?"})
    et.handle_raw(
        "response.done",
        {"response": {"id": "resp_1", "status": "completed", "usage": USAGE}},
    )
    et.close()
    root.end()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "turn" in spans, f"no turn span; got {list(spans)}"
    assert "user_message" in spans, f"no user_message span; got {list(spans)}"
    m = spans.get("model")
    assert m is not None, f"no generation span; got {list(spans)}"
    a = m.attributes
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.system"] == "qwen"
    assert a["gen_ai.request.model"] == "qwen3-omni-flash-realtime"
    assert a["gen_ai.response.id"] == "resp_1"
    assert a["gen_ai.usage.input_tokens"] == 441
    assert a["gen_ai.usage.output_tokens"] == 800
    assert a["gen_ai.usage.total_tokens"] == 1241
    assert a["gen_ai.usage.input_audio_tokens"] == 22
    assert a["gen_ai.usage.cached_tokens"] == 7
    assert a["gen_ai.usage.output_audio_tokens"] == 130
    assert "mivas.ttft_ms" in a and a["mivas.ttft_ms"] >= 0.0
    assert a["gen_ai.server.time_to_first_token"] >= 0.0
    assert "Hi, how can I help?" in a["gen_ai.output.messages"]

    r = spans["realtime_session"].attributes
    assert r["gen_ai.usage.input_tokens"] == 441
    assert r["gen_ai.usage.output_tokens"] == 800
    assert r["gen_ai.usage.total_tokens"] == 1241
    assert r["mivas.response.count"] == 1
    assert r["mivas.event_count"] == 6
    print("ok")


if __name__ == "__main__":
    main()
