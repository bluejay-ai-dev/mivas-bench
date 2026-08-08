"""OpenTelemetry → Bluejay via the official OpenAI Agents instrumentor.

Realtime does not emit Agents SDK local spans. The canonical fix is
``opentelemetry-instrumentation-openai-agents``, which:

  1. Registers an Agents SDK ``TracingProcessor`` → GenAI OTel (text agents)
  2. Patches ``RealtimeSession`` to emit OTel from session events 1:1
     (agent / tool / handoff / audio / LLM / usage)

We only:
  - point the TracerProvider at Bluejay OTLP
  - install that instrumentor
  - stamp ``bluejay.simulation_result_id`` + capture the root trace id
  - POST ``{trace_ids}`` after the call
  - fill two Realtime gaps the instrumentor misses (user audio transcript →
    prompt buffer; tool call arguments on tool_start)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator

import httpx
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.semconv._incubating.attributes import (
    gen_ai_attributes as GenAIAttributes,
)

logger = logging.getLogger("mivas.otel")

DEFAULT_OTLP_ENDPOINT = "https://otlp.getbluejay.ai/v1/traces"
DEFAULT_API_URL = "https://api.getbluejay.ai/v1"
TERMINAL_STATUSES = {
    "EVALUATING",
    "EVALUATED",
    "COMPLETED",
    "FAILED",
    "SYSTEM_ERROR",
    "NO_ANSWER",
    "CANCELLED",
}

_provider: TracerProvider | None = None
_instrumented = False
_pending_sim_id: ContextVar[str | None] = ContextVar("mivas_sim_id", default=None)
_captured_trace_id: ContextVar[str | None] = ContextVar("mivas_otel_tid", default=None)


def _api_url() -> str:
    return os.environ.get("BLUEJAY_API_URL", DEFAULT_API_URL).rstrip("/")


def _otlp_endpoint() -> str:
    return os.environ.get("BLUEJAY_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)


def _service_name() -> str:
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-openai")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


class _BluejayLinkProcessor(SpanProcessor):
    """Stamp sim id on root spans + remember the OTel trace id for POST."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        sim = _pending_sim_id.get()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return
        # Root / workflow span from the Realtime instrumentor.
        parent = getattr(span, "parent", None)
        if parent is None and sim:
            span.set_attribute("bluejay.simulation_result_id", str(sim))
            span.set_attribute("mivas.workflow.name", span.name)
            _captured_trace_id.set(format(ctx.trace_id, "032x"))

    def on_end(self, span: ReadableSpan) -> None:
        return

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def setup_otel() -> TracerProvider | None:
    """TracerProvider → Bluejay OTLP + official OpenAI Agents instrumentor."""
    global _provider, _instrumented

    api_key = _api_key()
    if not api_key:
        return None

    if _provider is None:
        # Capture full prompts/completions/tool IO (instrumentor respects these).
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
        os.environ.setdefault("TRACELOOP_TRACE_CONTENT", "true")

        resource = Resource.create({SERVICE_NAME: _service_name()})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            SimpleSpanProcessor(
                OTLPSpanExporter(_otlp_endpoint(), headers={"X-API-KEY": api_key})
            )
        )
        provider.add_span_processor(_BluejayLinkProcessor())
        otel_trace.set_tracer_provider(provider)
        _provider = provider
        logger.info("otel → %s service=%s", _otlp_endpoint(), _service_name())

    if not _instrumented:
        from opentelemetry.instrumentation.openai_agents import OpenAIAgentsInstrumentor

        # Keep OpenAI dashboard export alongside Bluejay OTLP.
        OpenAIAgentsInstrumentor().instrument(tracer_provider=_provider)
        _instrumented = True
        logger.info("OpenAIAgentsInstrumentor installed (incl. RealtimeSession patch)")

    return _provider


def flush() -> None:
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as e:
            logger.error("otel flush failed: %s", e)
    try:
        from agents import flush_traces

        flush_traces()
    except Exception:
        pass


def enrich_realtime_session(session: Any, *, simulation_result_id: str | None) -> None:
    """Stamp Bluejay ids + fill Realtime gaps the official instrumentor misses."""
    try:
        from opentelemetry.instrumentation.openai_agents import _realtime_wrappers as rw
        from agents.realtime.session import RealtimeSession
    except Exception:
        return

    state = rw._tracing_states.get(id(session))
    if state is not None and state.workflow_span is not None:
        if simulation_result_id:
            state.workflow_span.set_attribute(
                "bluejay.simulation_result_id", str(simulation_result_id)
            )
        ctx = state.workflow_span.get_span_context()
        if ctx.is_valid:
            _captured_trace_id.set(format(ctx.trace_id, "032x"))

    if getattr(RealtimeSession, "_mivas_enrich_installed", False):
        return

    current_put = RealtimeSession._put_event

    async def enriched_put_event(self: Any, event: Any) -> Any:
        result = await current_put(self, event)
        try:
            st = rw._tracing_states.get(id(self))
            if st is None:
                return result
            et = getattr(event, "type", None)
            if et == "tool_start":
                tool = getattr(event, "tool", None)
                name = getattr(tool, "name", None) if tool else None
                args = getattr(event, "arguments", None)
                span = st.tool_spans.get(name) if name else None
                if span is not None and args is not None:
                    span.set_attribute(
                        GenAIAttributes.GEN_AI_TOOL_CALL_ARGUMENTS,
                        args if isinstance(args, str) else str(args),
                    )
            elif et == "raw_model_event":
                data = getattr(event, "data", None)
                if data is None:
                    return result
                dtype, data = rw._unwrap_raw_event_data(data)
                if dtype == "input_audio_transcription_completed":
                    transcript = (
                        data.get("transcript")
                        if isinstance(data, dict)
                        else getattr(data, "transcript", None)
                    )
                    if transcript:
                        st.record_prompt("user", transcript)
        except Exception:
            pass
        return result

    RealtimeSession._put_event = enriched_put_event
    RealtimeSession._mivas_enrich_installed = True  # type: ignore[attr-defined]


async def _await_terminal_upsert(
    client: httpx.AsyncClient, simulation_result_id: str, timeout: float = 18.0
) -> str | None:
    deadline = time.monotonic() + timeout
    key = _api_key()
    if not key:
        return None
    while time.monotonic() < deadline:
        try:
            r = await client.get(
                f"{_api_url()}/retrieve-simulation-result/{simulation_result_id}",
                headers={"X-API-Key": key},
            )
            if r.status_code == 200:
                st = str(
                    ((r.json() or {}).get("simulation_result") or {}).get("status")
                )
                if st in TERMINAL_STATUSES:
                    return st
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None


async def post_trace_ids(simulation_result_id: str, trace_id: str) -> None:
    key = _api_key()
    if not key or not simulation_result_id or not trace_id:
        logger.warning(
            "skip update-simulation-result — sim=%s trace=%s key=%s",
            simulation_result_id,
            trace_id,
            bool(key),
        )
        return
    async with httpx.AsyncClient(timeout=20) as client:
        st = await _await_terminal_upsert(client, simulation_result_id)
        r = await client.post(
            f"{_api_url()}/update-simulation-result",
            json={
                "simulation_result_id": str(simulation_result_id),
                "trace_ids": [trace_id],
            },
            headers={"X-API-Key": key, "Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            logger.error(
                "update-simulation-result FAILED %s %s (status=%s)",
                r.status_code,
                r.text[:300],
                st,
            )
        else:
            logger.info(
                "update-simulation-result ok trace=%s sim=%s terminal=%s",
                trace_id,
                simulation_result_id,
                st,
            )


@asynccontextmanager
async def traced_run(
    workflow_name: str,
    *,
    simulation_result_id: str | None = None,
) -> AsyncIterator[None]:
    """Install instrumentor; RealtimeSession patch owns the span tree."""
    provider = setup_otel()
    if provider is None:
        yield
        return

    sim_token = _pending_sim_id.set(simulation_result_id)
    tid_token = _captured_trace_id.set(None)
    try:
        logger.info(
            "otel ready workflow=%s sim=%s (spans from OpenAIAgentsInstrumentor)",
            workflow_name,
            simulation_result_id,
        )
        yield
    finally:
        flush()
        otel_tid = _captured_trace_id.get()
        _pending_sim_id.reset(sim_token)
        _captured_trace_id.reset(tid_token)
        if simulation_result_id and otel_tid:
            await post_trace_ids(simulation_result_id, otel_tid)
        elif simulation_result_id and not otel_tid:
            logger.error(
                "have simulation_result_id=%s but no otel trace id to post",
                simulation_result_id,
            )
