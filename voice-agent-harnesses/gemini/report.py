"""OpenTelemetry → Bluejay OTLP for Gemini Live harnesses.

Gemini Live has no Agents-SDK span tree, so we emit GenAI-native spans:

  voice.call (root) — gen_ai.provider.name=gcp.gemini
    ├── agent.speech          (TTS / agent audio turns)
    └── execute_tool <name>   (gen_ai.tool.*)

After the call we POST to update-simulation-result with:
  - trace_ids  → waterfall flamegraph
  Conversation tool markers come from execute_tool OTel spans (not a tool_calls POST).

Chirp supplies simulation_result_id via X-Simulation-Result-Id on upgrade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
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

logger = logging.getLogger("mivas.otel.gemini")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

DEFAULT_OTLP_ENDPOINT = "https://otlp.getbluejay.ai/v1/traces"
DEFAULT_API_URL = "https://api.getbluejay.ai/v1"
# Bluejay may clear trace_ids during EVALUATING → COMPLETED; re-link after these.
FINAL_STATUSES = {
    "COMPLETED",
    "FAILED",
    "SYSTEM_ERROR",
    "NO_ANSWER",
    "CANCELLED",
    "NO_CONNECTION",
}
EARLY_UPSERT_STATUSES = {"EVALUATING", "EVALUATED", "CONVERSATION_ENDED"}
PROVIDER = "gcp.gemini"
TRACER_NAME = "mivas.gemini"

_provider: TracerProvider | None = None
_root_span: ContextVar[Span | None] = ContextVar("mivas_gemini_otel_root", default=None)
_call_t0: ContextVar[float | None] = ContextVar("mivas_gemini_otel_t0", default=None)
_reported_tools: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "mivas_gemini_reported_tools", default=None
)
# module fallbacks when asyncio tasks don't inherit ContextVars
_active_root: Span | None = None
_active_t0: float | None = None
_active_tools: list[dict[str, Any]] | None = None


def _api_url() -> str:
    return (os.environ.get("BLUEJAY_API_URL") or DEFAULT_API_URL).rstrip("/")


def _otlp_endpoint() -> str:
    return os.environ.get("BLUEJAY_OTLP_ENDPOINT") or DEFAULT_OTLP_ENDPOINT


def _service_name() -> str:
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-gemini")


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


def record_tool_call(
    name: str,
    parameters: Any,
    output: Any,
    *,
    start_offset_ms: int | None = None,
) -> None:
    """Buffer a tool call for the end-of-call update-simulation-result POST."""
    tools = _reported_tools.get()
    if tools is None:
        tools = _active_tools
    if tools is None:
        return
    tools.append(
        {
            "name": str(name),
            "parameters": parameters if isinstance(parameters, dict) else {"raw": parameters},
            "output": output,
            "start_offset_ms": int(
                start_offset_ms if start_offset_ms is not None else call_offset_ms()
            ),
        }
    )


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

    tracer = otel_trace.get_tracer(TRACER_NAME)
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
    name: str | None = None,
    parameters: Any = None,
    start_offset_ms: int | None = None,
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
    tracer = otel_trace.get_tracer(TRACER_NAME)
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


def _upsert_stop_statuses() -> set[str]:
    """Statuses that release the pre-POST wait.

    Default waits for a final status so the link is not wiped mid-eval. With
    MIVAS_UPSERT_BEFORE_EVAL set, the POST goes out as soon as the conversation ends, so
    the goal evaluator can see the call's tool calls — at the cost of a possible
    double-count of execute_tool spans if eval then wipes the link and the relink fires.
    """
    if os.environ.get("MIVAS_UPSERT_BEFORE_EVAL", "").strip().lower() in ("1", "true", "yes"):
        return FINAL_STATUSES | {"CONVERSATION_ENDED"}
    return FINAL_STATUSES


async def _await_terminal_upsert(
    client: httpx.AsyncClient, simulation_result_id: str, timeout: float = 300.0
) -> str | None:
    """Wait for a *final* status, then POST once.

    Not TERMINAL_STATUSES (it counts EVALUATING) and not 18 s: posting mid-eval gets
    trace_ids wiped, and the relink then re-extracts every execute_tool span on top of
    the first POST's, so each tool lands twice. Eval needs ~175 s; CHIRP has no session
    cap, so the wait is free.
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
                if st in _upsert_stop_statuses():
                    return st
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None


async def post_simulation_enrichment(
    simulation_result_id: str,
    *,
    trace_id: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> None:
    """Link OTel trace_ids. tool_calls ignored — use execute_tool spans."""
    del tool_calls
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
    if "trace_ids" not in body and "tool_calls" not in body:
        logger.warning("skip update-simulation-result — nothing to post")
        return

    await asyncio.sleep(0.5)

    last_err: str | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                st = await _await_terminal_upsert(client, simulation_result_id)
                if st in EARLY_UPSERT_STATUSES:
                    # Bluejay extracts execute_tool spans at POST time, so posting the
                    # instant the call ends can beat the OTLP spans into the store and link
                    # a trace with no tools. force_flush() only proves the exporter accepted
                    # them. Settle first — still inside the window before eval reads tools.
                    await asyncio.sleep(
                        float(os.environ.get("MIVAS_UPSERT_SETTLE_SECONDS", "10"))
                    )
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
                    if (st is None or st in EARLY_UPSERT_STATUSES) and trace_id:
                        await _relink_after_final(
                            client, simulation_result_id, body, key, trace_id, st
                        )
                    return
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.error(
                "update-simulation-result error attempt=%s %s", attempt, last_err
            )
        await asyncio.sleep(1.0 * attempt)
    logger.error("update-simulation-result gave up: %s", last_err)


async def _relink_after_final(
    client: httpx.AsyncClient,
    simulation_result_id: str,
    body: dict[str, Any],
    key: str,
    trace_id: str,
    early_status: str | None,
) -> None:
    """Re-POST trace_ids once the sim leaves EVALUATING (eval can wipe the link).

    Only if it actually got wiped: each POST re-extracts the execute_tool spans and
    appends them, so a redundant relink double-counts every tool on the timeline.
    """
    final: str | None = None
    linked = False
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        try:
            r = await client.get(
                f"{_api_url()}/retrieve-simulation-result/{simulation_result_id}",
                headers={"X-API-Key": key},
            )
            if r.status_code == 200:
                result = (r.json() or {}).get("simulation_result") or {}
                st = str(result.get("status"))
                if st in FINAL_STATUSES:
                    final = st
                    linked = trace_id in (result.get("trace_ids") or [])
                    break
        except Exception:
            pass
        await asyncio.sleep(2.0)
    if final is not None and linked:
        logger.info(
            "relink not needed after %s — trace=%s still linked sim=%s final=%s",
            early_status,
            trace_id,
            simulation_result_id,
            final,
        )
        return
    if final is None:
        logger.warning(
            "relink skipped — still not final after early upsert terminal=%s sim=%s",
            early_status,
            simulation_result_id,
        )
        return
    r = await client.post(
        f"{_api_url()}/update-simulation-result",
        json=body,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    if r.status_code >= 400:
        logger.error(
            "relink after %s FAILED sim=%s %s %s",
            early_status,
            simulation_result_id,
            r.status_code,
            r.text[:300],
        )
    else:
        logger.info(
            "relink after %s ok trace=%s sim=%s final=%s",
            early_status,
            trace_id,
            simulation_result_id,
            final,
        )


# back-compat alias used by older call sites
async def post_trace_ids(simulation_result_id: str, trace_id: str) -> None:
    await post_simulation_enrichment(
        simulation_result_id, trace_id=trace_id, tool_calls=_reported_tools.get()
    )


@asynccontextmanager
async def traced_run(
    workflow_name: str,
    *,
    simulation_result_id: str | None = None,
    model: str | None = None,
) -> AsyncIterator[None]:
    """OTel voice.call root; flush + link trace_ids/tool_calls on exit."""
    global _active_root, _active_t0, _active_tools

    provider = setup_otel()
    if provider is None:
        yield
        return

    tracer = otel_trace.get_tracer(TRACER_NAME)
    attrs: dict[str, Any] = {
        # Gemini / GenAI semantic conventions (not openai.realtime)
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
    tools_token = None
    t0 = time.monotonic()
    t0_token = _call_t0.set(t0)
    tool_buf: list[dict[str, Any]] = []
    prev_active = _active_root
    prev_t0 = _active_t0
    prev_tools = _active_tools
    try:
        with tracer.start_as_current_span(
            "voice.call",
            kind=SpanKind.SERVER,
            attributes=attrs,
        ) as root:
            root_token = _root_span.set(root)
            tools_token = _reported_tools.set(tool_buf)
            _active_root = root
            _active_t0 = t0
            _active_tools = tool_buf
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
        _active_tools = prev_tools
        if root_token is not None:
            _root_span.reset(root_token)
        if tools_token is not None:
            _reported_tools.reset(tools_token)
        _call_t0.reset(t0_token)
        flush()
        if simulation_result_id and (otel_tid or tool_buf):
            try:
                await post_simulation_enrichment(
                    simulation_result_id,
                    trace_id=otel_tid,
                                    )
            except Exception as e:
                logger.error(
                    "post_simulation_enrichment crashed: %s: %s",
                    type(e).__name__,
                    e,
                )
        elif simulation_result_id and not otel_tid:
            logger.error(
                "have simulation_result_id=%s but no otel trace id to post",
                simulation_result_id,
            )
