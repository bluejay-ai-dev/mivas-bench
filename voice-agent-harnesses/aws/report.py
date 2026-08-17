"""Nova 2 Sonic events → Bluejay OTel traces.

LangSmith-shaped tree, driven from the Bedrock bidirectional stream:

  realtime_session
    └── turn                   (one caller utterance → all ensuing agent activity)
          ├── user_message     (caller ASR transcript)
          ├── model            (one generation per response: gen_ai.usage.* tokens
          │                     from Nova's usageEvent + time-to-first-token + output)
          ├── execute_tool <n> (tool calls; Bluejay reads these)
          └── audio_interrupted (barge-in)

Nova's ``usageEvent`` carries per-turn ``details.delta`` and cumulative
``details.total`` token counts (speech/text modality) — delta lands on the
``model`` span, total is rolled onto the root.
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
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-aws")


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


def _sum_int_leaves(node: Any) -> int | None:
    if not isinstance(node, dict):
        return None
    total, seen = 0, False
    for v in node.values():
        if isinstance(v, int):
            total += v
            seen = True
    return total if seen else None


def _nova_side_attrs(node: Any) -> dict[str, int]:
    """One side of Nova usage details (delta or total) → gen_ai.usage.* ints."""
    out: dict[str, int] = {}
    if not isinstance(node, dict):
        return out
    i = node.get("input") if isinstance(node.get("input"), dict) else {}
    o = node.get("output") if isinstance(node.get("output"), dict) else {}
    it, ot = _sum_int_leaves(i), _sum_int_leaves(o)
    if it is not None:
        out[GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS] = it
    if ot is not None:
        out[GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = ot
    if it is not None and ot is not None:
        out["gen_ai.usage.total_tokens"] = it + ot
    for k, attr in (("speechTokens", "input_audio_tokens"), ("audioTokens", "input_audio_tokens"), ("textTokens", "input_text_tokens")):
        if isinstance(i.get(k), int):
            out[f"gen_ai.usage.{attr}"] = i[k]
    for k, attr in (("speechTokens", "output_audio_tokens"), ("audioTokens", "output_audio_tokens"), ("textTokens", "output_text_tokens")):
        if isinstance(o.get(k), int):
            out[f"gen_ai.usage.{attr}"] = o[k]
    return out


def _nova_usage(usage: Any) -> tuple[dict[str, int], dict[str, int]]:
    """Nova usageEvent → (per-turn delta attrs, cumulative total attrs).

    Field names aren't published, so extraction is defensive: top-level
    total{Input,Output}Tokens fall back for the cumulative side, and if no
    per-turn delta is present the total is reused so the model span still
    carries token counts.
    """
    details = usage.get("details") if isinstance(usage, dict) else None
    delta = _nova_side_attrs(_deep_get(details, "delta"))
    total = _nova_side_attrs(_deep_get(details, "total"))
    if not total and isinstance(usage, dict):
        ti, to, tt = usage.get("totalInputTokens"), usage.get("totalOutputTokens"), usage.get("totalTokens")
        if isinstance(ti, int):
            total[GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS] = ti
        if isinstance(to, int):
            total[GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = to
        if isinstance(tt, int):
            total["gen_ai.usage.total_tokens"] = tt
    if not delta:
        delta = dict(total)
    return delta, total


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


class NovaEventTracer:
    """Nova Sonic events → LangSmith-shaped tree under ``realtime_session``.

        realtime_session
          turn
            user_message      caller ASR transcript
            model             gen_ai.usage.* (per-turn delta) + TTFT + output
            execute_tool <n>  (parented via harness tool_span → the active turn)
            audio_interrupted barge-in
    """

    def __init__(self, tracer: Tracer, root: Span, model: str | None = None) -> None:
        self._tracer = tracer
        self.root = root
        self._model = model
        self._turn: Span | None = None
        self._turn_index = 0
        self._model_span: Span | None = None
        self._user_stop_mono: float | None = None  # caller-stop ref for TTFB
        self._ttft_ms: float | None = None
        self._seen_user: set[str] = set()
        self._resp_count = 0
        self._event_count = 0

    # -- turn / model lifecycle -------------------------------------------

    def current_turn(self) -> Span:
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
        return otel_trace.set_span_in_context(self.current_turn())

    def _ensure_model(self) -> Span:
        if self._model_span is None:
            attrs: dict[str, Any] = {
                GenAIAttributes.GEN_AI_OPERATION_NAME: "chat",
                GenAIAttributes.GEN_AI_SYSTEM: "aws.bedrock",
                "gen_ai.provider.name": "aws.bedrock",
                "mivas.modality": "audio",
                "mivas.event": "response",
            }
            if self._model:
                attrs[GenAIAttributes.GEN_AI_REQUEST_MODEL] = str(self._model)
            self._model_span = self._tracer.start_span(
                "model", context=self._turn_ctx(), kind=SpanKind.CLIENT, attributes=attrs
            )
        return self._model_span

    def _finish_model(self) -> None:
        span = self._model_span
        self._model_span = None
        ttft = self._ttft_ms
        self._ttft_ms = None
        self._user_stop_mono = None
        if span is None:
            return
        if ttft is not None:
            span.set_attribute("gen_ai.server.time_to_first_token", ttft / 1000.0)
            span.set_attribute("mivas.ttft_ms", round(ttft, 2))
        span.set_status(Status(StatusCode.OK))
        span.end()
        self._resp_count += 1
        self.root.set_attribute("mivas.response.count", self._resp_count)

    def _close_turn(self) -> None:
        self._finish_model()
        if self._turn is not None:
            self._turn.set_status(Status(StatusCode.OK))
            self._turn.end()
            self._turn = None

    # -- event signals (called by the chirp bridge) -----------------------

    def on_caller_start(self) -> None:
        """CHIRP speech.started → previous turn is over; open a fresh one."""
        self._event_count += 1
        self._close_turn()
        self.current_turn()

    def on_caller_stop(self) -> None:
        self._event_count += 1
        self._user_stop_mono = time.monotonic()

    def user_message(self, text: str) -> None:
        self._event_count += 1
        key = (text or "").strip()
        if not key or key in self._seen_user:
            return
        self._seen_user.add(key)
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

    def on_agent_audio(self) -> None:
        """First agent audio of a response → open model span + stamp TTFT."""
        self._event_count += 1
        self._ensure_model()
        if self._ttft_ms is None and self._user_stop_mono is not None:
            self._ttft_ms = (time.monotonic() - self._user_stop_mono) * 1000.0

    def set_output(self, transcript: str | None) -> None:
        if not transcript:
            return
        span = self._ensure_model()
        span.set_attribute("mivas.transcript", _clip(transcript))
        span.set_attribute(
            GenAIAttributes.GEN_AI_OUTPUT_MESSAGES,
            _clip([{"role": "assistant", "content": transcript}]),
        )

    def record_usage(self, usage: Any) -> None:
        """Nova usageEvent → delta on the model span, total on the root; ends the span."""
        self._event_count += 1
        delta, total = _nova_usage(usage)
        if delta:
            span = self._ensure_model()
            if self._model:
                span.set_attribute(GenAIAttributes.GEN_AI_RESPONSE_MODEL, str(self._model))
            for k, v in delta.items():
                span.set_attribute(k, v)
        for k, v in total.items():
            self.root.set_attribute(k, v)
        # usageEvent fires at completionEnd → this response is done.
        self._finish_model()

    def interrupted(self) -> None:
        self._event_count += 1
        if self._model_span is not None:
            self._model_span.set_attribute("mivas.interrupted", True)
        span = self._tracer.start_span(
            "audio_interrupted",
            context=self._turn_ctx(),
            kind=SpanKind.INTERNAL,
            attributes={"mivas.event": "audio_interrupted"},
        )
        span.set_status(Status(StatusCode.OK))
        span.end()
        self._finish_model()

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
    tracer = otel_trace.get_tracer("mivas.aws.nova")
    attrs: dict[str, Any] = {
        GenAIAttributes.GEN_AI_OPERATION_NAME: "execute_tool",
        "gen_ai.provider.name": "aws.bedrock",
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
) -> AsyncIterator[Optional[NovaEventTracer]]:
    provider = setup_otel()
    if provider is None:
        yield None
        return

    tracer = otel_trace.get_tracer("mivas.aws.nova")
    attrs: dict[str, Any] = {
        "mivas.workflow.name": workflow_name,
        GenAIAttributes.GEN_AI_OPERATION_NAME: "realtime_session",
        GenAIAttributes.GEN_AI_SYSTEM: "aws.bedrock",
        "gen_ai.provider.name": "aws.bedrock",
    }
    if simulation_result_id:
        attrs["bluejay.simulation_result_id"] = str(simulation_result_id)
    if model:
        attrs[GenAIAttributes.GEN_AI_REQUEST_MODEL] = str(model)

    otel_tid: str | None = None
    event_tracer: NovaEventTracer | None = None
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
            event_tracer = NovaEventTracer(tracer, root, model=model)
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
