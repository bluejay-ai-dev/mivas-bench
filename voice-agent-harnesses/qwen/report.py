"""Qwen-Audio Realtime events → Bluejay OTel traces.

Same LangSmith-shaped tree as the OpenAI chirp tracer, driven from DashScope WS
events (OpenAI-realtime-compatible) instead of the Agents SDK:

  realtime_session
    └── turn                   (one caller utterance → ensuing agent activity)
          ├── user_message     (caller transcript)
          ├── model            (one generation per response: gen_ai.usage.* token
          │                     breakdown + time-to-first-token + output)
          ├── execute_tool <n> (tool calls + handoffs; Bluejay reads these)
          └── audio_interrupted

Token counts + TTFT come off Qwen response.created/response.done into standard
OTel GenAI (`gen_ai.*`) attributes; turn boundaries from server_vad
``input_audio_buffer.speech_started``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
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


def _deep_get(obj: Any, *path: str) -> Any:
    for key in path:
        if obj is None:
            return None
        obj = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    return obj


def _usage_attrs(usage: Any) -> dict[str, int]:
    """Realtime response usage → gen_ai.usage.* ints (audio/text/cached broken out)."""
    out: dict[str, int] = {}
    if usage is None:
        return out

    def put(key: str, *path: str) -> None:
        v = _deep_get(usage, *path)
        if isinstance(v, int):
            out[key] = v

    put(GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS, "input_tokens")
    put(GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, "output_tokens")
    put("gen_ai.usage.total_tokens", "total_tokens")
    put("gen_ai.usage.input_audio_tokens", "input_token_details", "audio_tokens")
    put("gen_ai.usage.input_text_tokens", "input_token_details", "text_tokens")
    put("gen_ai.usage.cached_tokens", "input_token_details", "cached_tokens")
    put("gen_ai.usage.output_audio_tokens", "output_token_details", "audio_tokens")
    put("gen_ai.usage.output_text_tokens", "output_token_details", "text_tokens")
    put("gen_ai.usage.output_reasoning_tokens", "output_token_details", "reasoning_tokens")
    return out


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
        try:
            from metering import processor as _usage_processor

            _meter = _usage_processor()
            if _meter is not None:
                provider.add_span_processor(_meter)
        except ImportError:
            logger.warning("metering unavailable (runtime/ not importable); spend is not tracked")

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
    """Qwen-Audio Realtime events → a LangSmith-shaped tree under realtime_session.

        realtime_session
          turn
            user_message          (caller transcript)
            model                 (generation: gen_ai.usage.* tokens + TTFT + output)
            execute_tool <name>   (tools/handoffs — parented via state["_otel_root"])

    The chirp adapter forwards every Qwen WS event to ``handle_raw``; tool spans
    the harness creates parent under ``current_turn()``.
    """

    def __init__(self, tracer: Tracer, root: Span, model: str | None = None) -> None:
        self._tracer = tracer
        self.root = root
        self._model = model
        self._turn: Span | None = None
        self._turn_index = 0
        self._seen_user_text: set[str] = set()
        self._llm_span: Span | None = None
        self._resp_start_mono: float | None = None
        self._resp_ttft_ms: float | None = None
        self._usage_input = 0
        self._usage_output = 0
        self._response_count = 0
        self._event_count = 0

    # -- turn management --
    def _current_turn(self) -> Span:
        if self._turn is None:
            self._turn_index += 1
            self._turn = self._tracer.start_span(
                "turn",
                context=otel_trace.set_span_in_context(self.root),
                kind=SpanKind.INTERNAL,
                attributes={"mivas.turn.index": self._turn_index},
            )
        return self._turn

    def current_turn(self) -> Span:
        """Public: parent for tool spans the harness creates during this turn."""
        return self._current_turn()

    def _turn_ctx(self):
        return otel_trace.set_span_in_context(self._current_turn())

    def _close_turn(self) -> None:
        if self._llm_span is not None:
            self._finish_llm(None)
        if self._turn is not None:
            self._turn.set_status(Status(StatusCode.OK))
            self._turn.end()
            self._turn = None

    # -- generation span (response.created → response.done) --
    def _start_llm(self, response: Any) -> None:
        if self._llm_span is not None:
            self._finish_llm(None)
        self._resp_start_mono = time.monotonic()
        self._resp_ttft_ms = None
        model = self._model or _deep_get(response, "model")
        attrs: dict[str, Any] = {
            GenAIAttributes.GEN_AI_OPERATION_NAME: "chat",
            GenAIAttributes.GEN_AI_SYSTEM: "qwen",
            "gen_ai.provider.name": "dashscope",
            "mivas.modality": "audio",
            "mivas.event": "response.created",
        }
        if model:
            attrs[GenAIAttributes.GEN_AI_REQUEST_MODEL] = str(model)
        rid = _deep_get(response, "id")
        if rid:
            attrs[GenAIAttributes.GEN_AI_RESPONSE_ID] = str(rid)
        self._llm_span = self._tracer.start_span(
            "model", context=self._turn_ctx(), kind=SpanKind.CLIENT, attributes=attrs
        )

    def _mark_first_output(self) -> None:
        if (
            self._llm_span is None
            or self._resp_ttft_ms is not None
            or self._resp_start_mono is None
        ):
            return
        self._resp_ttft_ms = (time.monotonic() - self._resp_start_mono) * 1000.0

    def _finish_llm(self, response: Any) -> None:
        span = self._llm_span
        self._llm_span = None
        ttft = self._resp_ttft_ms
        self._resp_start_mono = None
        self._resp_ttft_ms = None
        if span is None:
            return
        attrs = _usage_attrs(_deep_get(response, "usage"))
        for key, value in attrs.items():
            span.set_attribute(key, value)
        rid = _deep_get(response, "id")
        if rid:
            span.set_attribute(GenAIAttributes.GEN_AI_RESPONSE_ID, str(rid))
        rmodel = _deep_get(response, "model") or self._model
        if rmodel:
            span.set_attribute(GenAIAttributes.GEN_AI_RESPONSE_MODEL, str(rmodel))
        if ttft is not None:
            span.set_attribute("gen_ai.server.time_to_first_token", ttft / 1000.0)
            span.set_attribute("mivas.ttft_ms", round(ttft, 2))
        if _deep_get(response, "status") == "failed":
            msg = _deep_get(response, "status_details", "error", "message") or "response failed"
            span.set_status(Status(StatusCode.ERROR, str(msg)))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end()
        self._usage_input += attrs.get(GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS, 0)
        self._usage_output += attrs.get(GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, 0)
        self._response_count += 1
        self.root.set_attribute(GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS, self._usage_input)
        self.root.set_attribute(GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, self._usage_output)
        self.root.set_attribute("gen_ai.usage.total_tokens", self._usage_input + self._usage_output)
        self.root.set_attribute("mivas.response.count", self._response_count)

    def _user_message(self, text: str) -> None:
        key = text.strip()
        if not key or key in self._seen_user_text:
            return
        self._seen_user_text.add(key)
        span = self._tracer.start_span(
            "user_message",
            context=self._turn_ctx(),
            kind=SpanKind.INTERNAL,
            attributes={
                "mivas.role": "user",
                "mivas.transcript": _clip(text),
                GenAIAttributes.GEN_AI_INPUT_MESSAGES: _clip([{"role": "user", "content": text}]),
            },
        )
        span.set_status(Status(StatusCode.OK))
        span.end()

    # -- event entry point (called by the chirp adapter for every Qwen event) --
    def handle_raw(self, et: str | None, ev: dict[str, Any]) -> None:
        if not et:
            return
        self._event_count += 1
        try:
            self._dispatch(et, ev)
        except Exception:
            logger.debug("qwen tracer failed on %s", et, exc_info=True)

    def _dispatch(self, et: str, ev: dict[str, Any]) -> None:
        if et == "input_audio_buffer.speech_started":
            # New caller utterance → previous turn is done; open a fresh one.
            self._close_turn()
            self._current_turn()
        elif et == "conversation.item.input_audio_transcription.completed":
            tr = (ev.get("transcript") or "").strip()
            if tr:
                self._user_message(tr)
        elif et == "response.created":
            self._start_llm(ev.get("response"))
        elif et in ("response.audio.delta", "response.audio_transcript.delta"):
            if ev.get("delta"):
                self._mark_first_output()
        elif et == "response.audio_transcript.done":
            tr = (ev.get("transcript") or "").strip()
            if tr and self._llm_span is not None:
                self._llm_span.set_attribute("mivas.transcript", _clip(tr))
                self._llm_span.set_attribute(
                    GenAIAttributes.GEN_AI_OUTPUT_MESSAGES,
                    _clip([{"role": "assistant", "content": tr}]),
                )
        elif et == "response.done":
            self._finish_llm(ev.get("response"))

    def close(self) -> None:
        self._close_turn()
        self.root.set_attribute("mivas.event_count", self._event_count)


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
    model: str | None = None,
) -> AsyncIterator[Optional[QwenEventTracer]]:
    provider = setup_otel()
    if provider is None:
        yield None
        return

    tracer = otel_trace.get_tracer("mivas.qwen.audio")
    attrs: dict[str, Any] = {
        "mivas.workflow.name": workflow_name,
        GenAIAttributes.GEN_AI_OPERATION_NAME: "realtime_session",
        GenAIAttributes.GEN_AI_SYSTEM: "qwen",
        "gen_ai.provider.name": "dashscope",
    }
    if model:
        attrs[GenAIAttributes.GEN_AI_REQUEST_MODEL] = str(model)
    if simulation_result_id:
        attrs["bluejay.simulation_result_id"] = str(simulation_result_id)

    otel_tid: str | None = None
    event_tracer: QwenEventTracer | None = None
    try:
        with tracer.start_as_current_span(
            "realtime_session",
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
            event_tracer = QwenEventTracer(tracer, root, model=model)
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
