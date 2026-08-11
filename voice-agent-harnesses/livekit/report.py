"""OpenTelemetry → Bluejay OTLP for the LiveKit harness.

Cloned from `vapi/report.py` (the corrected single-POST-after-final version).
Two LiveKit-specific changes:

* the exporter is attached to a **private** TracerProvider instead of the global
  one, so livekit-agents' own instrumentation does not dump its internal span
  tree into Bluejay alongside ours;
* `execute_tool` spans come from the in-process `@function_tool` bodies, and
  speech spans from real `agent_state_changed` / `user_state_changed` events.

Span tree:

  voice.call (root) — gen_ai.provider.name=livekit
    ├── agent.speech          (AgentSession agent_state_changed → speaking)
    ├── customer.speech       (AgentSession user_state_changed → speaking)
    └── execute_tool <name>   (gen_ai.tool.*; emitted by harness.run_tool)

After the call we POST to update-simulation-result with:
  - trace_ids  → waterfall flamegraph
  Conversation tool markers come from execute_tool OTel spans (not a tool_calls POST).

Bluejay supplies simulation_result_id via X-Simulation-Result-Id on the LiveKit
job metadata (native LIVEKIT dispatch — there is no CHIRP bridge here).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
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

logger = logging.getLogger("mivas.otel.livekit")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

DEFAULT_OTLP_ENDPOINT = "https://otlp.getbluejay.ai/v1/traces"
DEFAULT_API_URL = "https://api.getbluejay.ai/v1"
FINAL_STATUSES = {
    "COMPLETED",
    "FAILED",
    "SYSTEM_ERROR",
    "NO_ANSWER",
    "CANCELLED",
    "NO_CONNECTION",
}
PROVIDER = "livekit"
TRACER_NAME = "mivas.livekit"

_provider: TracerProvider | None = None
_root_span: ContextVar[Span | None] = ContextVar("mivas_livekit_otel_root", default=None)
_call_t0: ContextVar[float | None] = ContextVar("mivas_livekit_otel_t0", default=None)
# module fallbacks when asyncio tasks don't inherit ContextVars
_active_root: Span | None = None
_active_t0: float | None = None


def _api_url() -> str:
    return os.environ.get("BLUEJAY_API_URL", DEFAULT_API_URL).rstrip("/")


def _otlp_endpoint() -> str:
    return os.environ.get("BLUEJAY_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)


def _service_name() -> str:
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-livekit")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


def _json_attr(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def call_offset_ms() -> int:
    t0 = _call_t0.get()
    if t0 is None:
        t0 = _active_t0
    if t0 is None:
        return 0
    return max(0, int((time.monotonic() - t0) * 1000))


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
    # deliberately NOT otel_trace.set_tracer_provider(): livekit-agents resolves
    # its own tracer off the global provider and would export its whole internal
    # span tree to Bluejay as unrelated traces.
    _provider = provider
    logger.info("otel → %s service=%s", endpoint, _service_name())
    return provider


def flush() -> None:
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as e:
            logger.error("otel flush failed: %s", e)


def _tracer():
    return (_provider or otel_trace.get_tracer_provider()).get_tracer(TRACER_NAME)


def _parent_span() -> Span | None:
    parent = _root_span.get()
    if parent is not None and parent.get_span_context().is_valid:
        return parent
    if _active_root is not None and _active_root.get_span_context().is_valid:
        return _active_root
    cur = otel_trace.get_current_span()
    if cur is not None and cur.get_span_context().is_valid:
        return cur
    return None


@contextmanager
def tool_span(
    name: str,
    parameters: Any = None,
    *,
    call_id: str | None = None,
) -> Iterator[Span | None]:
    """Child span under the active voice.call root. No-op outside traced_run."""
    parent = _parent_span()
    if parent is None:
        yield None
        return

    tracer = _tracer()
    parent_ctx = otel_trace.set_span_in_context(parent)
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.arguments": _json_attr(parameters if parameters is not None else {}),
        # conversation timestamps come from span start vs voice.call (OTel extraction)
    }
    if call_id:
        attrs["gen_ai.tool.call.id"] = str(call_id)

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


def finish_tool_span(
    span: Span | None,
    output: Any,
    *,
    ok: bool = True,
) -> None:
    if span is not None:
        span.set_attribute("gen_ai.tool.call.result", _json_attr(output))
        if ok:
            span.set_status(Status(StatusCode.OK))
        else:
            span.set_status(Status(StatusCode.ERROR, _json_attr(output)[:400]))


def start_speech_span(
    utterance_id: str, *, speaker: str = "agent"
) -> Span | None:
    """Begin agent.speech or customer.speech under voice.call."""
    parent = _parent_span()
    if parent is None:
        return None
    tracer = _tracer()
    parent_ctx = otel_trace.set_span_in_context(parent)
    is_customer = speaker in ("customer", "user", "digital_human")
    span_name = "customer.speech" if is_customer else "agent.speech"
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": (
            "speech_to_text" if is_customer else "text_to_speech"
        ),
        "gen_ai.provider.name": PROVIDER,
        "mivas.utterance_id": str(utterance_id),
        "mivas.speech.speaker": "customer" if is_customer else "agent",
        "bluejay.speech.start_offset_ms": call_offset_ms(),
    }
    span = tracer.start_span(
        span_name,
        context=parent_ctx,
        kind=SpanKind.INTERNAL,
        attributes=attrs,
    )
    return span

def end_speech_span(span: Span | None) -> None:
    if span is None:
        return
    span.set_attribute("bluejay.speech.end_offset_ms", call_offset_ms())
    span.set_status(Status(StatusCode.OK))
    span.end()


async def _await_terminal_upsert(
    client: httpx.AsyncClient, simulation_result_id: str, timeout: float = 600.0
) -> str | None:
    """Wait for a *final* status before the upsert.

    600 s, not the 150 s the CHIRP harnesses use: this runs on a detached thread
    with no job deadline, and the wall clock we have to beat is our own hangup to
    Bluejay's COMPLETED — which includes the ~2 min the simulation can linger after
    we hang up, plus evaluation. Result 712617 timed out at 300 s, posted during
    EVALUATING, and evaluation then wiped its trace_ids: a linked-then-wiped run is
    unrecoverable, because re-posting is what double-counts every tool.

    Posting during EVALUATING works, but eval then wipes trace_ids, and re-posting
    makes Bluejay re-extract the execute_tool spans on top of the ones the first
    POST already produced — every tool lands on the timeline twice. One POST after
    the sim settles gives a surviving link and one row per tool. On timeout we post
    anyway and log it; we never re-post, because that is the double-count.
    """
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
                if st in FINAL_STATUSES:
                    return st
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None


async def post_simulation_enrichment(
    simulation_result_id: str,
    *,
    trace_id: str | None,
) -> None:
    """Link OTel trace_ids; conversation tools come from execute_tool spans."""
    key = _api_key()
    if not key or not simulation_result_id:
        logger.warning(
            "skip update-simulation-result — "
            "simulation_result_id=%s key=%s",
            simulation_result_id,
            bool(key),
        )
        return
    if not str(simulation_result_id).isdigit():
        logger.warning(
            "skip update-simulation-result — non-numeric sim id=%s",
            simulation_result_id,
        )
        return

    body: dict[str, Any] = {"simulation_result_id": str(simulation_result_id)}
    if trace_id:
        body["trace_ids"] = [trace_id]
    if "trace_ids" not in body:
        logger.warning("skip update-simulation-result — nothing to post")
        return

    await asyncio.sleep(0.5)

    last_err: str | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                st = await _await_terminal_upsert(client, simulation_result_id)
                if st is None:
                    check = await client.get(
                        f"{_api_url()}/retrieve-simulation-result/{simulation_result_id}",
                        headers={"X-API-Key": key},
                    )
                    if check.status_code == 404:
                        logger.warning(
                            "skip update-simulation-result — sim=%s not found",
                            simulation_result_id,
                        )
                        return
                r = await client.post(
                    f"{_api_url()}/update-simulation-result",
                    json=body,
                    headers={"X-API-Key": key, "Content-Type": "application/json"},
                )
                if r.status_code >= 400:
                    last_err = f"{r.status_code} {r.text[:300]} (status={st})"
                    logger.error(
                        "update-simulation-result FAILED attempt=%s %s",
                        attempt,
                        last_err,
                    )
                    if r.status_code == 404:
                        return
                else:
                    logger.info(
                        "update-simulation-result ok trace=%s sim=%s terminal=%s attempt=%s",
                        trace_id,
                        simulation_result_id,
                        st,
                        attempt,
                    )
                    if st is None:
                        logger.warning(
                            "linked without a final status — sim=%s may still be "
                            "EVALUATING; if eval wipes trace_ids the link is lost "
                            "(we do not re-post: a second POST double-counts tools)",
                            simulation_result_id,
                        )
                    return
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.error(
                "update-simulation-result error attempt=%s %s", attempt, last_err
            )
        await asyncio.sleep(1.0 * attempt)
    logger.error("update-simulation-result gave up: %s", last_err)



@asynccontextmanager
async def traced_run(
    workflow_name: str,
    *,
    simulation_result_id: str | None = None,
    model: str | None = None,
) -> AsyncIterator[None]:
    """OTel voice.call root; flush + link trace_ids/tool_calls on exit."""
    global _active_root, _active_t0

    provider = setup_otel()
    if provider is None:
        yield
        return

    tracer = _tracer()
    attrs: dict[str, Any] = {
        "gen_ai.system": PROVIDER,
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.operation.name": "invoke_agent",
        "mivas.workflow.name": workflow_name,
    }
    if model:
        attrs["gen_ai.request.model"] = model
    if simulation_result_id:
        attrs["bluejay.simulation_result_id"] = str(simulation_result_id)

    otel_tid: str | None = None
    root_token = None
    t0 = time.monotonic()
    t0_token = _call_t0.set(t0)
    prev_active = _active_root
    prev_t0 = _active_t0
    try:
        with tracer.start_as_current_span(
            "voice.call",
            kind=SpanKind.SERVER,
            attributes=attrs,
        ) as root:
            root_token = _root_span.set(root)
            _active_root = root
            _active_t0 = t0
            ctx = root.get_span_context()
            if ctx.is_valid:
                otel_tid = format(ctx.trace_id, "032x")
                logger.info(
                    "otel trace_id=%s sim=%s workflow=%s model=%s",
                    otel_tid,
                    simulation_result_id,
                    workflow_name,
                    model,
                )
            try:
                yield
            except Exception as e:
                if type(e).__name__.startswith("ConnectionClosed"):
                    root.set_status(Status(StatusCode.OK))
                else:
                    raise
    finally:
        _active_root = prev_active
        _active_t0 = prev_t0
        if root_token is not None:
            _root_span.reset(root_token)
        _call_t0.reset(t0_token)
        flush()
        if simulation_result_id and otel_tid:
            # LiveKit cancels the entrypoint ~15 s after the room closes
            # ("entrypoint did not exit in time"), which kills any coroutine still
            # waiting here — and the single POST must not happen until the
            # simulation reaches a FINAL status. A plain non-daemon thread outlives
            # the job and keeps the worker process alive until the link lands.
            threading.Thread(
                target=asyncio.run,
                args=(
                    post_simulation_enrichment(simulation_result_id, trace_id=otel_tid),
                ),
                name=f"mivas-link-{simulation_result_id}",
                daemon=False,
            ).start()
        elif simulation_result_id and not otel_tid:
            logger.error(
                "have simulation_result_id=%s but no otel trace id to post",
                simulation_result_id,
            )
