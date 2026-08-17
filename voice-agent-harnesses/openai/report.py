"""Realtime session events → Bluejay OTel traces.

LangSmith-shaped proxy: wrap the ``RealtimeSession``, parse its event stream,
and emit the same tree a proper tracing SDK (LangSmith/Langfuse) would:

  realtime_session
    └── turn                   (one caller utterance → all ensuing agent activity)
          ├── user_message     (caller transcript)
          ├── model            (one generation per response: gen_ai.usage.* token
          │                     breakdown + time-to-first-token + output)
          ├── execute_tool <n> (tool calls + handoffs-as-tools; Bluejay reads these)
          └── audio_interrupted (barge-in)

Token counts + TTFT are pulled off the Realtime response.created/response.done
events into standard OTel GenAI (`gen_ai.*`) attributes and rolled up onto the
root, exported to Bluejay's own OTLP. Turn boundaries come from OpenAI's
``input_audio_buffer.speech_started``; per-chunk audio is parsed for TTFT only,
never emitted as spans.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

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

# target agent name → the blueprint's handoff tool name, so a handoff is reported under
# the name the industry declares (`transfer_to_identity`) rather than a synthesized one.
# Populated by build_from_blueprint; unknown targets fall back to handoff_to_<target>.
_HANDOFF_TOOL_NAMES: dict[str, str] = {}


def register_handoff_tool_names(mapping: dict[str, str]) -> None:
    _HANDOFF_TOOL_NAMES.update(mapping)

_provider: TracerProvider | None = None


def _api_url() -> str:
    return (os.environ.get("BLUEJAY_API_URL") or DEFAULT_API_URL).rstrip("/")


def _otlp_endpoint() -> str:
    return os.environ.get("BLUEJAY_OTLP_ENDPOINT") or DEFAULT_OTLP_ENDPOINT


def _service_name() -> str:
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-openai")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


def _clip(value: Any, n: int = _MAX_ATTR) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


def _deep_get(obj: Any, *path: str) -> Any:
    """Read a dotted path off a pydantic model or a plain dict."""
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


def _unwrap_raw(data: Any) -> tuple[str | None, Any]:
    if isinstance(data, dict):
        dtype = data.get("type")
        nested = data.get("data")
        if isinstance(nested, dict) and nested.get("type"):
            return nested.get("type"), nested
        return dtype, data
    dtype = getattr(data, "type", None)
    nested = getattr(data, "data", None)
    if nested is not None:
        nested_type = (
            nested.get("type")
            if isinstance(nested, dict)
            else getattr(nested, "type", None)
        )
        if nested_type:
            return nested_type, nested
    return dtype, data


def setup_otel() -> TracerProvider | None:
    """TracerProvider → Bluejay OTLP. No Agents instrumentor."""
    global _provider

    api_key = _api_key()
    if not api_key:
        return None

    if _provider is None:
        try:
            from agents import set_tracing_disabled

            set_tracing_disabled(True)
        except Exception:
            pass

        resource = Resource.create({SERVICE_NAME: _service_name()})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            # BatchSpanProcessor, not SimpleSpanProcessor: Simple exports every span
            # with a BLOCKING http request from whatever thread ended the span — the
            # asyncio loop. At max_concurrent=20 that starved the loop badly enough
            # that calls stopped dispatching tools at all (no per-call DB was created
            # for the last third of run 228909) and the spans that did exist landed
            # after the trace-link POST, so Bluejay extracted 1 tool out of 7. Batching
            # moves the export to a background thread; force_flush() before the POST
            # still guarantees delivery.
            BatchSpanProcessor(
                OTLPSpanExporter(_otlp_endpoint(), headers={"X-API-KEY": api_key}),
                # defaults (2048 queue / 512 batch / 5 s delay) overflow at 60
                # concurrent calls — every span past the queue is dropped SILENTLY,
                # which cost 27 of 180 samples their tool data in run 229001 while
                # the per-call DBs proved the tools had run.
                max_queue_size=int(os.environ.get("MIVAS_OTEL_QUEUE", "32768")),
                max_export_batch_size=512,
                schedule_delay_millis=1000,
            )
        )
        otel_trace.set_tracer_provider(provider)
        _provider = provider
        logger.info("otel → %s service=%s (event proxy)", _otlp_endpoint(), _service_name())

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
    """POST trace_ids to Bluejay, retrying transient 5xx/429 so a single 502 does not drop the link."""
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
    """Link this call's OTel trace after hangup. One POST, no wait for COMPLETED.

    traced_run already force_flush()'d; that only proves the collector accepted the
    spans. Middleware extracts execute_tool spans with a single ClickHouse read at
    POST time, so settle first. A second POST re-extracts every span and doubles
    the tool timeline — do not relink.
    """
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


class RealtimeEventTracer:
    """RealtimeSession events → a LangSmith-shaped tree under ``realtime_session``.

        realtime_session
          turn                    (one caller utterance → all ensuing agent activity)
            user_message          (caller transcript)
            model                 (generation: gen_ai.usage.* tokens + TTFT + output)
            execute_tool <name>   (tool calls / handoffs — Bluejay reads these)
            audio_interrupted     (barge-in)

    No per-chunk speech spans and no separate agent-window / agent.turn / agent.speech
    spans: the caller turn is one ``user_message``, the agent turn is the ``model``
    generation. Turn boundaries come from OpenAI ``input_audio_buffer.speech_started``.
    """

    def __init__(self, tracer: Tracer, root: Span, model: str | None = None) -> None:
        self._tracer = tracer
        self.root = root
        self._model = model
        self._turn: Span | None = None
        self._turn_index = 0
        self._tool_spans: dict[str, Span] = {}
        self._seen_user_text: set[str] = set()
        # One generation span per Realtime response (response.created → response.done),
        # carrying tokens + TTFT, the way LangSmith/Langfuse report a model call.
        self._llm_span: Span | None = None
        self._resp_start_mono: float | None = None
        self._resp_ttft_ms: float | None = None
        self._usage_input = 0
        self._usage_output = 0
        self._response_count = 0
        self._event_count = 0

    def wrap(self, session: Any) -> "TracedRealtimeSession":
        return TracedRealtimeSession(session, self)

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

    def _turn_ctx(self):
        return otel_trace.set_span_in_context(self._current_turn())

    def _close_turn(self) -> None:
        """End the model span, any open tools, and the turn itself."""
        if self._llm_span is not None:
            self._finish_llm(None)
        for span in list(self._tool_spans.values()):
            span.set_status(Status(StatusCode.OK))
            span.end()
        self._tool_spans.clear()
        if self._turn is not None:
            self._turn.set_status(Status(StatusCode.OK))
            self._turn.end()
            self._turn = None

    def _start_llm(self, response: Any) -> None:
        """response.created → open a generation span; duration = model latency."""
        if self._llm_span is not None:
            self._finish_llm(None)  # never leak a span if done was missed
        self._resp_start_mono = time.monotonic()
        self._resp_ttft_ms = None
        model = self._model or _deep_get(response, "model")
        attrs: dict[str, Any] = {
            GenAIAttributes.GEN_AI_OPERATION_NAME: "chat",
            GenAIAttributes.GEN_AI_SYSTEM: "openai",
            "mivas.modality": "audio",
            "mivas.event": "response.created",
        }
        if model:
            attrs[GenAIAttributes.GEN_AI_REQUEST_MODEL] = str(model)
        rid = _deep_get(response, "id")
        if rid:
            attrs[GenAIAttributes.GEN_AI_RESPONSE_ID] = str(rid)
        self._llm_span = self._tracer.start_span(
            "model",
            context=self._turn_ctx(),
            kind=SpanKind.CLIENT,
            attributes=attrs,
        )

    def _mark_first_output(self) -> None:
        """First audio/transcript chunk of the current response → time to first token."""
        if (
            self._llm_span is None
            or self._resp_ttft_ms is not None
            or self._resp_start_mono is None
        ):
            return
        self._resp_ttft_ms = (time.monotonic() - self._resp_start_mono) * 1000.0

    def _finish_llm(self, response: Any) -> None:
        """response.done → stamp usage + TTFT, end the span, roll totals onto root."""
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
            # semconv metric name (seconds) + a ms attribute, as the SDKs surface both.
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
        self.root.set_attribute(
            GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS, self._usage_input
        )
        self.root.set_attribute(
            GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, self._usage_output
        )
        self.root.set_attribute(
            "gen_ai.usage.total_tokens", self._usage_input + self._usage_output
        )
        self.root.set_attribute("mivas.response.count", self._response_count)

    def handle(self, event: Any) -> None:
        etype = getattr(event, "type", None) or "unknown"
        self._event_count += 1
        try:
            self._dispatch(etype, event)
        except Exception:
            logger.debug("event tracer failed on %s", etype, exc_info=True)

    def _dispatch(self, etype: str, event: Any) -> None:
        if etype == "tool_start":
            tool = getattr(event, "tool", None)
            name = getattr(tool, "name", None) or "unknown_tool"
            args = getattr(event, "arguments", None)
            attrs: dict[str, Any] = {
                GenAIAttributes.GEN_AI_OPERATION_NAME: "execute_tool",
                GenAIAttributes.GEN_AI_TOOL_NAME: name,
                "mivas.event": "tool_start",
            }
            if args is not None:
                attrs[GenAIAttributes.GEN_AI_TOOL_CALL_ARGUMENTS] = _clip(args)
            self._tool_spans[name] = self._tracer.start_span(
                f"execute_tool {name}",
                context=self._turn_ctx(),
                kind=SpanKind.INTERNAL,
                attributes=attrs,
            )

        elif etype == "tool_end":
            tool = getattr(event, "tool", None)
            name = getattr(tool, "name", None) or "unknown_tool"
            output = getattr(event, "output", None)
            span = self._tool_spans.pop(name, None)
            if span is not None:
                if output is not None:
                    span.set_attribute(
                        GenAIAttributes.GEN_AI_TOOL_CALL_RESULT, _clip(output)
                    )
                span.set_status(Status(StatusCode.OK))
                span.end()

        elif etype == "handoff":
            frm = getattr(event, "from_agent", None)
            to = getattr(event, "to_agent", None)
            from_name = getattr(frm, "name", None) or "Unknown"
            to_name = getattr(to, "name", None) or "Unknown"
            tool_name = _HANDOFF_TOOL_NAMES.get(to_name) or f"handoff_to_{to_name}"
            # Bluejay tool_calls path: handoffs show up as execute_tool.
            with self._tracer.start_as_current_span(
                f"execute_tool {tool_name}",
                context=self._turn_ctx(),
                kind=SpanKind.INTERNAL,
                attributes={
                    GenAIAttributes.GEN_AI_OPERATION_NAME: "execute_tool",
                    GenAIAttributes.GEN_AI_TOOL_NAME: tool_name,
                    "gen_ai.handoff.from_agent": from_name,
                    "gen_ai.handoff.to_agent": to_name,
                    "mivas.event": "handoff",
                },
            ) as span:
                span.set_attribute(
                    GenAIAttributes.GEN_AI_TOOL_CALL_ARGUMENTS,
                    _clip({"from": from_name, "to": to_name}),
                )
                span.set_attribute(
                    GenAIAttributes.GEN_AI_TOOL_CALL_RESULT,
                    _clip({"success": True, "to_agent": to_name}),
                )
                span.set_status(Status(StatusCode.OK))

        elif etype == "audio":
            self._mark_first_output()  # first audio chunk → TTFT; no per-chunk span

        elif etype == "audio_interrupted":
            if self._llm_span is not None:
                self._llm_span.set_attribute("mivas.interrupted", True)
            span = self._tracer.start_span(
                "audio_interrupted",
                context=self._turn_ctx(),
                kind=SpanKind.INTERNAL,
                attributes={"mivas.event": "audio_interrupted"},
            )
            span.set_status(Status(StatusCode.OK))
            span.end()

        elif etype == "error":
            err = getattr(event, "error", "Unknown error")
            self.root.set_attribute("mivas.error", _clip(err, 1000))
            self.root.record_exception(Exception(str(err)))

        elif etype == "raw_model_event":
            data = getattr(event, "data", None)
            dtype, payload = _unwrap_raw(data)
            self._raw(dtype, payload)

    def _raw(self, dtype: str | None, data: Any) -> None:
        if not dtype:
            return

        def _get(obj: Any, key: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        if dtype == "input_audio_buffer.speech_started":
            # New caller utterance → the previous turn is done; open a fresh one.
            self._close_turn()
            self._current_turn()
            return

        if dtype in {
            "conversation.item.input_audio_transcription.completed",
            "input_audio_transcription_completed",
        }:
            transcript = _get(data, "transcript")
            if transcript:
                self._user_message(str(transcript))
            return

        if dtype in {
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        }:
            if _get(data, "delta"):
                self._mark_first_output()
            return

        if dtype in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        }:
            transcript = _get(data, "transcript")
            if transcript and self._llm_span is not None:
                self._llm_span.set_attribute("mivas.transcript", _clip(transcript))
                self._llm_span.set_attribute(
                    GenAIAttributes.GEN_AI_OUTPUT_MESSAGES,
                    _clip([{"role": "assistant", "content": str(transcript)}]),
                )
            return

        if dtype == "response.created":
            self._start_llm(_get(data, "response"))
            return

        if dtype == "response.done":
            self._finish_llm(_get(data, "response"))
            return

    def _user_message(self, text: str) -> None:
        """Caller transcript → a point ``user_message`` span under the current turn."""
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
                GenAIAttributes.GEN_AI_INPUT_MESSAGES: _clip(
                    [{"role": "user", "content": text}]
                ),
            },
        )
        span.set_status(Status(StatusCode.OK))
        span.end()

    def close(self) -> None:
        self._close_turn()
        self.root.set_attribute("mivas.event_count", self._event_count)


class TracedRealtimeSession:
    """Transparent proxy: same API as RealtimeSession, events → tracer.

    ``RealtimeSession.__aiter__`` is an async generator (not ``__anext__`` on
    the session), so we wrap that iterator rather than calling ``__anext__``
    on the session object.
    """

    def __init__(self, session: Any, tracer: RealtimeEventTracer) -> None:
        self._session = session
        self._tracer = tracer
        self._iter: Any = None

    def __aiter__(self) -> "TracedRealtimeSession":
        # RealtimeSession.__aiter__ is `async def` + yield → async gen object.
        self._iter = self._session.__aiter__()
        return self

    async def __anext__(self) -> Any:
        if self._iter is None:
            self._iter = self._session.__aiter__()
        try:
            event = await self._iter.__anext__()
        except StopAsyncIteration:
            raise
        self._tracer.handle(event)
        return event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@asynccontextmanager
async def traced_run(
    workflow_name: str,
    *,
    simulation_result_id: str | None = None,
    model: str | None = None,
) -> AsyncIterator[Optional[RealtimeEventTracer]]:
    """Open ``voice.call`` root; yield an event tracer (or None if no API key)."""
    provider = setup_otel()
    if provider is None:
        yield None
        return

    tracer = otel_trace.get_tracer("mivas.openai.realtime")
    attrs: dict[str, Any] = {
        "mivas.workflow.name": workflow_name,
        GenAIAttributes.GEN_AI_OPERATION_NAME: "realtime_session",
        GenAIAttributes.GEN_AI_SYSTEM: "openai",
    }
    if simulation_result_id:
        attrs["bluejay.simulation_result_id"] = str(simulation_result_id)

    otel_tid: str | None = None
    event_tracer: RealtimeEventTracer | None = None
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
            if model:
                root.set_attribute(GenAIAttributes.GEN_AI_REQUEST_MODEL, str(model))
            event_tracer = RealtimeEventTracer(tracer, root, model=model)
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
