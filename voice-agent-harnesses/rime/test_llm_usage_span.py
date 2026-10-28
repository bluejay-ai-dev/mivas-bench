"""A Prism response must land as a `model` generation span with tokens + TTFT,
under realtime_session -> turn, the same LangSmith shape as the other harnesses.

    uv run python voice-agent-harnesses/rime/test_llm_usage_span.py
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

USAGE = {"input_tokens": 573, "output_tokens": 16, "total_tokens": 589, "context_window": 65536}


def main() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    root = tracer.start_span("realtime_session")
    et = report.PrismEventTracer(tracer, root, model="prism")

    et.handle_raw("input_audio_buffer.speech_started", {"item_id": "item_1"})  # opens turn
    et.handle_raw(
        "conversation.item.input_audio_transcription.completed",
        {"item_id": "item_1", "transcript": "Hi there"},
    )
    et.handle_raw("response.created", {"response": {"id": "resp_1", "metadata": {"prism_cause": "user_text"}}})
    et.handle_raw("response.text.delta", {"delta": "Hi"})  # -> TTFT
    et.handle_raw("response.audio.delta", {"delta": "AAAA"})
    et.handle_raw("response.text.done", {"text": "Hi, how can I help?"})
    et.handle_raw(
        "response.done",
        {"response": {"id": "resp_1", "status": "completed",
                      "status_details": {"reason": "stop"}, "usage": USAGE}},
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
    assert a["gen_ai.system"] == "rime"
    assert a["gen_ai.request.model"] == "prism"
    assert a["gen_ai.usage.input_tokens"] == 573
    assert a["gen_ai.usage.output_tokens"] == 16
    assert a["gen_ai.usage.total_tokens"] == 589
    assert a["mivas.ttft_ms"] >= 0
    assert a["mivas.response.cause"] == "user_text"
    assert "how can I help" in a["mivas.transcript"]
    r = spans["realtime_session"].attributes
    assert r["gen_ai.usage.input_tokens"] == 573 and r["mivas.response.count"] == 1
    print("rime usage span ok")


if __name__ == "__main__":
    main()
