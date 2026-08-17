"""OpenTelemetry → Bluejay OTLP for Grok / xAI voice harnesses.

The xAI Speech-to-Speech WebSocket has no Agents-SDK span tree, so we emit a
LangSmith-shaped GenAI tree (feed raw events to ``handle_event``):

  realtime_session (root) — gen_ai.system=xai
    └── turn                  (one caller utterance → all ensuing agent activity)
          ├── user_message    (caller transcript)
          ├── model           (one generation per response: gen_ai.usage.* tokens
          │                    + time-to-first-token + output)
          └── execute_tool <name>   (tool calls / handoffs — Bluejay reads these)

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
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv._incubating.attributes import (
    gen_ai_attributes as GenAIAttributes,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

logger = logging.getLogger("mivas.otel.grok")
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
EARLY_UPSERT_STATUSES = {"EVALUATING", "EVALUATED", "CONVERSATION_ENDED"}
PROVIDER = "xai"
TRACER_NAME = "mivas.grok"

_provider: TracerProvider | None = None
_root_span: ContextVar[Span | None] = ContextVar("mivas_grok_otel_root", default=None)
_call_t0: ContextVar[float | None] = ContextVar("mivas_grok_otel_t0", default=None)
_reported_tools: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "mivas_grok_reported_tools", default=None
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
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-grok")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


def _json_attr(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


_MAX_ATTR = 4000


def _clip(value: Any, n: int = _MAX_ATTR) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


def _deep_get(obj: Any, *path: str) -> Any:
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


# Active LangSmith-shaped tracer for the current call; set by traced_run.
_active_tracer: ContextVar["RealtimeEventTracer | None"] = ContextVar(
    "mivas_grok_active_tracer", default=None
)
_active_tracer_mod: "RealtimeEventTracer | None" = None


def active_tracer() -> "RealtimeEventTracer | None":
    t = _active_tracer.get()
    return t if t is not None else _active_tracer_mod


def handle_event(event: Any) -> None:
    """Feed one raw xAI websocket event dict to the active tracer (no-op if none)."""
    t = active_tracer()
    if t is not None and isinstance(event, dict):
        t.handle_raw(event)


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
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint, headers={"X-API-KEY": api_key}),
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
    logger.info("otel → %s service=%s", endpoint, _service_name())
    return provider


def flush() -> None:
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as e:
            logger.error("otel flush failed: %s", e)


class RealtimeEventTracer:
    """xAI websocket events → a LangSmith-shaped tree under ``realtime_session``.

        realtime_session
          turn                    (one caller utterance → all ensuing agent activity)
            user_message          (caller transcript)
            model                 (generation: gen_ai.usage.* tokens + TTFT + output)
            execute_tool <name>   (tool calls / handoffs — Bluejay reads these)

    Turn boundary = ``conversation.item.input_audio_transcription.completed`` (a new
    caller utterance); greetings open a turn lazily on ``response.created``.
    """

    def __init__(self, tracer: Tracer, root: Span, model: str | None = None) -> None:
        self._tracer = tracer
        self.root = root
        self._model = model
        self._turn: Span | None = None
        self._turn_index = 0
        self._seen_user_text: set[str] = set()
        self._llm_span: Span | None = None
        self._resp_start_mono: float | None = None
        self._resp_ttft_ms: float | None = None
        self._usage_input = 0
        self._usage_output = 0
        self._response_count = 0
        self._event_count = 0

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

    def _close_turn(self) -> None:
        if self._llm_span is not None:
            self._finish_llm(None)
        if self._turn is not None:
            self._turn.set_status(Status(StatusCode.OK))
            self._turn.end()
            self._turn = None

    def _start_llm(self, response: Any) -> None:
        if self._llm_span is not None:
            self._finish_llm(None)
        self._resp_start_mono = time.monotonic()
        self._resp_ttft_ms = None
        model = self._model or _deep_get(response, "model")
        attrs: dict[str, Any] = {
            GenAIAttributes.GEN_AI_OPERATION_NAME: "chat",
            GenAIAttributes.GEN_AI_SYSTEM: PROVIDER,
            "gen_ai.provider.name": PROVIDER,
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
        if (
            self._llm_span is None
            or self._resp_ttft_ms is not None
            or self._resp_start_mono is None
        ):
            return
        self._resp_ttft_ms = (time.monotonic() - self._resp_start_mono) * 1000.0

    def _finish_llm(self, response: Any) -> None:
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
            span.set_attribute("gen_ai.server.time_to_first_token", ttft / 1000.0)
            span.set_attribute("mivas.ttft_ms", round(ttft, 2))
        span.set_status(Status(StatusCode.OK))
        span.end()

        self._usage_input += attrs.get(GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS, 0)
        self._usage_output += attrs.get(GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, 0)
        self._response_count += 1
        self.root.set_attribute(GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS, self._usage_input)
        self.root.set_attribute(GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, self._usage_output)
        self.root.set_attribute("gen_ai.usage.total_tokens", self._usage_input + self._usage_output)
        self.root.set_attribute("mivas.response.count", self._response_count)

    def _user_message(self, text: str) -> None:
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
                GenAIAttributes.GEN_AI_INPUT_MESSAGES: _clip([{"role": "user", "content": text}]),
            },
        )
        span.set_status(Status(StatusCode.OK))
        span.end()

    def handle_raw(self, event: dict) -> None:
        etype = event.get("type")
        self._event_count += 1
        try:
            self._raw(etype, event)
        except Exception:
            logger.debug("grok tracer failed on %s", etype, exc_info=True)

    def _raw(self, etype: str | None, event: dict) -> None:
        if not etype:
            return
        if etype == "conversation.item.input_audio_transcription.completed":
            tr = (event.get("transcript") or "").strip()
            if tr:
                self._close_turn()  # new caller utterance → fresh turn
                self._user_message(tr)
            return
        if etype == "response.created":
            self._start_llm(event.get("response") or {})
            return
        if etype in {
            "response.audio.delta",
            "response.output_audio.delta",
            "response.output_audio_transcript.delta",
            "response.output_text.delta",
        }:
            if event.get("delta") or event.get("audio"):
                self._mark_first_output()
            return
        if etype == "response.output_audio_transcript.done":
            tr = (event.get("transcript") or "").strip()
            if tr and self._llm_span is not None:
                self._llm_span.set_attribute("mivas.transcript", _clip(tr))
                self._llm_span.set_attribute(
                    GenAIAttributes.GEN_AI_OUTPUT_MESSAGES,
                    _clip([{"role": "assistant", "content": tr}]),
                )
            return
        if etype == "response.done":
            resp = dict(event.get("response") or {})
            if "usage" not in resp and event.get("usage") is not None:
                resp["usage"] = event["usage"]
            self._finish_llm(resp)
            return

    def close(self) -> None:
        self._close_turn()
        self.root.set_attribute("mivas.event_count", self._event_count)


def _parent_span() -> Span | None:
    """Always the voice.call root — never the current speech/tool span.

    Falling back to get_current_span() nests customer.speech under agent.speech
    (and vice versa) in Bluejay's waterfall.
    """
    parent = _root_span.get()
    if parent is not None and parent.get_span_context().is_valid:
        return parent
    if _active_root is not None and _active_root.get_span_context().is_valid:
        return _active_root
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
    parent: Span | None = None,
) -> Iterator[Span | None]:
    """Tool call under the active turn (LangSmith shape). No-op outside traced_run."""
    if parent is not None and parent.get_span_context().is_valid:
        root = parent
    else:
        t = active_tracer()
        root = t.current_turn() if t is not None else _parent_span()
    if root is None:
        yield None
        return

    tracer = otel_trace.get_tracer(TRACER_NAME)
    parent_ctx = otel_trace.set_span_in_context(root)
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.arguments": _json_attr(parameters if parameters is not None else {}),
        "bluejay.speech.start_offset_ms": call_offset_ms(),
    }
    if call_id:
        attrs["gen_ai.tool.call.id"] = str(call_id)

    # start_span (not start_as_current) so later speech stays a sibling of this tool.
    span = tracer.start_span(
        f"execute_tool {name}",
        context=parent_ctx,
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
            span.set_attribute("bluejay.speech.end_offset_ms", call_offset_ms())
            span.end()


def finish_tool_span(
    span: Span | None,
    output: Any,
    *,
    ok: bool = True,
    name: str | None = None,
    parameters: Any = None,
    start_offset_ms: int | None = None,
) -> None:
    if span is None:
        return
    span.set_attribute("gen_ai.tool.call.result", _json_attr(output))
    span.set_attribute("bluejay.speech.end_offset_ms", call_offset_ms())
    if ok:
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_status(Status(StatusCode.ERROR, _json_attr(output)[:400]))
    # tool_span ends the span on exit; this only fills attributes.


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
) -> AsyncIterator[Span | None]:
    """OTel voice.call root; flush + link trace_ids/tool_calls on exit."""
    global _active_root, _active_t0, _active_tools, _active_tracer_mod

    provider = setup_otel()
    if provider is None:
        yield None
        return

    tracer = otel_trace.get_tracer(TRACER_NAME)
    attrs: dict[str, Any] = {
        "gen_ai.system": PROVIDER,
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.operation.name": "realtime_session",
        "mivas.workflow.name": workflow_name,
    }
    if model:
        attrs["gen_ai.request.model"] = model
    if simulation_result_id:
        attrs["bluejay.simulation_result_id"] = str(simulation_result_id)

    otel_tid: str | None = None
    root_token = None
    tools_token = None
    tracer_token = None
    event_tracer: RealtimeEventTracer | None = None
    t0 = time.monotonic()
    t0_token = _call_t0.set(t0)
    tool_buf: list[dict[str, Any]] = []
    prev_active = _active_root
    prev_t0 = _active_t0
    prev_tools = _active_tools
    prev_tracer_mod = _active_tracer_mod
    try:
        with tracer.start_as_current_span(
            "realtime_session",
            kind=SpanKind.SERVER,
            attributes=attrs,
        ) as root:
            root_token = _root_span.set(root)
            tools_token = _reported_tools.set(tool_buf)
            event_tracer = RealtimeEventTracer(tracer, root, model=model)
            tracer_token = _active_tracer.set(event_tracer)
            _active_root = root
            _active_t0 = t0
            _active_tools = tool_buf
            _active_tracer_mod = event_tracer
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
                yield root
            except Exception as e:
                if type(e).__name__.startswith("ConnectionClosed"):
                    root.set_status(Status(StatusCode.OK))
                else:
                    raise
            finally:
                event_tracer.close()
    finally:
        _active_root = prev_active
        _active_t0 = prev_t0
        _active_tools = prev_tools
        _active_tracer_mod = prev_tracer_mod
        if root_token is not None:
            _root_span.reset(root_token)
        if tools_token is not None:
            _reported_tools.reset(tools_token)
        if tracer_token is not None:
            _active_tracer.reset(tracer_token)
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
