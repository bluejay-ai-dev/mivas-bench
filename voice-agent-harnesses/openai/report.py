"""Realtime session events → Bluejay OTel traces.

LangSmith-style proxy: wrap the ``RealtimeSession``, parse session events,
and emit an OTel tree for Bluejay:

  voice.call
    ├── user.turn          (what the user said)
    ├── agent.turn         (what the agent said)
    ├── agent.speech       (audio segment + transcript)
    ├── execute_tool <n>   (tool calls + handoffs-as-tools)
    └── agent.<name>       (agent active window)

Protocol chatter (audio chunks, history_updated, …) is parsed for those
spans only — it is not dumped onto the root as OTel span events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

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

logger = logging.getLogger("mivas.otel")

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
_MAX_ATTR = 4000

# target agent name → the blueprint's handoff tool name, so a handoff is reported under
# the name the industry declares (`transfer_to_identity`) rather than a synthesized one.
# Populated by build_from_blueprint; unknown targets fall back to handoff_to_<target>.
_HANDOFF_TOOL_NAMES: dict[str, str] = {}


def register_handoff_tool_names(mapping: dict[str, str]) -> None:
    _HANDOFF_TOOL_NAMES.update(mapping)

_provider: TracerProvider | None = None


def _api_url() -> str:
    return (os.environ.get("BLUEJAY_API_URL") or DEFAULT_API_URL).rstrip("/")


def _otlp_endpoint() -> str:
    return os.environ.get("BLUEJAY_OTLP_ENDPOINT") or DEFAULT_OTLP_ENDPOINT


def _service_name() -> str:
    return os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-openai")


def _api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


def _clip(value: Any, n: int = _MAX_ATTR) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


def _extract_content_text(item_content: Any) -> str | None:
    if isinstance(item_content, str):
        return item_content
    if not item_content or not isinstance(item_content, list):
        return None
    for part in item_content:
        if isinstance(part, dict):
            text = part.get("text") or part.get("transcript")
        else:
            text = getattr(part, "text", None) or getattr(part, "transcript", None)
        if text:
            return str(text)
    return None


def _unwrap_raw(data: Any) -> tuple[str | None, Any]:
    if isinstance(data, dict):
        dtype = data.get("type")
        nested = data.get("data")
        if isinstance(nested, dict) and nested.get("type"):
            return nested.get("type"), nested
        return dtype, data
    dtype = getattr(data, "type", None)
    nested = getattr(data, "data", None)
    if nested is not None:
        nested_type = (
            nested.get("type")
            if isinstance(nested, dict)
            else getattr(nested, "type", None)
        )
        if nested_type:
            return nested_type, nested
    return dtype, data


def setup_otel() -> TracerProvider | None:
    """TracerProvider → Bluejay OTLP. No Agents instrumentor."""
    global _provider

    api_key = _api_key()
    if not api_key:
        return None

    if _provider is None:
        try:
            from agents import set_tracing_disabled

            set_tracing_disabled(True)
        except Exception:
            pass

        resource = Resource.create({SERVICE_NAME: _service_name()})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            # BatchSpanProcessor, not SimpleSpanProcessor: Simple exports every span
            # with a BLOCKING http request from whatever thread ended the span — the
            # asyncio loop. At max_concurrent=20 that starved the loop badly enough
            # that calls stopped dispatching tools at all (no per-call DB was created
            # for the last third of run 228909) and the spans that did exist landed
            # after the trace-link POST, so Bluejay extracted 1 tool out of 7. Batching
            # moves the export to a background thread; force_flush() before the POST
            # still guarantees delivery.
            BatchSpanProcessor(
                OTLPSpanExporter(_otlp_endpoint(), headers={"X-API-KEY": api_key}),
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
        logger.info("otel → %s service=%s (event proxy)", _otlp_endpoint(), _service_name())

    return _provider


def flush() -> None:
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as e:
            logger.error("otel flush failed: %s", e)


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

    MIVAS_UPSERT_SETTLE_SECONDS delays the POST after the stop status is seen.
    force_flush() guarantees the spans reached the collector, NOT that they are
    queryable yet — the server extracts execute_tool spans at link time, so a POST that
    beats ingest links a trace and extracts nothing. Harmless on the default path (the
    wait for a final status already outlasts ingest), load-bearing with
    MIVAS_UPSERT_BEFORE_EVAL, where the POST fires seconds after the last span.
    """
    deadline = time.monotonic() + timeout
    key = _api_key()
    if not key:
        return None
    settle = float(os.environ.get("MIVAS_UPSERT_SETTLE_SECONDS", "0") or 0)
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
                    if settle > 0:
                        await asyncio.sleep(settle)
                    return st
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None


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
        await asyncio.sleep(float(os.environ.get("MIVAS_RELINK_SETTLE_SECONDS", "15")))
        try:
            r = await client.get(
                f"{_api_url()}/retrieve-simulation-result/{simulation_result_id}",
                headers={"X-API-Key": key},
            )
            if r.status_code == 200:
                result = (r.json() or {}).get("simulation_result") or {}
                linked = trace_id in (result.get("trace_ids") or [])
        except Exception:
            pass
        if linked:
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


async def post_trace_ids(simulation_result_id: str, trace_id: str) -> None:
    key = _api_key()
    if not key or not simulation_result_id or not trace_id:
        logger.warning(
            "skip update-simulation-result — sim=%s trace=%s key=%s",
            simulation_result_id,
            trace_id,
            bool(key),
        )
        return
    body = {
        "simulation_result_id": str(simulation_result_id),
        "trace_ids": [trace_id],
    }
    async with httpx.AsyncClient(timeout=20) as client:
        st = await _await_terminal_upsert(client, simulation_result_id)
        if st in EARLY_UPSERT_STATUSES:
            # Bluejay extracts execute_tool spans at POST time, so posting the instant the
            # call ends can beat the OTLP spans into the store and link a trace with no
            # tools. force_flush() only proves the exporter accepted them. Settle first —
            # still well inside the window before evaluation reads the tool list.
            await asyncio.sleep(float(os.environ.get("MIVAS_UPSERT_SETTLE_SECONDS", "10")))
        r = await client.post(
            f"{_api_url()}/update-simulation-result",
            json=body,
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
            if st is None or st in EARLY_UPSERT_STATUSES:
                await _relink_after_final(
                    client, simulation_result_id, body, key, trace_id, st
                )


class RealtimeEventTracer:
    """Maps RealtimeSession events → child spans under ``voice.call``."""

    def __init__(self, tracer: Tracer, root: Span) -> None:
        self._tracer = tracer
        self.root = root
        self._agent_spans: dict[str, Span] = {}
        self._tool_spans: dict[str, Span] = {}
        self._speech_spans: dict[str, Span] = {}
        self._speech_text: dict[str, list[str]] = {}
        self._customer_speech: Span | None = None
        self._seen_agent_text: set[str] = set()
        self._seen_user_text: set[str] = set()

    def wrap(self, session: Any) -> "TracedRealtimeSession":
        return TracedRealtimeSession(session, self)

    def start_customer_speech(self, utterance_id: str) -> Span | None:
        """CHIRP inbound speech.* → customer.speech under voice.call."""
        self.end_customer_speech()
        span = self._tracer.start_span(
            "customer.speech",
            context=otel_trace.set_span_in_context(self.root),
            kind=SpanKind.INTERNAL,
            attributes={
                GenAIAttributes.GEN_AI_OPERATION_NAME: "speech_to_text",
                "mivas.speech.speaker": "customer",
                "mivas.utterance_id": str(utterance_id),
                "mivas.event": "chirp.speech.started",
            },
        )
        self._customer_speech = span
        return span

    def end_customer_speech(self, span: Span | None = None) -> None:
        target = span if span is not None else self._customer_speech
        if target is None:
            return
        if self._customer_speech is target:
            self._customer_speech = None
        target.set_status(Status(StatusCode.OK))
        target.end()

    def _parent_ctx(self, agent_name: str | None = None):
        if agent_name and agent_name in self._agent_spans:
            return otel_trace.set_span_in_context(self._agent_spans[agent_name])
        return otel_trace.set_span_in_context(self.root)

    def handle(self, event: Any) -> None:
        etype = getattr(event, "type", None) or "unknown"
        try:
            self._dispatch(etype, event)
        except Exception:
            logger.debug("event tracer failed on %s", etype, exc_info=True)

    def _dispatch(self, etype: str, event: Any) -> None:
        if etype == "agent_start":
            agent = getattr(event, "agent", None)
            name = getattr(agent, "name", None) or "Unknown"
            if name not in self._agent_spans:
                span = self._tracer.start_span(
                    f"agent.{name}",
                    context=otel_trace.set_span_in_context(self.root),
                    kind=SpanKind.INTERNAL,
                    attributes={
                        GenAIAttributes.GEN_AI_AGENT_NAME: name,
                        "mivas.event": "agent_start",
                    },
                )
                self._agent_spans[name] = span

        elif etype == "agent_end":
            agent = getattr(event, "agent", None)
            name = getattr(agent, "name", None) or "Unknown"
            span = self._agent_spans.pop(name, None)
            if span is not None:
                span.set_status(Status(StatusCode.OK))
                span.end()

        elif etype == "tool_start":
            tool = getattr(event, "tool", None)
            agent = getattr(event, "agent", None)
            name = getattr(tool, "name", None) or "unknown_tool"
            agent_name = getattr(agent, "name", None) if agent else None
            args = getattr(event, "arguments", None)
            attrs: dict[str, Any] = {
                GenAIAttributes.GEN_AI_OPERATION_NAME: "execute_tool",
                GenAIAttributes.GEN_AI_TOOL_NAME: name,
                "mivas.event": "tool_start",
            }
            if agent_name:
                attrs[GenAIAttributes.GEN_AI_AGENT_NAME] = agent_name
            if args is not None:
                attrs[GenAIAttributes.GEN_AI_TOOL_CALL_ARGUMENTS] = _clip(args)
            span = self._tracer.start_span(
                f"execute_tool {name}",
                context=self._parent_ctx(agent_name),
                kind=SpanKind.INTERNAL,
                attributes=attrs,
            )
            self._tool_spans[name] = span

        elif etype == "tool_end":
            tool = getattr(event, "tool", None)
            name = getattr(tool, "name", None) or "unknown_tool"
            output = getattr(event, "output", None)
            args = getattr(event, "arguments", None)
            span = self._tool_spans.pop(name, None)
            if span is not None:
                if output is not None:
                    span.set_attribute(
                        GenAIAttributes.GEN_AI_TOOL_CALL_RESULT, _clip(output)
                    )
                span.set_status(Status(StatusCode.OK))
                span.end()

        elif etype == "handoff":
            frm = getattr(event, "from_agent", None)
            to = getattr(event, "to_agent", None)
            from_name = getattr(frm, "name", None) or "Unknown"
            to_name = getattr(to, "name", None) or "Unknown"
            tool_name = _HANDOFF_TOOL_NAMES.get(to_name) or f"handoff_to_{to_name}"
            # Bluejay tool_calls path: handoffs show up as execute_tool.
            with self._tracer.start_as_current_span(
                f"execute_tool {tool_name}",
                context=self._parent_ctx(from_name),
                kind=SpanKind.INTERNAL,
                attributes={
                    GenAIAttributes.GEN_AI_OPERATION_NAME: "execute_tool",
                    GenAIAttributes.GEN_AI_TOOL_NAME: tool_name,
                    "gen_ai.handoff.from_agent": from_name,
                    "gen_ai.handoff.to_agent": to_name,
                    "mivas.event": "handoff",
                },
            ) as span:
                span.set_attribute(
                    GenAIAttributes.GEN_AI_TOOL_CALL_ARGUMENTS,
                    _clip({"from": from_name, "to": to_name}),
                )
                span.set_attribute(
                    GenAIAttributes.GEN_AI_TOOL_CALL_RESULT,
                    _clip({"success": True, "to_agent": to_name}),
                )
                span.set_status(Status(StatusCode.OK))
            # Close the from-agent window; to-agent will get agent_start.
            old = self._agent_spans.pop(from_name, None)
            if old is not None:
                old.set_status(Status(StatusCode.OK))
                old.end()

        elif etype == "audio":
            item_id = getattr(event, "item_id", None) or "unknown"
            content_index = getattr(event, "content_index", 0)
            key = f"{item_id}:{content_index}"
            if key not in self._speech_spans:
                self._speech_spans[key] = self._tracer.start_span(
                    "agent.speech",
                    context=self._parent_ctx(),
                    kind=SpanKind.INTERNAL,
                    attributes={
                        "mivas.event": "audio",
                        "mivas.item_id": str(item_id),
                        "mivas.content_index": int(content_index or 0),
                    },
                )
                self._speech_text[key] = []

        elif etype in {"audio_end", "audio_interrupted"}:
            item_id = getattr(event, "item_id", None) or "unknown"
            content_index = getattr(event, "content_index", 0)
            key = f"{item_id}:{content_index}"
            span = self._speech_spans.pop(key, None)
            parts = self._speech_text.pop(key, [])
            if span is not None:
                if parts:
                    text = "".join(parts)
                    span.set_attribute("mivas.transcript", _clip(text))
                    span.set_attribute(
                        GenAIAttributes.GEN_AI_OUTPUT_MESSAGES,
                        _clip([{"role": "assistant", "content": text}]),
                    )
                if etype == "audio_interrupted":
                    span.set_attribute("mivas.interrupted", True)
                span.set_status(Status(StatusCode.OK))
                span.end()

        elif etype == "history_added":
            item = getattr(event, "item", None)
            role = getattr(item, "role", None) if item else None
            text = _extract_content_text(getattr(item, "content", None) if item else None)
            if text and role == "user":
                self._user_turn(text, source="history_added")
            elif text and role == "assistant":
                self._agent_turn(text, source="history_added")

        elif etype == "history_updated":
            history = getattr(event, "history", None)
            if isinstance(history, list):
                for item in reversed(history):
                    role = getattr(item, "role", None)
                    text = _extract_content_text(getattr(item, "content", None))
                    if text and role == "assistant":
                        self._agent_turn(text, source="history_updated")
                        break

        elif etype == "error":
            err = getattr(event, "error", "Unknown error")
            self.root.set_attribute("mivas.error", _clip(err, 1000))
            self.root.record_exception(Exception(str(err)))

        elif etype == "raw_model_event":
            data = getattr(event, "data", None)
            dtype, payload = _unwrap_raw(data)
            self._raw(dtype, payload)

    def _raw(self, dtype: str | None, data: Any) -> None:
        if not dtype:
            return

        def _get(obj: Any, key: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        if dtype in {
            "conversation.item.input_audio_transcription.completed",
            "input_audio_transcription_completed",
        }:
            transcript = _get(data, "transcript")
            if transcript:
                self._user_turn(str(transcript), source=dtype)
            return

        if dtype in {
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        }:
            delta = _get(data, "delta") or ""
            item_id = _get(data, "item_id") or "unknown"
            content_index = _get(data, "content_index", 0)
            key = f"{item_id}:{content_index}"
            if key in self._speech_text and delta:
                self._speech_text[key].append(str(delta))
            return

        if dtype in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        }:
            transcript = _get(data, "transcript")
            item_id = _get(data, "item_id") or "unknown"
            content_index = _get(data, "content_index", 0)
            key = f"{item_id}:{content_index}"
            if transcript and key in self._speech_spans:
                self._speech_spans[key].set_attribute(
                    "mivas.transcript", _clip(transcript)
                )
            if transcript:
                self._agent_turn(str(transcript), source=dtype)
            return

        if dtype == "response.done":
            usage = None
            response = _get(data, "response")
            if isinstance(response, dict):
                usage = response.get("usage")
            elif response is not None:
                usage = getattr(response, "usage", None)
            if usage is not None:
                self.root.set_attribute("mivas.usage", _clip(usage))

    def _user_turn(self, text: str, *, source: str) -> None:
        key = text.strip()
        if not key or key in self._seen_user_text:
            return
        self._seen_user_text.add(key)
        with self._tracer.start_as_current_span(
            "user.turn",
            context=otel_trace.set_span_in_context(self.root),
            kind=SpanKind.INTERNAL,
            attributes={
                "mivas.event": source,
                "mivas.role": "user",
                "mivas.transcript": _clip(text),
                GenAIAttributes.GEN_AI_INPUT_MESSAGES: _clip(
                    [{"role": "user", "content": text}]
                ),
            },
        ) as span:
            span.set_status(Status(StatusCode.OK))

    def _agent_turn(self, text: str, *, source: str) -> None:
        key = text.strip()
        if not key or key in self._seen_agent_text:
            return
        self._seen_agent_text.add(key)
        with self._tracer.start_as_current_span(
            "agent.turn",
            context=self._parent_ctx(),
            kind=SpanKind.INTERNAL,
            attributes={
                "mivas.event": source,
                "mivas.role": "assistant",
                "mivas.transcript": _clip(text),
                GenAIAttributes.GEN_AI_OUTPUT_MESSAGES: _clip(
                    [{"role": "assistant", "content": text}]
                ),
            },
        ) as span:
            span.set_status(Status(StatusCode.OK))

    def close(self) -> None:
        self.end_customer_speech()
        for span in list(self._tool_spans.values()):
            span.set_status(Status(StatusCode.OK))
            span.end()
        self._tool_spans.clear()
        for span in list(self._speech_spans.values()):
            span.set_status(Status(StatusCode.OK))
            span.end()
        self._speech_spans.clear()
        for span in list(self._agent_spans.values()):
            span.set_status(Status(StatusCode.OK))
            span.end()
        self._agent_spans.clear()


class TracedRealtimeSession:
    """Transparent proxy: same API as RealtimeSession, events → tracer.

    ``RealtimeSession.__aiter__`` is an async generator (not ``__anext__`` on
    the session), so we wrap that iterator rather than calling ``__anext__``
    on the session object.
    """

    def __init__(self, session: Any, tracer: RealtimeEventTracer) -> None:
        self._session = session
        self._tracer = tracer
        self._iter: Any = None

    def __aiter__(self) -> "TracedRealtimeSession":
        # RealtimeSession.__aiter__ is `async def` + yield → async gen object.
        self._iter = self._session.__aiter__()
        return self

    async def __anext__(self) -> Any:
        if self._iter is None:
            self._iter = self._session.__aiter__()
        try:
            event = await self._iter.__anext__()
        except StopAsyncIteration:
            raise
        self._tracer.handle(event)
        return event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@asynccontextmanager
async def traced_run(
    workflow_name: str,
    *,
    simulation_result_id: str | None = None,
) -> AsyncIterator[Optional[RealtimeEventTracer]]:
    """Open ``voice.call`` root; yield an event tracer (or None if no API key)."""
    provider = setup_otel()
    if provider is None:
        yield None
        return

    tracer = otel_trace.get_tracer("mivas.openai.realtime")
    attrs: dict[str, Any] = {
        "mivas.workflow.name": workflow_name,
        GenAIAttributes.GEN_AI_OPERATION_NAME: "voice.call",
    }
    if simulation_result_id:
        attrs["bluejay.simulation_result_id"] = str(simulation_result_id)

    otel_tid: str | None = None
    event_tracer: RealtimeEventTracer | None = None
    try:
        with tracer.start_as_current_span(
            "voice.call",
            kind=SpanKind.SERVER,
            attributes=attrs,
        ) as root:
            ctx = root.get_span_context()
            if ctx.is_valid:
                otel_tid = format(ctx.trace_id, "032x")
                logger.info(
                    "otel trace_id=%s sim=%s workflow=%s",
                    otel_tid,
                    simulation_result_id,
                    workflow_name,
                )
            event_tracer = RealtimeEventTracer(tracer, root)
            yield event_tracer
            event_tracer.close()
    finally:
        flush()
        if simulation_result_id:
            try:
                from snapshot import capture_final

                await asyncio.to_thread(capture_final, str(simulation_result_id))
            except Exception:
                logger.exception("final snapshot failed sim=%s", simulation_result_id)
        if simulation_result_id and otel_tid:
            await post_trace_ids(simulation_result_id, otel_tid)
        elif simulation_result_id and not otel_tid:
            logger.error(
                "have simulation_result_id=%s but no otel trace id to post",
                simulation_result_id,
            )
