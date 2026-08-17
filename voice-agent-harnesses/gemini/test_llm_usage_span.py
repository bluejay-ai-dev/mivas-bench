"""A Gemini Live response must land as a `model` generation span with the same
telemetry LangSmith/Langfuse pull: token breakdown + time-to-first-token, inside
the realtime_session → turn tree.

    uv run python voice-agent-harnesses/gemini/test_llm_usage_span.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402


def _mod(name: str, count: int) -> SimpleNamespace:
    return SimpleNamespace(modality=SimpleNamespace(name=name), token_count=count)


USAGE = SimpleNamespace(
    prompt_token_count=441,
    response_token_count=800,
    total_token_count=1241,
    cached_content_token_count=7,
    thoughts_token_count=605,
    prompt_tokens_details=[_mod("AUDIO", 22), _mod("TEXT", 419)],
    response_tokens_details=[_mod("AUDIO", 130), _mod("TEXT", 65)],
)


def main() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    root = tracer.start_span("realtime_session")
    tr = report.GeminiTrace(tracer, root, model="gemini-3.1-flash-live-preview")

    tr.start_turn()                       # caller speech.started → open turn
    tr.user_message("Hi there")           # input transcription
    tr.mark_ref()                         # caller stopped → TTFT baseline
    tr.on_model_audio()                   # first agent audio → open model span + TTFT
    tr.add_output("Hi, how can I help?")  # output transcription
    tr.record_usage(USAGE)
    tr.bump_event()
    tr.finish_model()                     # turn_complete
    tr.close()
    root.end()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "turn" in spans, f"no turn span; got {list(spans)}"
    assert "user_message" in spans, f"no user_message span; got {list(spans)}"
    m = spans.get("model")
    assert m is not None, f"no generation span; got {list(spans)}"
    a = m.attributes
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.system"] == "gcp.gemini", a.get("gen_ai.system")
    assert a["gen_ai.request.model"] == "gemini-3.1-flash-live-preview"
    assert a["gen_ai.usage.input_tokens"] == 441, a.get("gen_ai.usage.input_tokens")
    assert a["gen_ai.usage.output_tokens"] == 800
    assert a["gen_ai.usage.total_tokens"] == 1241
    assert a["gen_ai.usage.cached_tokens"] == 7
    assert a["gen_ai.usage.output_reasoning_tokens"] == 605
    assert a["gen_ai.usage.input_audio_tokens"] == 22
    assert a["gen_ai.usage.input_text_tokens"] == 419
    assert a["gen_ai.usage.output_audio_tokens"] == 130
    assert a["gen_ai.usage.output_text_tokens"] == 65
    assert "mivas.ttft_ms" in a and a["mivas.ttft_ms"] >= 0.0, a.get("mivas.ttft_ms")
    assert a["gen_ai.server.time_to_first_token"] >= 0.0
    assert "Hi, how can I help?" in a["gen_ai.output.messages"], a.get("gen_ai.output.messages")

    r = spans["realtime_session"].attributes
    assert r["gen_ai.usage.input_tokens"] == 441
    assert r["gen_ai.usage.output_tokens"] == 800
    assert r["gen_ai.usage.total_tokens"] == 1241
    assert r["mivas.response.count"] == 1
    assert r["mivas.event_count"] == 1
    print("ok")


if __name__ == "__main__":
    main()
