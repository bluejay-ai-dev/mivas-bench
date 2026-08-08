"""OpenAI Agents → OpenTelemetry → Bluejay OTLP, then link via update-simulation-result.

No bluejay-sdk / BluejayTracing (LiveKit-specific). No tool_calls API posts.

Stack:
  agents.trace() → openai-agents-opentelemetry → OTel TracerProvider
  → OTLP HTTP → BLUEJAY_OTLP_ENDPOINT
  → POST /v1/update-simulation-result {simulation_result_id, trace_ids:[tid]}

Chirp supplies simulation_result_id as X-Simulation-Result-Id on the
WebSocket upgrade (Bluejay CHIRP docs).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from agents import add_trace_processor, trace
from openai_agents_opentelemetry import OpenTelemetryTracingProcessor
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

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
_processor: "_CapturingOTelProcessor | None" = None
_processor_registered = False


class _CapturingOTelProcessor(OpenTelemetryTracingProcessor):
    """Agents→OTel bridge; records the OTel root trace id per Agents workflow.

    The stock processor does not attach roots to the global OTel context, so we
    read the root span it creates.
    """

    def __init__(self) -> None:
        super().__init__(tracer_name="openai.agents")
        self.otel_trace_ids: dict[str, str] = {}

    def on_trace_start(self, agent_trace: Any) -> None:
        super().on_trace_start(agent_trace)
        agents_tid = getattr(agent_trace, "trace_id", None)
        if not agents_tid:
            return
        with self._lock:
            span = self._trace_root_spans.get(agents_tid)
        if span is None:
            return
        ctx = span.get_span_context()
        if ctx.is_valid:
            self.otel_trace_ids[agents_tid] = format(ctx.trace_id, "032x")


def _api_url() -> str:
    return os.environ.get("BLUEJAY_API_URL", DEFAULT_API_URL).rstrip("/")


def _otlp_endpoint() -> str:
    return os.environ.get("BLUEJAY_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)


def _service_name() -> str:
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-openai")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


def setup_otel() -> TracerProvider | None:
    """Install global OTel exporter to Bluejay OTLP. No-op without BLUEJAY_API_KEY."""
    global _provider, _processor, _processor_registered

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

    if not _processor_registered:
        _processor = _CapturingOTelProcessor()
        add_trace_processor(_processor)
        _processor_registered = True

    logger.info("otel → %s service=%s", endpoint, _service_name())
    return provider


def flush() -> None:
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as e:
            logger.error("otel flush failed: %s", e)


def _otel_trace_id_for_agents(agents_trace_id: str) -> str | None:
    if _processor is None:
        return None
    return _processor.otel_trace_ids.get(agents_trace_id)


async def _await_terminal_upsert(
    client: httpx.AsyncClient, simulation_result_id: str, timeout: float = 18.0
) -> str | None:
    """Wait until Bluejay has written its terminal row so our POST is not overwritten."""
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
    """Agents workflow → OTLP; on exit flush and POST trace_ids when sim id is set."""
    provider = setup_otel()
    if provider is None:
        yield
        return

    agents_tid: str | None = None
    otel_tid: str | None = None
    try:
        with trace(workflow_name=workflow_name) as t:
            agents_tid = t.trace_id
            otel_tid = _otel_trace_id_for_agents(agents_tid)
            if otel_tid:
                logger.info(
                    "otel trace_id=%s agents=%s sim=%s",
                    otel_tid,
                    agents_tid,
                    simulation_result_id,
                )
            yield
    finally:
        flush()
        if simulation_result_id and otel_tid:
            await post_trace_ids(simulation_result_id, otel_tid)
        elif simulation_result_id and not otel_tid:
            logger.error(
                "have simulation_result_id=%s but no otel trace id to post",
                simulation_result_id,
            )
