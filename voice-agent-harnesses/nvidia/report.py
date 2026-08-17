"""OpenTelemetry → Bluejay OTLP for NVIDIA Nemotron voice harnesses.

The Nemotron cascaded pipeline has no Agents-SDK span tree, so we emit GenAI-native spans:

  voice.call (root) — gen_ai.provider.name=nvidia
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
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

logger = logging.getLogger("mivas.otel.nvidia")
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
PROVIDER = "nvidia"
TRACER_NAME = "mivas.nvidia"

_provider: TracerProvider | None = None
_root_span: ContextVar[Span | None] = ContextVar("mivas_nvidia_otel_root", default=None)
_call_t0: ContextVar[float | None] = ContextVar("mivas_nvidia_otel_t0", default=None)
_reported_tools: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "mivas_nvidia_reported_tools", default=None
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
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-nvidia")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


_MAX_ATTR = 4000


def _json_attr(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _clip_attr(value: Any, n: int = _MAX_ATTR) -> str:
    s = _json_attr(value)
    return s if len(s) <= n else s[: n - 3] + "..."


def _deep_get(obj: Any, *path: str) -> Any:
    """Read a dotted path off a dict or object."""
    for key in path:
        if obj is None:
            return None
        obj = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    return obj


def _usage_attrs(usage: Any) -> dict[str, int]:
    """Realtime response usage → gen_ai.usage.* ints. Empty when the provider
    reports no usage (VoiceChat/NVCF does not — the model span still carries TTFT)."""
    out: dict[str, int] = {}
    if usage is None:
        return out

    def put(key: str, *path: str) -> None:
        v = _deep_get(usage, *path)
        if isinstance(v, int):
            out[key] = v

    put("gen_ai.usage.input_tokens", "input_tokens")
    put("gen_ai.usage.output_tokens", "output_tokens")
    put("gen_ai.usage.total_tokens", "total_tokens")
    put("gen_ai.usage.input_audio_tokens", "input_token_details", "audio_tokens")
    put("gen_ai.usage.input_text_tokens", "input_token_details", "text_tokens")
    put("gen_ai.usage.cached_tokens", "input_token_details", "cached_tokens")
    put("gen_ai.usage.output_audio_tokens", "output_token_details", "audio_tokens")
    put("gen_ai.usage.output_text_tokens", "output_token_details", "text_tokens")
    return out


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
    """Sibling of speech spans under voice.call. No-op outside traced_run."""
    root = parent if parent is not None and parent.get_span_context().is_valid else _parent_span()
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
    if name:
        record_tool_call(
            name, parameters, output, start_offset_ms=start_offset_ms
        )
    if span is None:
        return
    span.set_attribute("gen_ai.tool.call.result", _json_attr(output))
    span.set_attribute("bluejay.speech.end_offset_ms", call_offset_ms())
    if ok:
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_status(Status(StatusCode.ERROR, _json_attr(output)[:400]))
    # tool_span ends the span on exit; this only fills attributes.


def start_speech_span(
    utterance_id: str, *, speaker: str = "agent", parent: Span | None = None
) -> Span | None:
    """Begin agent.speech or customer.speech as a direct child of voice.call."""
    root = parent if parent is not None and parent.get_span_context().is_valid else _parent_span()
    if root is None:
        return None
    tracer = otel_trace.get_tracer(TRACER_NAME)
    parent_ctx = otel_trace.set_span_in_context(root)
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


class RealtimeSpanTracer:
    """LangSmith-shaped tree for the S2S VoiceChat path, driven imperatively from
    the CHIRP bridge's already-hand-dispatched event loop (no wrappable iterator):

        realtime_session (root)
          turn                 (one caller utterance → all ensuing agent activity)
            model              (per response: TTFT + output transcript; gen_ai.usage.*
                                only if the provider reports it — VoiceChat does not)

    Tools land as ``execute_tool`` via ``tool_span`` (parented at the root), which
    Bluejay extracts regardless of nesting. No per-chunk / agent.speech / customer.speech
    spans. VoiceChat exposes no user ASR transcript, so a turn has no ``user_message``.
    """

    def __init__(self, root: Span | None, model: str | None = None, *, system: str = PROVIDER, tracer=None) -> None:
        self._tracer = tracer or otel_trace.get_tracer(TRACER_NAME)
        self.root = root
        self._model = model
        self._system = system
        self._turn: Span | None = None
        self._turn_index = 0
        self._model_span: Span | None = None
        self._resp_start_mono: float | None = None
        self._resp_ttft_ms: float | None = None
        self._usage_input = 0
        self._usage_output = 0
        self._response_count = 0

    def _ok(self) -> bool:
        return self.root is not None and self.root.get_span_context().is_valid

    def _current_turn(self) -> Span | None:
        if not self._ok():
            return None
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
        turn = self._current_turn()
        return otel_trace.set_span_in_context(turn) if turn is not None else None

    def _close_turn(self) -> None:
        self.end_model(None)
        if self._turn is not None:
            self._turn.set_status(Status(StatusCode.OK))
            self._turn.end()
            self._turn = None

    def on_user_speech(self) -> None:
        """New caller utterance → previous turn is done; open a fresh one."""
        if not self._ok():
            return
        self._close_turn()
        self._current_turn()

    def start_model(self, event: Any = None) -> None:
        """response.created → open a generation span; duration = model latency."""
        ctx = self._turn_ctx()
        if ctx is None:
            return
        if self._model_span is not None:
            self.end_model(None)
        self._resp_start_mono = time.monotonic()
        self._resp_ttft_ms = None
        model = self._model or _deep_get(event, "response", "model")
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": self._system,
            "gen_ai.provider.name": self._system,
            "mivas.modality": "audio",
        }
        if model:
            attrs["gen_ai.request.model"] = str(model)
        rid = _deep_get(event, "response", "id")
        if rid:
            attrs["gen_ai.response.id"] = str(rid)
        self._model_span = self._tracer.start_span(
            "model", context=ctx, kind=SpanKind.CLIENT, attributes=attrs
        )

    def mark_first_output(self) -> None:
        """First agent audio/transcript chunk → time to first token."""
        if (
            self._model_span is None
            or self._resp_ttft_ms is not None
            or self._resp_start_mono is None
        ):
            return
        self._resp_ttft_ms = (time.monotonic() - self._resp_start_mono) * 1000.0

    def set_output(self, text: str | None) -> None:
        if self._model_span is not None and text:
            self._model_span.set_attribute("mivas.transcript", _clip_attr(text))
            self._model_span.set_attribute(
                "gen_ai.output.messages",
                _clip_attr([{"role": "assistant", "content": str(text)}]),
            )

    def end_model(self, event: Any) -> None:
        """response.done → stamp usage (if any) + TTFT, end the span, roll onto root."""
        span = self._model_span
        self._model_span = None
        ttft = self._resp_ttft_ms
        self._resp_start_mono = None
        self._resp_ttft_ms = None
        if span is None:
            return
        attrs = _usage_attrs(_deep_get(event, "response", "usage") if event else None)
        for key, value in attrs.items():
            span.set_attribute(key, value)
        rmodel = (_deep_get(event, "response", "model") if event else None) or self._model
        if rmodel:
            span.set_attribute("gen_ai.response.model", str(rmodel))
        if ttft is not None:
            span.set_attribute("gen_ai.server.time_to_first_token", ttft / 1000.0)
            span.set_attribute("mivas.ttft_ms", round(ttft, 2))
        span.set_status(Status(StatusCode.OK))
        span.end()
        self._usage_input += attrs.get("gen_ai.usage.input_tokens", 0)
        self._usage_output += attrs.get("gen_ai.usage.output_tokens", 0)
        self._response_count += 1
        if attrs and self._ok():
            self.root.set_attribute("gen_ai.usage.input_tokens", self._usage_input)
            self.root.set_attribute("gen_ai.usage.output_tokens", self._usage_output)
            self.root.set_attribute(
                "gen_ai.usage.total_tokens", self._usage_input + self._usage_output
            )
        if self._ok():
            self.root.set_attribute("mivas.response.count", self._response_count)

    def close(self) -> None:
        self._close_turn()


async def _await_upsert_ready(
    client: httpx.AsyncClient, simulation_result_id: str, timeout: float = 120.0
) -> str | None:
    """Wait until the sim is linkable (conversation over), not until COMPLETED.

    Waiting for FINAL blocked the CHIRP handler for minutes after CALL END, so
    trace_ids/tool actuals stayed empty through CONVERSATION_ENDED / EVALUATING.
    Post as soon as conversation ends; `_relink_after_final` repairs eval wipes.
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
                if st in FINAL_STATUSES or st in EARLY_UPSERT_STATUSES:
                    return st
        except Exception:
            pass
        await asyncio.sleep(0.5)
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
                st = await _await_upsert_ready(client, simulation_result_id)
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
                        "update-simulation-result ok trace=%s sim=%s status=%s attempt=%s",
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
    root_name: str = "voice.call",
    operation: str = "invoke_agent",
) -> AsyncIterator[Span | None]:
    """OTel root span; flush + link trace_ids/tool_calls on exit.

    ``root_name``/``operation`` default to the cascaded pipeline's ``voice.call``;
    the S2S VoiceChat path passes ``realtime_session`` for the LangSmith-shaped tree.
    """
    global _active_root, _active_t0, _active_tools

    provider = setup_otel()
    if provider is None:
        yield None
        return

    tracer = otel_trace.get_tracer(TRACER_NAME)
    attrs: dict[str, Any] = {
        "gen_ai.system": PROVIDER,
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.operation.name": operation,
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
            root_name,
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
                yield root
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
        # Do not await enrichment here — waiting for Bluejay status held the CHIRP
        # handler open for minutes after CALL END and delayed the next dial.
        if simulation_result_id and (otel_tid or tool_buf):
            tid, sim = otel_tid, simulation_result_id

            async def _enrich() -> None:
                try:
                    await post_simulation_enrichment(sim, trace_id=tid)
                except Exception as e:
                    logger.error(
                        "post_simulation_enrichment crashed: %s: %s",
                        type(e).__name__,
                        e,
                    )

            asyncio.create_task(_enrich(), name=f"otel-enrich-{sim}")
        elif simulation_result_id and not otel_tid:
            logger.error(
                "have simulation_result_id=%s but no otel trace id to post",
                simulation_result_id,
            )
