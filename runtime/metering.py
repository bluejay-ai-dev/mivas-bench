"""Reports provider usage to the Bluejay usage metering queue.

Usage is read off the spans the tracers already stamp: a span processor registered beside the
OTLP exporter turns `gen_ai.usage.*` into `llm_request` events, so no harness has to know
anything about billing.

Inert unless METRONOME_SQS_QUEUE_URL and METRONOME_CUSTOMER_ID are set.
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
from urllib.parse import urlparse
from uuid import uuid4

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

logger = logging.getLogger(__name__)

SOURCE_SERVICE = "mivas_bench"

GENERATION_SPANS = {"model", "agent_turn", "realtime_inference"}
GENERATION_PREFIX = "chat "
ROOT_SPANS = {"voice.call"}

_FLUSH_AT = 100
_FLUSH_TIMEOUT = 5.0
_MAX_TRACKED_TRACES = 50_000
_MAX_TOKENS = 2**32 - 1
_MAX_ATTR = 256


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def enabled() -> bool:
    return bool(_env("METRONOME_SQS_QUEUE_URL") and _env("METRONOME_CUSTOMER_ID"))


def transaction_id(span_id: str, part: str) -> str:
    """Matches the dashboard's replay derivation, so a span metered live and the same span
    replayed out of otel_traces collapse onto one id."""
    h = hashlib.sha256(f"mivas:{span_id}:{part}".encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _clip(value: Any) -> str:
    s = str(value)
    return s if len(s) <= _MAX_ATTR else s[:_MAX_ATTR]


def _int(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return min(max(n, 0), _MAX_TOKENS)


def build_events(
    model: str,
    usage: dict[str, Any],
    *,
    span_id: str,
    timestamp: str,
    source_operation: str = "",
    metadata: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One generation becomes up to two events.

    cost_events has no audio column and realtime audio input bills several times text, so audio
    rides under `{model}-audio`. Anything not broken out as audio counts as text, so the two
    still sum to what the provider reported.

    input_tokens goes out cached-inclusive: the metering handler narrows it before ingest, and
    subtracting here would drop the cached tokens twice.
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

    model = _clip(model)
    base: dict[str, str] = {
        "unmetered": "true",
        "source_service": SOURCE_SERVICE,
        "span_id": _clip(span_id),
        **{k: _clip(v) for k, v in (metadata or {}).items()},
    }
    if source_operation:
        base["source_operation"] = _clip(source_operation)
    if _env("METRONOME_ORG_ID"):
        base["org_id"] = _env("METRONOME_ORG_ID")

    def event(part: str, name: str, inp: int, outp: int) -> Iterable[dict[str, Any]]:
        if inp + outp == 0:
            return ()
        return (
            {
                "transaction_id": transaction_id(span_id, part),
                "timestamp": timestamp,
                "customer_id": _env("METRONOME_CUSTOMER_ID"),
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
    """Buffers events and sends them from a worker thread.

    Never from the calling thread: spans end on the asyncio loop, and a blocking export from
    there is what starved the loop before the tracers moved to BatchSpanProcessor.

    A bench run must never fail because the ledger is unreachable, so every failure is a
    logged drop.
    """

    def __init__(self) -> None:
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._client: Any = None
        self._pool: Any = None
        self._pending: set[Any] = set()

    def _sqs(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            host = urlparse(_env("METRONOME_SQS_QUEUE_URL")).hostname or ""
            parts = host.split(".")
            key, secret = _env("METRONOME_AWS_ACCESS_KEY_ID"), _env("METRONOME_AWS_SECRET_ACCESS_KEY")
            self._client = boto3.client(
                "sqs",
                # the queue names its own region; the default chain points wherever the bench
                # happens to run snapshots
                region_name=parts[1] if len(parts) > 3 and parts[0] == "sqs" else None,
                # metering holds its own narrow credentials where it has them, rather than
                # signing the billing path with whatever the harness uses for its provider
                aws_access_key_id=key or None,
                aws_secret_access_key=secret or None,
                config=Config(
                    connect_timeout=3,
                    read_timeout=5,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
        return self._client

    def _submit(self, events: list[dict[str, Any]]) -> None:
        from concurrent.futures import ThreadPoolExecutor

        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metering")
            future = self._pool.submit(self._send, events)
            self._pending.add(future)
        future.add_done_callback(lambda f: self._pending.discard(f))

    def add(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with self._lock:
            self._buffer.extend(events)
            ready = None
            if len(self._buffer) >= _FLUSH_AT:
                ready, self._buffer = self._buffer, []
        if ready:
            self._submit(ready)

    def flush(self, timeout: float = _FLUSH_TIMEOUT) -> None:
        """Bounded: the caller is usually a harness about to post its trace ids, and metering is
        never worth delaying that."""
        with self._lock:
            ready, self._buffer = self._buffer, []
        if ready:
            self._submit(ready)
        for future in list(self._pending):
            try:
                future.result(timeout=timeout)
            except Exception:
                pass

    def shutdown(self) -> None:
        self.flush()
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)

    def _send(self, events: list[dict[str, Any]]) -> None:
        try:
            self._sqs().send_message(
                QueueUrl=_env("METRONOME_SQS_QUEUE_URL"),
                MessageBody=json.dumps({"customer_id": _env("METRONOME_CUSTOMER_ID"), "events": events}),
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
    """`usage` takes the gen_ai key names without the prefix: input_tokens, output_tokens,
    cached_tokens, input_audio_tokens, output_audio_tokens."""
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
    """For a call returning `(result, (model, usage))`. The realtime harnesses have no such call
    boundary, so their usage is metered by the processor below."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result, (model, usage) = fn(*args, **kwargs)
        record_llm_usage(model, usage, span_id=uuid4().hex)
        return result

    return wrapper


def _usage_from(span: ReadableSpan) -> dict[str, Any]:
    attrs = span.attributes or {}
    keys = ("input_tokens", "output_tokens", "cached_tokens", "input_audio_tokens", "output_audio_tokens")
    return {key: attrs.get(f"gen_ai.usage.{key}") for key in keys}


class UsageMeteringProcessor(SpanProcessor):
    def __init__(self) -> None:
        self._metered_traces: set[int] = set()
        self._warned: set[str] = set()
        self._lock = threading.Lock()

    def on_start(self, span: Any, parent_context: Any = None) -> None:
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

        # A root holds a rollup of the generations beneath it, so it is only the right row for
        # the cascaded harnesses, which report no per-generation span at all.
        if not is_generation:
            if name not in ROOT_SPANS:
                self._warn_unmetered(name)
                return
            with self._lock:
                already = trace_id in self._metered_traces
                self._metered_traces.discard(trace_id)
            if already:
                return

        service = ""
        if span.resource is not None:
            service = str(span.resource.attributes.get("service.name") or "")

        record_llm_usage(
            str(attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model") or ""),
            _usage_from(span),
            span_id=f"{span.context.span_id:016x}" if span.context else "",
            timestamp=datetime.fromtimestamp(span.end_time / 1e9, tz=timezone.utc).isoformat()
            if span.end_time
            else None,
            source_operation=service,
            metadata={
                key.replace("mivas.", "mivas_"): str(attrs[key])
                for key in ("mivas.event", "mivas.modality")
                if attrs.get(key) is not None
            },
        )
        if is_generation:
            with self._lock:
                if len(self._metered_traces) >= _MAX_TRACKED_TRACES:
                    # only reachable via calls killed before their root span ended
                    self._metered_traces.clear()
                    logger.warning("metering trace table full; cleared")
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
        _sender.shutdown()


def processor() -> SpanProcessor | None:
    if not enabled():
        logger.info("metering off: METRONOME_SQS_QUEUE_URL / METRONOME_CUSTOMER_ID not set")
        return None
    return UsageMeteringProcessor()
