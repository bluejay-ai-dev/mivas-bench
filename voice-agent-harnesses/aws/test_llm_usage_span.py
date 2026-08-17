"""A Nova Sonic response must land as a `model` span under realtime_session →
turn, carrying the usageEvent token breakdown (per-turn delta) + TTFT.

    uv run python voice-agent-harnesses/aws/test_llm_usage_span.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402

# Nova usageEvent: per-turn `delta` + cumulative `total`, speech/text modality.
USAGE = {
    "completionId": "c1",
    "totalInputTokens": 120,
    "totalOutputTokens": 340,
    "totalTokens": 460,
    "details": {
        "delta": {
            "input": {"speechTokens": 40, "textTokens": 20},
            "output": {"speechTokens": 300, "textTokens": 15},
        },
        "total": {
            "input": {"speechTokens": 80, "textTokens": 40},
            "output": {"speechTokens": 320, "textTokens": 20},
        },
    },
}


def main() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    root = tracer.start_span("realtime_session")
    et = report.NovaEventTracer(tracer, root, model="amazon.nova-sonic-v1:0")

    et.on_caller_start()  # opens turn 1
    et.on_caller_stop()  # TTFT reference
    et.user_message("Book me an appointment")
    et.on_agent_audio()  # opens model span + stamps TTFT
    et.set_output("Sure, what day works?")
    et.record_usage(USAGE)  # delta → model span, total → root, ends the span
    et.close()
    root.end()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "turn" in spans, f"no turn span; got {list(spans)}"
    assert "user_message" in spans, f"no user_message span; got {list(spans)}"
    m = spans.get("model")
    assert m is not None, f"no model span; got {list(spans)}"
    a = m.attributes
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.system"] == "aws.bedrock"
    assert a["gen_ai.request.model"] == "amazon.nova-sonic-v1:0"
    # per-turn delta: input 40+20=60, output 300+15=315
    assert a["gen_ai.usage.input_tokens"] == 60, a.get("gen_ai.usage.input_tokens")
    assert a["gen_ai.usage.output_tokens"] == 315
    assert a["gen_ai.usage.total_tokens"] == 375
    assert a["gen_ai.usage.input_audio_tokens"] == 40
    assert a["gen_ai.usage.input_text_tokens"] == 20
    assert a["gen_ai.usage.output_audio_tokens"] == 300
    assert a["gen_ai.usage.output_text_tokens"] == 15
    assert "mivas.ttft_ms" in a and a["mivas.ttft_ms"] >= 0.0
    assert a["gen_ai.server.time_to_first_token"] >= 0.0
    assert "Sure, what day works?" in a["gen_ai.output.messages"]

    r = spans["realtime_session"].attributes
    # root carries cumulative total: input 80+40=120, output 320+20=340
    assert r["gen_ai.usage.input_tokens"] == 120, r.get("gen_ai.usage.input_tokens")
    assert r["gen_ai.usage.output_tokens"] == 340
    assert r["mivas.response.count"] == 1
    assert r["mivas.event_count"] >= 4
    print("ok")


if __name__ == "__main__":
    main()
