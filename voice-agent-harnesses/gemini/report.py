"""OpenTelemetry → Bluejay OTLP for Gemini Live harnesses.

LangSmith-shaped tree (same as the OpenAI realtime harness), emitted from the
Gemini Live server event stream:

  realtime_session (root) — gen_ai.system=gcp.gemini
    └── turn                    (one caller utterance → all ensuing agent activity)
          ├── user_message      (caller transcript)
          ├── model             (one generation per response: gen_ai.usage.* token
          │                      breakdown + time-to-first-token + output transcript)
          ├── execute_tool <n>  (gen_ai.tool.*; Bluejay reads these)
          └── audio_interrupted (barge-in)

Token counts come from Gemini `usage_metadata` (prompt/response/total, cached,
thoughts=reasoning, and per-modality audio/text) into standard gen_ai.usage.*
attributes and are rolled up onto the root. TTFT = caller-stop → first agent
audio. State lives on a per-call `GeminiTrace` (one CHIRP process serves many
concurrent calls, so module globals would race).

After the call we POST update-simulation-result with trace_ids; tool markers come
from execute_tool spans. Chirp supplies simulation_result_id via
X-Simulation-Result-Id on upgrade.
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
# The per-call GeminiTrace (turn/model state). Set before the inbound/outbound
# tasks are created so both inherit the same object and share its turn spans.
_trace: ContextVar["GeminiTrace | None"] = ContextVar("mivas_gemini_trace", default=None)
_active_trace: "GeminiTrace | None" = None


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


def _parent_span() -> Span | None:
    # Tools nest under the active turn, so execute_tool lands inside realtime_session → turn.
    tr = _trace.get() or _active_trace
    if tr is not None:
        return tr.current_turn()
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


def _usage_attrs(um: Any) -> dict[str, int]:
    """Gemini Live UsageMetadata → gen_ai.usage.* ints (audio/text/cached/reasoning)."""
    out: dict[str, int] = {}
    if um is None:
        return out

    def put(key: str, val: Any) -> None:
        if isinstance(val, int):
            out[key] = val

    put("gen_ai.usage.input_tokens", getattr(um, "prompt_token_count", None))
    put("gen_ai.usage.output_tokens", getattr(um, "response_token_count", None))
    put("gen_ai.usage.total_tokens", getattr(um, "total_token_count", None))
    put("gen_ai.usage.cached_tokens", getattr(um, "cached_content_token_count", None))
    put("gen_ai.usage.output_reasoning_tokens", getattr(um, "thoughts_token_count", None))
    for det in getattr(um, "prompt_tokens_details", None) or []:
        mod = str(getattr(getattr(det, "modality", None), "name", getattr(det, "modality", ""))).upper()
        if mod == "AUDIO":
            put("gen_ai.usage.input_audio_tokens", getattr(det, "token_count", None))
        elif mod == "TEXT":
            put("gen_ai.usage.input_text_tokens", getattr(det, "token_count", None))
    for det in getattr(um, "response_tokens_details", None) or []:
        mod = str(getattr(getattr(det, "modality", None), "name", getattr(det, "modality", ""))).upper()
        if mod == "AUDIO":
            put("gen_ai.usage.output_audio_tokens", getattr(det, "token_count", None))
        elif mod == "TEXT":
            put("gen_ai.usage.output_text_tokens", getattr(det, "token_count", None))
    return out


class GeminiTrace:
    """Per-call LangSmith-shaped tree: realtime_session → turn → {user_message, model, execute_tool}.

    One CHIRP process serves concurrent calls; each ``_bridge`` gets its own
    instance, so turn/model state can never bleed between calls.
    """

    def __init__(self, tracer: Any, root: Span, model: str | None = None) -> None:
        self._tracer = tracer
        self.root = root
        self._model = model
        self._turn: Span | None = None
        self._turn_index = 0
        self._model_span: Span | None = None
        self._ref_mono: float | None = None  # caller-stop (or turn-open) → TTFT baseline
        self._ttft_ms: float | None = None
        self._pending_usage: Any = None
        self._out_parts: list[str] = []
        self._seen_user: set[str] = set()
        self._usage_in = 0
        self._usage_out = 0
        self._responses = 0
        self._events = 0

    # -- turn --
    def current_turn(self) -> Span:
        if self._turn is None:
            self._turn_index += 1
            self._turn = self._tracer.start_span(
                "turn",
                context=otel_trace.set_span_in_context(self.root),
                kind=SpanKind.INTERNAL,
                attributes={"mivas.turn.index": self._turn_index},
            )
            if self._ref_mono is None:
                self._ref_mono = time.monotonic()
        return self._turn

    def _turn_ctx(self):
        return otel_trace.set_span_in_context(self.current_turn())

    def start_turn(self) -> None:
        """New caller utterance → previous turn done, open a fresh one."""
        self.close_turn()
        self._ref_mono = time.monotonic()
        self.current_turn()

    def mark_ref(self) -> None:
        """Caller stopped speaking → TTFT baseline for the model's reply."""
        self._ref_mono = time.monotonic()

    def close_turn(self) -> None:
        if self._model_span is not None:
            self.finish_model()
        if self._turn is not None:
            self._turn.set_status(Status(StatusCode.OK))
            self._turn.end()
            self._turn = None

    # -- caller --
    def user_message(self, text: str | None) -> None:
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
                "mivas.transcript": _json_attr(key),
                "gen_ai.input.messages": _json_attr([{"role": "user", "content": key}]),
            },
        )
        span.set_status(Status(StatusCode.OK))
        span.end()

    # -- model --
    def on_model_audio(self) -> None:
        """First agent audio chunk of a response → open the generation span + TTFT."""
        if self._model_span is not None:
            return
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": PROVIDER,
            "gen_ai.provider.name": PROVIDER,
            "mivas.modality": "audio",
            "mivas.event": "model",
        }
        if self._model:
            attrs["gen_ai.request.model"] = str(self._model)
        self._model_span = self._tracer.start_span(
            "model", context=self._turn_ctx(), kind=SpanKind.CLIENT, attributes=attrs
        )
        if self._ref_mono is not None:
            self._ttft_ms = max(0.0, (time.monotonic() - self._ref_mono) * 1000.0)

    def record_usage(self, um: Any) -> None:
        if um is not None:
            self._pending_usage = um

    def add_output(self, text: str | None) -> None:
        if text:
            self._out_parts.append(str(text))

    def finish_model(self) -> None:
        """turn_complete → stamp usage + TTFT + transcript, end the span, roll totals up."""
        span = self._model_span
        self._model_span = None
        ttft = self._ttft_ms
        self._ttft_ms = None
        usage = self._pending_usage
        self._pending_usage = None
        parts = self._out_parts
        self._out_parts = []
        if span is None:
            return
        attrs = _usage_attrs(usage)
        for key, value in attrs.items():
            span.set_attribute(key, value)
        if self._model:
            span.set_attribute("gen_ai.response.model", str(self._model))
        if ttft is not None:
            span.set_attribute("gen_ai.server.time_to_first_token", ttft / 1000.0)
            span.set_attribute("mivas.ttft_ms", round(ttft, 2))
        if parts:
            text = "".join(parts)
            span.set_attribute("mivas.transcript", _json_attr(text))
            span.set_attribute(
                "gen_ai.output.messages", _json_attr([{"role": "assistant", "content": text}])
            )
        span.set_status(Status(StatusCode.OK))
        span.end()
        # ponytail: sum per-response like the OpenAI harness; if Gemini reports
        # cumulative usage_metadata this over-counts the root total — the per-span
        # gen_ai.usage.* are the authoritative numbers either way.
        self._usage_in += attrs.get("gen_ai.usage.input_tokens", 0)
        self._usage_out += attrs.get("gen_ai.usage.output_tokens", 0)
        self._responses += 1
        self.root.set_attribute("gen_ai.usage.input_tokens", self._usage_in)
        self.root.set_attribute("gen_ai.usage.output_tokens", self._usage_out)
        self.root.set_attribute("gen_ai.usage.total_tokens", self._usage_in + self._usage_out)
        self.root.set_attribute("mivas.response.count", self._responses)

    def interrupted(self) -> None:
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

    def bump_event(self) -> None:
        self._events += 1

    def close(self) -> None:
        self.close_turn()
        self.root.set_attribute("mivas.event_count", self._events)


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
) -> AsyncIterator["GeminiTrace | None"]:
    """OTel realtime_session root; yields a GeminiTrace; links trace_ids on exit."""
    global _active_root, _active_t0, _active_tools, _active_trace

    provider = setup_otel()
    if provider is None:
        yield None
        return

    tracer = otel_trace.get_tracer(TRACER_NAME)
    attrs: dict[str, Any] = {
        # Gemini / GenAI semantic conventions (not openai.realtime)
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
    t0 = time.monotonic()
    t0_token = _call_t0.set(t0)
    tool_buf: list[dict[str, Any]] = []
    prev_active = _active_root
    prev_t0 = _active_t0
    prev_tools = _active_tools
    prev_trace = _active_trace
    trace_token = None
    trace: GeminiTrace | None = None
    try:
        with tracer.start_as_current_span(
            "realtime_session",
            kind=SpanKind.SERVER,
            attributes=attrs,
        ) as root:
            root_token = _root_span.set(root)
            tools_token = _reported_tools.set(tool_buf)
            trace = GeminiTrace(tracer, root, model=model)
            trace_token = _trace.set(trace)
            _active_root = root
            _active_t0 = t0
            _active_tools = tool_buf
            _active_trace = trace
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
                yield trace
            except Exception as e:
                if type(e).__name__.startswith("ConnectionClosed"):
                    root.set_status(Status(StatusCode.OK))
                else:
                    raise
            finally:
                trace.close()
    finally:
        _active_root = prev_active
        _active_t0 = prev_t0
        _active_tools = prev_tools
        _active_trace = prev_trace
        if root_token is not None:
            _root_span.reset(root_token)
        if tools_token is not None:
            _reported_tools.reset(tools_token)
        if trace_token is not None:
            _trace.reset(trace_token)
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
