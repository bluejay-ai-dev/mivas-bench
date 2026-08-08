"""OpenTelemetry → Bluejay OTLP for OpenAI Realtime harnesses.

OpenAI Realtime does **not** emit Agents SDK local spans (function/speech/…),
so openai-agents-opentelemetry only ever produced an empty workflow root.
We create the OTel tree ourselves:

  voice.call (root)
    └── execute_tool <name>   (gen_ai.tool.* — Bluejay-readable)

Then POST {simulation_result_id, trace_ids:[tid]} after the call.
Chirp supplies simulation_result_id via X-Simulation-Result-Id on upgrade.
No bluejay-sdk / BluejayTracing. No tool_calls API posts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Iterator

import httpx
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

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
_root_span: ContextVar[Span | None] = ContextVar("mivas_otel_root", default=None)
_call_t0: ContextVar[float | None] = ContextVar("mivas_otel_t0", default=None)


def _api_url() -> str:
    return os.environ.get("BLUEJAY_API_URL", DEFAULT_API_URL).rstrip("/")


def _otlp_endpoint() -> str:
    return os.environ.get("BLUEJAY_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)


def _service_name() -> str:
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-openai")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


def _json_attr(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def setup_otel() -> TracerProvider | None:
    """Install global OTel exporter to Bluejay OTLP. No-op without BLUEJAY_API_KEY."""
    global _provider

    api_key = _api_key()
    if not api_key:
        return None

    if _provider is not None:
        return _provider

    endpoint = _otlp_endpoint()
    resource = Resource.create({SERVICE_NAME: _service_name()})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(endpoint, headers={"X-API-KEY": api_key})
        )
    )
    otel_trace.set_tracer_provider(provider)
    _provider = provider
    logger.info("otel → %s service=%s", endpoint, _service_name())
    return provider


def flush() -> None:
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as e:
            logger.error("otel flush failed: %s", e)


@contextmanager
def tool_span(
    name: str,
    parameters: Any = None,
    *,
    call_id: str | None = None,
) -> Iterator[Span | None]:
    """Child span under the active voice.call root. No-op outside traced_run."""
    parent = _root_span.get()
    if parent is None or not parent.get_span_context().is_valid:
        yield None
        return

    tracer = otel_trace.get_tracer("mivas.openai")
    parent_ctx = otel_trace.set_span_in_context(parent)
    t0 = _call_t0.get()
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.arguments": _json_attr(parameters if parameters is not None else {}),
    }
    if call_id:
        attrs["gen_ai.tool.call.id"] = str(call_id)
    if t0 is not None:
        attrs["bluejay.tool.start_offset_ms"] = int((time.monotonic() - t0) * 1000)

    with tracer.start_as_current_span(
        f"execute_tool {name}",
        context=parent_ctx,
        kind=SpanKind.CLIENT,
        attributes=attrs,
    ) as span:
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:400]))
            raise


def finish_tool_span(span: Span | None, output: Any) -> None:
    if span is None:
        return
    span.set_attribute("gen_ai.tool.call.result", _json_attr(output))
    span.set_status(Status(StatusCode.OK))


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
    """Link OTel trace to the Chirp/sim result. trace_ids only — never tool_calls."""
    key = _api_key()
    if not key or not simulation_result_id or not trace_id:
        logger.warning(
            "skip update-simulation-result — "
            "simulation_result_id=%s trace_id=%s key=%s",
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
    """OTel voice.call root for a realtime session; flush + link trace_ids on exit."""
    provider = setup_otel()
    if provider is None:
        yield
        return

    tracer = otel_trace.get_tracer("mivas.openai")
    attrs: dict[str, Any] = {
        "gen_ai.system": "openai.realtime",
        "mivas.workflow.name": workflow_name,
    }
    if simulation_result_id:
        attrs["bluejay.simulation_result_id"] = str(simulation_result_id)

    otel_tid: str | None = None
    root_token = None
    t0_token = _call_t0.set(time.monotonic())
    try:
        with tracer.start_as_current_span(
            "voice.call",
            kind=SpanKind.SERVER,
            attributes=attrs,
        ) as root:
            root_token = _root_span.set(root)
            ctx = root.get_span_context()
            if ctx.is_valid:
                otel_tid = format(ctx.trace_id, "032x")
                logger.info(
                    "otel trace_id=%s sim=%s workflow=%s",
                    otel_tid,
                    simulation_result_id,
                    workflow_name,
                )
            try:
                yield
            except Exception as e:
                # Normal CHIRP/OpenAI close after end_call — not a failed call.
                if type(e).__name__.startswith("ConnectionClosed"):
                    root.set_status(Status(StatusCode.OK))
                else:
                    raise
    finally:
        if root_token is not None:
            _root_span.reset(root_token)
        _call_t0.reset(t0_token)
        flush()
        if simulation_result_id and otel_tid:
            await post_trace_ids(simulation_result_id, otel_tid)
        elif simulation_result_id and not otel_tid:
            logger.error(
                "have simulation_result_id=%s but no otel trace id to post",
                simulation_result_id,
            )
