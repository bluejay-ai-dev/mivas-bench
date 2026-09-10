"""GPT-Live session → Bluejay OTel ``voice.call`` trace.

  voice.call (SERVER)                      bluejay.simulation_result_id, gen_ai.*
    ├── customer.speech / agent.speech     transcript fragments grouped by gap
    ├── chat <backend model> (CLIENT)      one per delegated Responses invocation,
    │                                      gen_ai.usage.* from response.completed
    └── execute_tool <name>                every backend function call, incl.
                                           handoffs and end_call (Bluejay reads these)

After the call: force_flush, settle, then one ``update-simulation-result`` POST
with ``trace_ids`` so Bluejay extracts tool actuals from the spans.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
from opentelemetry import trace as otel
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from live import FunctionCall, Observer

log = logging.getLogger("mivas.gpt-live.otel")

OTLP_ENDPOINT = os.environ.get("BLUEJAY_OTLP_ENDPOINT") or "https://otlp.getbluejay.ai/v1/traces"
API_URL = (os.environ.get("BLUEJAY_API_URL") or "https://api.getbluejay.ai/v1").rstrip("/")
SERVICE = os.environ.get("BLUEJAY_SERVICE_NAME", "mivas-openai-gpt-live")
SETTLE_S = float(os.environ.get("MIVAS_UPSERT_SETTLE_SECONDS", "10"))
# Transcript fragments from one speaker closer than this are one utterance
# (guide "Estimate conversation turns": start at 1.5 s and tune).
UTTERANCE_GAP_MS = int(os.environ.get("GPT_LIVE_UTTERANCE_GAP_MS", "1500"))
_MAX_ATTR = 4000

_provider: TracerProvider | None = None


def api_key() -> str | None:
    return os.environ.get("BLUEJAY_API_KEY") or None


def provider() -> TracerProvider | None:
    global _provider
    key = api_key()
    if not key:
        return None
    if _provider is None:
        p = TracerProvider(resource=Resource.create({SERVICE_NAME: SERVICE}))
        p.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(OTLP_ENDPOINT, headers={"X-API-KEY": key}),
                max_queue_size=int(os.environ.get("MIVAS_OTEL_QUEUE", "32768")),
                max_export_batch_size=512,
                schedule_delay_millis=1000,
            )
        )
        otel.set_tracer_provider(p)
        _provider = p
        log.info("otel → %s service=%s", OTLP_ENDPOINT, SERVICE)
    return _provider


def cur_start(span: Span) -> int:
    attrs = getattr(span, "attributes", None) or {}
    return int(attrs.get("mivas.start_ms", 0))


def _clip(v: Any) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= _MAX_ATTR else s[: _MAX_ATTR - 3] + "..."


class Tracer(Observer):
    """One instance per call. ``open()`` before the session, ``close()`` after."""

    def __init__(self, simulation_result_id: str | None, *, model: str, backend_model: str) -> None:
        self.sim_id = simulation_result_id
        self.model = model
        self.backend_model = backend_model
        self._tracer = otel.get_tracer("mivas.openai.gpt-live")
        self.root: Span | None = None
        self.trace_id: str | None = None
        self._t0_ns = time.time_ns()  # session timeline zero ≈ session.started
        self._utt: dict[str, tuple[Span, list[str], int]] = {}  # role → (span, text, end_ms)
        self._tools: dict[str, Span] = {}
        self._chats: dict[str, Span] = {}
        self._backend_tokens = {"in": 0, "out": 0}

    # ---- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        attrs = {
            "gen_ai.operation.name": "voice.call",
            "gen_ai.system": "openai",
            "gen_ai.request.model": self.model,
            "mivas.backend.model": self.backend_model,
        }
        if self.sim_id:
            attrs["bluejay.simulation_result_id"] = self.sim_id
        self.root = self._tracer.start_span("voice.call", kind=SpanKind.SERVER, attributes=attrs)
        ctx = self.root.get_span_context()
        if ctx.is_valid:
            self.trace_id = format(ctx.trace_id, "032x")
        log.info("trace_id=%s sim=%s", self.trace_id, self.sim_id)

    def close(self) -> None:
        for role in list(self._utt):
            self._end_utterance(role)
        for span in list(self._tools.values()) + list(self._chats.values()):
            span.set_status(Status(StatusCode.OK))
            span.end()
        self._tools.clear()
        self._chats.clear()
        if self.root is not None:
            self.root.set_attribute("gen_ai.usage.input_tokens", self._backend_tokens["in"])
            self.root.set_attribute("gen_ai.usage.output_tokens", self._backend_tokens["out"])
            self.root.set_status(Status(StatusCode.OK))
            self.root.end()

    def _ctx(self):
        return otel.set_span_in_context(self.root) if self.root is not None else None

    def _wall_ns(self, session_ms: int) -> int:
        return self._t0_ns + int(session_ms) * 1_000_000

    # ---- Observer ----------------------------------------------------------

    def session_started(self, session: dict[str, Any]) -> None:
        self._t0_ns = time.time_ns()
        if self.root is not None and session.get("id"):
            self.root.set_attribute("gen_ai.session.id", str(session["id"]))

    def transcript(self, role: str, delta: str, start_ms: int, end_ms: int) -> None:
        if not delta:
            return
        cur = self._utt.get(role)
        if cur is not None and start_ms - cur[2] > UTTERANCE_GAP_MS:
            self._end_utterance(role)
            cur = None
        if cur is None:
            name = "customer.speech" if role == "user" else "agent.speech"
            span = self._tracer.start_span(
                name,
                context=self._ctx(),
                kind=SpanKind.INTERNAL,
                start_time=self._wall_ns(start_ms),
                attributes={"mivas.role": role, "mivas.start_ms": start_ms},
            )
            cur = (span, [], end_ms)
        span, parts, _ = cur
        parts.append(delta)
        self._utt[role] = (span, parts, max(end_ms, cur[2]))

    def _end_utterance(self, role: str) -> None:
        cur = self._utt.pop(role, None)
        if cur is None:
            return
        span, parts, end_ms = cur
        text = "".join(parts).strip()
        log.info("%s [%d-%dms] %s", "CALLER" if role == "user" else "AGENT ", cur_start(span), end_ms, text)
        span.set_attribute("mivas.transcript", _clip(text))
        span.set_attribute("mivas.end_ms", end_ms)
        span.set_status(Status(StatusCode.OK))
        span.end(end_time=self._wall_ns(end_ms))

    def response_created(self, response_id: str, delegation_id: str | None) -> None:
        if not response_id or response_id in self._chats:
            return
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "openai",
            "gen_ai.request.model": self.backend_model,
            "gen_ai.response.id": response_id,
        }
        if delegation_id:
            attrs["mivas.delegation_id"] = delegation_id
        self._chats[response_id] = self._tracer.start_span(
            f"chat {self.backend_model}", context=self._ctx(), kind=SpanKind.CLIENT, attributes=attrs
        )

    def backend_text(self, response_id: str | None, text: str) -> None:
        log.info("BACKEND [%s] %s", response_id, text)
        span = self._chats.get(response_id or "")
        if span is not None:
            span.set_attribute("gen_ai.output.messages", _clip([{"role": "assistant", "content": text}]))

    def response_done(self, response: dict[str, Any], delegation_id: str | None) -> None:
        rid = response.get("id") or ""
        span = self._chats.pop(rid, None)
        if span is None:
            return
        usage = response.get("usage") or {}
        for key, path in (
            ("gen_ai.usage.input_tokens", ("input_tokens",)),
            ("gen_ai.usage.output_tokens", ("output_tokens",)),
            ("gen_ai.usage.total_tokens", ("total_tokens",)),
            ("gen_ai.usage.cached_tokens", ("input_tokens_details", "cached_tokens")),
            ("gen_ai.usage.output_reasoning_tokens", ("output_tokens_details", "reasoning_tokens")),
        ):
            v: Any = usage
            for k in path:
                v = v.get(k) if isinstance(v, dict) else None
            if isinstance(v, int):
                span.set_attribute(key, v)
        self._backend_tokens["in"] += int(usage.get("input_tokens") or 0)
        self._backend_tokens["out"] += int(usage.get("output_tokens") or 0)
        if response.get("model"):
            span.set_attribute("gen_ai.response.model", str(response["model"]))
        status = response.get("status")
        if status == "failed":
            span.set_status(Status(StatusCode.ERROR, _clip(response.get("error") or "failed")))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end()

    def tool_start(self, call: FunctionCall) -> None:
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": call.name,
            "gen_ai.tool.call.arguments": _clip(call.arguments),
            "mivas.tool.call_id": call.call_id,
        }
        if call.response_id:
            attrs["gen_ai.response.id"] = call.response_id
        self._tools[call.call_id] = self._tracer.start_span(
            f"execute_tool {call.name}", context=self._ctx(), kind=SpanKind.INTERNAL, attributes=attrs
        )

    def tool_end(self, call: FunctionCall, result: dict[str, Any]) -> None:
        span = self._tools.pop(call.call_id, None)
        if span is None:
            return
        span.set_attribute("gen_ai.tool.call.result", _clip(result))
        ok = result.get("success", result.get("ok", True)) is not False
        span.set_status(Status(StatusCode.OK) if ok else Status(StatusCode.ERROR, _clip(result.get("error") or "")))
        span.end()

    def handoff(self, from_stage: str, to_stage: str) -> None:
        if self.root is not None:
            self.root.add_event("handoff", {"from": from_stage, "to": to_stage})

    def usage(self, seconds: float | None, usage_ratio: float | None) -> None:
        if self.root is None:
            return
        if seconds is not None:
            self.root.set_attribute("mivas.live.usage_seconds", float(seconds))
        if usage_ratio is not None:
            self.root.set_attribute("mivas.live.context_usage_ratio", float(usage_ratio))

    def error(self, error: dict[str, Any]) -> None:
        if self.root is not None:
            self.root.add_event("live.error", {k: _clip(v) for k, v in error.items() if v is not None})

    def closed(self, reason: str, usage_seconds: float | None) -> None:
        if self.root is not None:
            self.root.set_attribute("mivas.close_reason", reason)
            if usage_seconds is not None:
                self.root.set_attribute("mivas.live.usage_seconds", float(usage_seconds))


# ---- Bluejay link -----------------------------------------------------------

async def post_trace_ids(simulation_result_id: str, trace_id: str) -> None:
    """Link once, after flush + settle. Relinking re-extracts spans and doubles tool rows."""
    key = api_key()
    if not key:
        return
    if _provider is not None:
        _provider.force_flush()
    await asyncio.sleep(SETTLE_S)
    body = {"simulation_result_id": str(simulation_result_id), "trace_ids": [trace_id]}
    async with httpx.AsyncClient(timeout=20) as client:
        for attempt in range(4):
            try:
                r = await client.post(
                    f"{API_URL}/update-simulation-result",
                    json=body,
                    headers={"X-API-Key": key, "Content-Type": "application/json"},
                )
            except httpx.TransportError as e:
                log.warning("update-simulation-result transport error (%s/4): %s", attempt + 1, e)
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code < 400:
                log.info("update-simulation-result ok sim=%s trace=%s", simulation_result_id, trace_id)
                return
            if r.status_code not in (429, 500, 502, 503, 504):
                break
            await asyncio.sleep(2**attempt)
        log.error("update-simulation-result FAILED %s %s", r.status_code, r.text[:300])
