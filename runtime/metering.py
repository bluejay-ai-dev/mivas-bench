"""Provider usage from a bench run → the Bluejay usage metering queue.

The bench pays for its own provider calls and no harness knows anything about billing, so
usage is read back off the spans the tracers already stamp. One span processor, registered
beside the OTLP exporter, turns ``gen_ai.usage.*`` into ``llm_request`` events on the same
SQS queue the rest of the platform meters through, which then bills them and copies them
into ClickHouse ``cost_events``.

Events carry ``unmetered=true`` and go out under an org whose contract holds no rates, so
nothing here can price anything; the point is that internal spend stops being invisible.

Inert unless ``METRONOME_SQS_QUEUE_URL`` and ``METRONOME_CUSTOMER_ID`` are both set, so a
clone of the bench outside Bluejay never tries to meter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Iterable

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

logger = logging.getLogger(__name__)

SOURCE_SERVICE = "mivas_bench"

# Spans that carry one generation's usage. A harness that names its generation span
# something else lands in the skipped-shapes warning below rather than vanishing.
GENERATION_SPANS = {"model", "agent_turn", "realtime_inference"}
GENERATION_PREFIX = "chat "

# The per-call root. Metered only when a trace produced no generation span at all, which is
# how the cascaded harnesses report: they aggregate the SDK's metrics onto the root and emit
# nothing per generation. Every other harness reaches the root having already metered, so
# the rollup is dropped and no call is counted twice.
ROOT_SPANS = {"voice.call"}

_FLUSH_AT = 100


def _queue_url() -> str:
    return os.environ.get("METRONOME_SQS_QUEUE_URL", "").strip()


def _customer_id() -> str:
    return os.environ.get("METRONOME_CUSTOMER_ID", "").strip()


def _org_id() -> str:
    return os.environ.get("METRONOME_ORG_ID", "").strip()


def enabled() -> bool:
    return bool(_queue_url() and _customer_id())


def transaction_id(span_id: str, part: str) -> str:
    """Same derivation the dashboard's backfill uses, so a span metered live and a span
    replayed out of otel_traces collapse onto one id instead of billing twice."""
    h = hashlib.sha256(f"mivas:{span_id}:{part}".encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _int(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def build_events(
    model: str,
    usage: dict[str, Any],
    *,
    span_id: str,
    timestamp: str,
    source_operation: str = "",
    metadata: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One generation becomes up to two events. cost_events has no audio column and realtime
    audio input bills several times text, so audio rides under ``{model}-audio``, matching how
    the dashboard already splits realtime usage. Anything not broken out as audio counts as
    text, so the two events still sum to what the provider reported.

    ``input_tokens`` goes out cached-inclusive: the metering handler narrows it to the uncached
    remainder before ingest, and pre-subtracting here would drop the cached tokens twice.
    """
    if not model:
        return []

    total_in = _int(usage.get("input_tokens"))
    total_out = _int(usage.get("output_tokens"))
    audio_in = min(_int(usage.get("input_audio_tokens")), total_in)
    audio_out = min(_int(usage.get("output_audio_tokens")), total_out)
    text_in = total_in - audio_in
    cached = min(_int(usage.get("cached_tokens")), total_in)
    cached_text = min(cached, text_in)
    cached_audio = cached - cached_text

    base: dict[str, str] = {
        "unmetered": "true",
        "source_service": SOURCE_SERVICE,
        "span_id": span_id,
        **(metadata or {}),
    }
    if source_operation:
        base["source_operation"] = source_operation
    if _org_id():
        base["org_id"] = _org_id()

    def event(part: str, name: str, inp: int, outp: int) -> Iterable[dict[str, Any]]:
        if inp + outp == 0:
            return ()
        return (
            {
                "transaction_id": transaction_id(span_id, part),
                "timestamp": timestamp,
                "customer_id": _customer_id(),
                "event_type": "llm_request",
                "properties": {
                    "llm_model": name,
                    "input_tokens": str(inp),
                    "cached_input_tokens": str(cached_text if part == "text" else cached_audio),
                    "output_tokens": str(outp),
                },
                "metadata": dict(base),
            },
        )

    return [
        *event("text", model, text_in, total_out - audio_out),
        *event("audio", f"{model}-audio", audio_in, audio_out),
    ]


class _Sender:
    """Buffers events and ships them to SQS. Best-effort by design: a bench run must never
    fail because the ledger is unreachable, but every drop says why."""

    def __init__(self) -> None:
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._client: Any = None

    def _sqs(self) -> Any:
        if self._client is None:
            import boto3  # imported late: the bench runs fine without it

            self._client = boto3.client("sqs")
        return self._client

    def add(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with self._lock:
            self._buffer.extend(events)
            ready = self._buffer if len(self._buffer) >= _FLUSH_AT else None
            if ready is not None:
                self._buffer = []
        if ready:
            self._send(ready)

    def flush(self) -> None:
        with self._lock:
            ready, self._buffer = self._buffer, []
        if ready:
            self._send(ready)

    def _send(self, events: list[dict[str, Any]]) -> None:
        try:
            self._sqs().send_message(
                QueueUrl=_queue_url(),
                MessageBody=json.dumps({"customer_id": _customer_id(), "events": events}),
            )
            logger.info("metering → %d usage events", len(events))
        except Exception:
            self._client = None
            logger.warning("metering send failed, %d usage events dropped", len(events), exc_info=True)


_sender = _Sender()


def record_llm_usage(
    model: str,
    usage: dict[str, Any],
    *,
    span_id: str,
    timestamp: str | None = None,
    source_operation: str = "",
    metadata: dict[str, str] | None = None,
) -> None:
    """Meter one generation. ``usage`` takes the gen_ai key names without the prefix:
    input_tokens, output_tokens, cached_tokens, input_audio_tokens, output_audio_tokens."""
    if not enabled():
        return
    _sender.add(
        build_events(
            model,
            usage,
            span_id=span_id,
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            source_operation=source_operation,
            metadata=metadata,
        )
    )


def meter_llm_usage(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for a call that returns ``(result, (model, usage))``, the shape the metering
    decorators in middleware and text_agent use. The bench's realtime harnesses have no such
    call boundary — their usage arrives as span attributes and is metered by the processor
    below — so this is here for a plain request/response provider call added later."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result, reported = fn(*args, **kwargs)
        model, usage = reported
        record_llm_usage(model, usage, span_id=transaction_id(repr(reported), "call"))
        return result

    return wrapper


def _usage_from(span: ReadableSpan) -> dict[str, Any]:
    attrs = span.attributes or {}
    keys = (
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "input_audio_tokens",
        "output_audio_tokens",
    )
    return {key: attrs.get(f"gen_ai.usage.{key}") for key in keys}


class UsageMeteringProcessor(SpanProcessor):
    """Meters every generation span the tracers emit, so a harness never has to be taught
    about billing and a new one is covered the day it stamps gen_ai.usage."""

    def __init__(self) -> None:
        self._metered_traces: set[int] = set()
        self._warned: set[str] = set()
        self._lock = threading.Lock()

    def on_start(self, span: Any, parent_context: Any = None) -> None:  # pragma: no cover
        pass

    def on_end(self, span: ReadableSpan) -> None:
        try:
            self._on_end(span)
        except Exception:
            logger.warning("metering skipped a span it could not read", exc_info=True)

    def _on_end(self, span: ReadableSpan) -> None:
        attrs = span.attributes or {}
        if attrs.get("gen_ai.usage.input_tokens") is None and attrs.get("gen_ai.usage.output_tokens") is None:
            return

        name = span.name
        trace_id = span.context.trace_id if span.context else 0
        is_generation = name in GENERATION_SPANS or name.startswith(GENERATION_PREFIX)

        if not is_generation:
            if name not in ROOT_SPANS:
                self._warn_unmetered(name)
                return
            with self._lock:
                already = trace_id in self._metered_traces
                self._metered_traces.discard(trace_id)
            if already:
                return  # the generations under this root were metered one by one

        model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model") or ""
        service = ""
        if span.resource is not None:
            service = str(span.resource.attributes.get("service.name") or "")

        metadata = {
            key.replace("mivas.", "mivas_"): str(attrs[key])
            for key in ("mivas.event", "mivas.modality")
            if attrs.get(key) is not None
        }
        record_llm_usage(
            str(model),
            _usage_from(span),
            span_id=f"{span.context.span_id:016x}" if span.context else "",
            timestamp=datetime.fromtimestamp(span.end_time / 1e9, tz=timezone.utc).isoformat()
            if span.end_time
            else None,
            source_operation=service,
            metadata=metadata,
        )
        if is_generation:
            with self._lock:
                self._metered_traces.add(trace_id)

    def _warn_unmetered(self, name: str) -> None:
        with self._lock:
            if name in self._warned:
                return
            self._warned.add(name)
        logger.warning("metering saw usage on unmetered span shape %r; spend under it is not tracked", name)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        _sender.flush()
        return True

    def shutdown(self) -> None:
        _sender.flush()


def processor() -> SpanProcessor | None:
    """The processor to register on the tracer provider, or None when metering is off."""
    if not enabled():
        logger.info("metering off: METRONOME_SQS_QUEUE_URL / METRONOME_CUSTOMER_ID not set")
        return None
    return UsageMeteringProcessor()
