"""Bluejay tracing for the Gemini LiveKit SIP worker.

livekit-agents emits its own OTel span tree (agent_session, agent_turn,
function_tool, ...). Point it at Bluejay OTLP via telemetry.set_tracer_provider,
capture the call's trace id after session.start(), POST the link at shutdown.
No custom spans.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx
import opentelemetry.trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger("mivas.otel.gemini")

DEFAULT_OTLP_ENDPOINT = "https://otlp.getbluejay.ai/v1/traces"
DEFAULT_API_URL = "https://api.getbluejay.ai/v1"

_provider: trace_sdk.TracerProvider | None = None
FINAL_STATUSES = {"COMPLETED", "FAILED", "SYSTEM_ERROR", "NO_ANSWER", "CANCELLED", "NO_CONNECTION"}


def setup_otel() -> trace_sdk.TracerProvider | None:
    """Route livekit-agents' spans to Bluejay. No-op without BLUEJAY_API_KEY.

    Call at every job start: livekit shuts the active tracer provider down at
    job end (_shutdown_telemetry), so a shared provider exports nothing after
    the pod's first call ("Exporter already shutdown, ignoring batch").
    """
    global _provider
    key = os.getenv("BLUEJAY_API_KEY")
    if not key:
        return None

    provider = trace_sdk.TracerProvider(
        resource=Resource.create(
            {SERVICE_NAME: os.getenv("BLUEJAY_SERVICE_NAME", "mivas-gemini")}
        )
    )
    # Batch: the simple processor posts every span synchronously on the agent
    # loop ("event loop blocked for 180ms" during calls). link() force-flushes.
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                os.getenv("BLUEJAY_OTLP_ENDPOINT") or DEFAULT_OTLP_ENDPOINT,
                headers={"X-API-KEY": key},
            ),
            schedule_delay_millis=500,
        )
    )

    from livekit.agents.telemetry import set_tracer_provider

    set_tracer_provider(provider)
    if _provider is None:
        # global provider is set-once in OTel; livekit's tracer is re-settable
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
    """Flush spans, then POST the trace link once the result is final.

    Bluejay extracts the call's tool calls at the moment trace_ids land, from
    whatever spans its store has ingested by then. Ingestion lags the OTLP POST
    by seconds to tens of seconds, so linking straight from the shutdown
    callback dropped the last turn's tools (a tool batched with end_call or
    escalate_to_human) on 8 of 30 smoke calls; a relink later counted them.
    Same recipe as grok/report.py: wait for a final status (eval takes about
    three minutes, which is more than enough), POST once. Off the job loop.
    """
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
    threading.Thread(
        target=_link_when_final,
        args=(str(simulation_result_id), trace_id, os.getenv("BLUEJAY_API_KEY")),
        daemon=True,
    ).start()


def _link_when_final(simulation_result_id: str, trace_id: str, key: str) -> None:
    api_url = (os.getenv("BLUEJAY_API_URL") or DEFAULT_API_URL).rstrip("/")
    headers = {"X-API-Key": key}
    deadline = time.monotonic() + float(os.getenv("BLUEJAY_LINK_WAIT_S", "900"))
    status = None
    with httpx.Client(timeout=15) as client:
        while time.monotonic() < deadline:
            try:
                r = client.get(f"{api_url}/retrieve-simulation-result/{simulation_result_id}", headers=headers)
                if r.status_code == 200:
                    status = str((r.json().get("simulation_result") or {}).get("status"))
                    if status in FINAL_STATUSES:
                        break
            except Exception as e:
                logger.warning("status poll failed sim=%s: %s", simulation_result_id, e)
            time.sleep(5)
        else:
            logger.error("linking before a final status (%s) sim=%s", status, simulation_result_id)
        for attempt in range(4):
            try:
                r = client.post(
                    f"{api_url}/update-simulation-result",
                    json={"simulation_result_id": simulation_result_id, "trace_ids": [trace_id]},
                    headers={**headers, "Content-Type": "application/json"},
                )
                r.raise_for_status()
                logger.info(
                    "update-simulation-result ok trace=%s sim=%s status=%s", trace_id, simulation_result_id, status
                )
                return
            except Exception as e:
                logger.error("update-simulation-result FAILED (attempt %d): %s", attempt + 1, e)
                time.sleep(5)
