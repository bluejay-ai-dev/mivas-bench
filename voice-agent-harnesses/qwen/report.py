"""Qwen-Audio Realtime events → Bluejay OTel traces.

Same OTel tree as the OpenAI chirp tracer, driven from DashScope WS events
instead of the Agents SDK:

  voice.call
    ├── customer.speech   (CHIRP speech.started / completed)
    ├── agent.speech      (response.audio.delta … audio.done)
    └── execute_tool <n>
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator, Optional

import httpx
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv._incubating.attributes import (
    gen_ai_attributes as GenAIAttributes,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

logger = logging.getLogger("mivas.otel")

DEFAULT_OTLP_ENDPOINT = "https://otlp.getbluejay.ai/v1/traces"
DEFAULT_API_URL = "https://api.getbluejay.ai/v1"
_RETRYABLE_UPSERT_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTR = 4000

_provider: TracerProvider | None = None


def _api_url() -> str:
    return (os.environ.get("BLUEJAY_API_URL") or DEFAULT_API_URL).rstrip("/")


def _otlp_endpoint() -> str:
    return os.environ.get("BLUEJAY_OTLP_ENDPOINT") or DEFAULT_OTLP_ENDPOINT


def _service_name() -> str:
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-qwen")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


def _clip(value: Any, n: int = _MAX_ATTR) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


def setup_otel() -> TracerProvider | None:
    global _provider

    api_key = _api_key()
    if not api_key:
        return None

    if _provider is None:
        resource = Resource.create({SERVICE_NAME: _service_name()})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(_otlp_endpoint(), headers={"X-API-KEY": api_key}),
                max_queue_size=int(os.environ.get("MIVAS_OTEL_QUEUE", "32768")),
                max_export_batch_size=512,
                schedule_delay_millis=1000,
            )
        )
        otel_trace.set_tracer_provider(provider)
        _provider = provider
        logger.info("otel → %s service=%s", _otlp_endpoint(), _service_name())

    return _provider


def flush() -> None:
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as e:
            logger.error("otel flush failed: %s", e)


async def _post_update_simulation_result(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    key: str,
    *,
    attempts: int = 4,
) -> httpx.Response:
    last: httpx.Response | None = None
    for i in range(attempts):
        try:
            last = await client.post(
                f"{_api_url()}/update-simulation-result",
                json=body,
                headers={"X-API-Key": key, "Content-Type": "application/json"},
            )
        except httpx.TransportError as exc:
            if i == attempts - 1:
                raise
            logger.warning(
                "update-simulation-result transport error attempt %s/%s: %s",
                i + 1,
                attempts,
                exc,
            )
            await asyncio.sleep(2**i)
            continue
        if last.status_code < 400 or last.status_code not in _RETRYABLE_UPSERT_STATUS:
            return last
        if i == attempts - 1:
            return last
        logger.warning(
            "update-simulation-result %s attempt %s/%s, retrying",
            last.status_code,
            i + 1,
            attempts,
        )
        await asyncio.sleep(2**i)
    assert last is not None
    return last


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
    body = {
        "simulation_result_id": str(simulation_result_id),
        "trace_ids": [trace_id],
    }
    await asyncio.sleep(float(os.environ.get("MIVAS_UPSERT_SETTLE_SECONDS", "10")))
    async with httpx.AsyncClient(timeout=20) as client:
        r = await _post_update_simulation_result(client, body, key)
        if r.status_code >= 400:
            logger.error(
                "update-simulation-result FAILED %s %s",
                r.status_code,
                r.text[:300],
            )
        else:
            logger.info(
                "update-simulation-result ok trace=%s sim=%s",
                trace_id,
                simulation_result_id,
            )


class QwenEventTracer:
    """Maps CHIRP + Qwen-Audio events → child spans under ``voice.call``."""

    def __init__(self, tracer: Tracer, root: Span) -> None:
        self._tracer = tracer
        self.root = root
        self._customer_speech: Span | None = None
        self._agent_speech: Span | None = None
        self._tool_spans: dict[str, Span] = {}

    def start_customer_speech(self, utterance_id: str) -> Span | None:
        self.end_customer_speech()
        span = self._tracer.start_span(
            "customer.speech",
            context=otel_trace.set_span_in_context(self.root),
            kind=SpanKind.INTERNAL,
            attributes={
                GenAIAttributes.GEN_AI_OPERATION_NAME: "speech_to_text",
                "mivas.speech.speaker": "customer",
                "mivas.utterance_id": str(utterance_id),
                "mivas.event": "chirp.speech.started",
            },
        )
        self._customer_speech = span
        return span

    def end_customer_speech(self) -> None:
        if self._customer_speech is None:
            return
        self._customer_speech.set_status(Status(StatusCode.OK))
        self._customer_speech.end()
        self._customer_speech = None

    def start_agent_speech(self, utterance_id: str) -> Span | None:
        if self._agent_speech is not None:
            return self._agent_speech
        span = self._tracer.start_span(
            "agent.speech",
            context=otel_trace.set_span_in_context(self.root),
            kind=SpanKind.INTERNAL,
            attributes={
                "mivas.event": "audio",
                "mivas.utterance_id": str(utterance_id),
                "mivas.speech.speaker": "agent",
            },
        )
        self._agent_speech = span
        return span

    def end_agent_speech(self, *, transcript: str | None = None) -> None:
        if self._agent_speech is None:
            return
        if transcript:
            self._agent_speech.set_attribute("mivas.transcript", _clip(transcript))
            self._agent_speech.set_attribute(
                GenAIAttributes.GEN_AI_OUTPUT_MESSAGES,
                _clip([{"role": "assistant", "content": transcript}]),
            )
        self._agent_speech.set_status(Status(StatusCode.OK))
        self._agent_speech.end()
        self._agent_speech = None

    def close(self) -> None:
        self.end_customer_speech()
        self.end_agent_speech()
        for span in list(self._tool_spans.values()):
            span.set_status(Status(StatusCode.OK))
            span.end()
        self._tool_spans.clear()


@contextmanager
def tool_span(
    name: str,
    parameters: Any = None,
    *,
    call_id: str | None = None,
    parent: Span | None = None,
) -> Iterator[Span | None]:
    root = parent if parent is not None and parent.get_span_context().is_valid else None
    if root is None:
        yield None
        return
    tracer = otel_trace.get_tracer("mivas.qwen.audio")
    attrs: dict[str, Any] = {
        GenAIAttributes.GEN_AI_OPERATION_NAME: "execute_tool",
        "gen_ai.provider.name": "dashscope",
        GenAIAttributes.GEN_AI_TOOL_NAME: name,
        GenAIAttributes.GEN_AI_TOOL_CALL_ARGUMENTS: _clip(
            parameters if parameters is not None else {}
        ),
    }
    if call_id:
        attrs["gen_ai.tool.call.id"] = str(call_id)
    span = tracer.start_span(
        f"execute_tool {name}",
        context=otel_trace.set_span_in_context(root),
        kind=SpanKind.CLIENT,
        attributes=attrs,
    )
    try:
        yield span
    except Exception as e:
        span.record_exception(e)
        span.set_status(Status(StatusCode.ERROR, str(e)[:400]))
        span.end()
        raise
    else:
        if span.is_recording():
            span.end()


def finish_tool_span(
    span: Span | None,
    output: Any,
    *,
    ok: bool = True,
) -> None:
    if span is None:
        return
    span.set_attribute(GenAIAttributes.GEN_AI_TOOL_CALL_RESULT, _clip(output))
    if ok:
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_status(Status(StatusCode.ERROR, _clip(output)[:400]))


@asynccontextmanager
async def traced_run(
    workflow_name: str,
    *,
    simulation_result_id: str | None = None,
) -> AsyncIterator[Optional[QwenEventTracer]]:
    provider = setup_otel()
    if provider is None:
        yield None
        return

    tracer = otel_trace.get_tracer("mivas.qwen.audio")
    attrs: dict[str, Any] = {
        "mivas.workflow.name": workflow_name,
        GenAIAttributes.GEN_AI_OPERATION_NAME: "voice.call",
        "gen_ai.provider.name": "dashscope",
    }
    if simulation_result_id:
        attrs["bluejay.simulation_result_id"] = str(simulation_result_id)

    otel_tid: str | None = None
    event_tracer: QwenEventTracer | None = None
    try:
        with tracer.start_as_current_span(
            "voice.call",
            kind=SpanKind.SERVER,
            attributes=attrs,
        ) as root:
            ctx = root.get_span_context()
            if ctx.is_valid:
                otel_tid = format(ctx.trace_id, "032x")
                logger.info(
                    "otel trace_id=%s sim=%s workflow=%s",
                    otel_tid,
                    simulation_result_id,
                    workflow_name,
                )
            event_tracer = QwenEventTracer(tracer, root)
            yield event_tracer
            event_tracer.close()
    finally:
        flush()
        if simulation_result_id:
            try:
                from snapshot import capture_final

                await asyncio.to_thread(capture_final, str(simulation_result_id))
            except Exception:
                logger.exception("final snapshot failed sim=%s", simulation_result_id)
        if simulation_result_id and otel_tid:
            await post_trace_ids(simulation_result_id, otel_tid)
        elif simulation_result_id and not otel_tid:
            logger.error(
                "have simulation_result_id=%s but no otel trace id to post",
                simulation_result_id,
            )
