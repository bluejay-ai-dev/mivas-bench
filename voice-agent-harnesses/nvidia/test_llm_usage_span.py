"""VoiceChat (S2S) responses land as realtime_session → turn → model with TTFT
and agent output. VoiceChat/NVCF reports no token usage, so token attrs are absent.

    uv run python voice-agent-harnesses/nvidia/test_llm_usage_span.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402


def main() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    root = tracer.start_span("realtime_session")
    rt = report.RealtimeSpanTracer(root, model="nvidia/nemotron-voicechat", tracer=tracer)

    rt.on_user_speech()                      # opens turn 1
    rt.start_model({"response": {"id": "r1"}})
    rt.mark_first_output()                   # → TTFT
    rt.set_output("Hi, how can I help?")
    rt.end_model({"response": {}})           # VoiceChat reports no usage
    rt.close()
    root.end()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "turn" in spans, f"no turn span; got {list(spans)}"
    model = spans.get("model")
    assert model is not None, f"no model span; got {list(spans)}"
    a = model.attributes
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.system"] == "nvidia"
    assert a["gen_ai.request.model"] == "nvidia/nemotron-voicechat"
    assert a["gen_ai.response.id"] == "r1"
    assert "mivas.ttft_ms" in a and a["mivas.ttft_ms"] >= 0.0, a.get("mivas.ttft_ms")
    assert a["gen_ai.server.time_to_first_token"] >= 0.0
    assert "Hi, how can I help?" in a["gen_ai.output.messages"], a.get("gen_ai.output.messages")
    # VoiceChat/NVCF exposes no token usage — token attrs must be absent, not faked.
    assert "gen_ai.usage.input_tokens" not in a, "usage must not be fabricated"

    r = spans["realtime_session"].attributes
    assert r["mivas.response.count"] == 1
    print("ok")


if __name__ == "__main__":
    main()
