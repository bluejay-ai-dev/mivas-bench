"""Bluejay tracing for the Gemini LiveKit SIP worker.

livekit-agents emits its own OTel span tree (agent_session, agent_turn,
function_tool, ...). Point it at Bluejay OTLP via telemetry.set_tracer_provider,
capture the call's trace id after session.start(), POST the link at shutdown.
No custom spans.
"""

from __future__ import annotations

import logging
import os

import httpx
import opentelemetry.trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

logger = logging.getLogger("mivas.otel.gemini")

DEFAULT_OTLP_ENDPOINT = "https://otlp.getbluejay.ai/v1/traces"
DEFAULT_API_URL = "https://api.getbluejay.ai/v1"

_provider: trace_sdk.TracerProvider | None = None


def setup_otel() -> trace_sdk.TracerProvider | None:
    """Route livekit-agents' spans to Bluejay. No-op without BLUEJAY_API_KEY."""
    global _provider
    key = os.getenv("BLUEJAY_API_KEY")
    if not key or _provider is not None:
        return _provider

    provider = trace_sdk.TracerProvider(
        resource=Resource.create(
            {SERVICE_NAME: os.getenv("BLUEJAY_SERVICE_NAME", "mivas-gemini")}
        )
    )
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                os.getenv("BLUEJAY_OTLP_ENDPOINT") or DEFAULT_OTLP_ENDPOINT,
                headers={"X-API-KEY": key},
            )
        )
    )

    from livekit.agents.telemetry import set_tracer_provider

    set_tracer_provider(provider)
    otel_trace.set_tracer_provider(provider)
    _provider = provider
    logger.info("livekit telemetry → bluejay otlp")
    return provider


def capture_trace() -> str | None:
    """Trace id of the current span; call right after session.start()."""
    ctx = otel_trace.get_current_span().get_span_context()
    if ctx.is_valid:
        tid = format(ctx.trace_id, "032x")
        logger.info("trace_id=%s", tid)
        return tid
    logger.warning("no current span to capture a trace id from")
    return None


async def link(simulation_result_id: str | None, trace_id: str | None) -> None:
    """Flush spans and POST the trace link once. Use as a shutdown callback."""
    if _provider is None:
        return
    try:
        _provider.force_flush()
    except Exception as e:
        logger.error("force_flush failed: %s", e)
    if not simulation_result_id or not trace_id:
        logger.warning(
            "skip update-simulation-result — sim=%s trace=%s", simulation_result_id, trace_id
        )
        return

    api_url = (os.getenv("BLUEJAY_API_URL") or DEFAULT_API_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(
                f"{api_url}/update-simulation-result",
                json={
                    "simulation_result_id": str(simulation_result_id),
                    "trace_ids": [trace_id],
                },
                headers={
                    "X-API-Key": os.getenv("BLUEJAY_API_KEY"),
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            logger.info(
                "update-simulation-result ok trace=%s sim=%s", trace_id, simulation_result_id
            )
        except Exception as e:
            logger.error("update-simulation-result FAILED: %s", e)
