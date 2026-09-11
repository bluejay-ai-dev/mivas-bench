"""GPT-Live (`gpt-live-1`) WebSocket session.

Wire contract: the GA guides — "Connect over WebSockets" (voice-websockets),
"Delegation and tools" (live-delegation), "Usage and graceful close"
(live-conversations). Transport-neutral: audio in and out are raw PCM bytes at the
session's sample rate; whoever owns the caller leg (CHIRP, SIP, a test) plugs in.

One session per call. Multi-agent packs are soft handoffs on that session: the
backend (Responses) stage is swapped with ``session.update`` and the live model
gets a ``session.instructions.append`` role-change notice. History is kept by
the service; nothing is replayed.

Tool loop (Responses delegation):
  session.delegation.created ─┐
  response.event{response.created}          → open delegation
  response.event{response.output_item.done} → collect completed function_call
  response.event{response.completed}        → run collected calls, then
      response.item.create(function_call_output) × n,
      [session.update + session.instructions.append if a call was a handoff],
      then response.create

Full duplex: the model handles barge-in itself. This client never mutes,
cancels, or gates audio in either direction.
"""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from pack import Pack, Stage, today_line

log = logging.getLogger("mivas.gpt-live")

MODEL = "gpt-live-1"
LIVE_URL = os.environ.get("GPT_LIVE_URL", "wss://api.openai.com/v1/live/sessions")
BACKEND_MODEL = os.environ.get("GPT_LIVE_BACKEND_MODEL", "gpt-5.6-terra")
VOICE = os.environ.get("GPT_LIVE_VOICE", "gleam")
# 24 kHz is the documented default; 16 kHz PCM is a supported format and is what
# CHIRP carries, so picking it keeps the caller leg resample-free in both directions.
SAMPLE_RATE = int(os.environ.get("GPT_LIVE_SAMPLE_RATE", "16000"))

ACK_TIMEOUT_S = float(os.environ.get("GPT_LIVE_ACK_TIMEOUT_S", "15"))
# After end_call: hang up once the agent has been quiet this long (farewell done), bounded.
END_CALL_QUIET_S = float(os.environ.get("GPT_LIVE_END_CALL_QUIET_S", "1.5"))
END_CALL_MAX_S = float(os.environ.get("GPT_LIVE_END_CALL_MAX_S", "20"))
# ...and if no farewell audio starts at all within this window, hang up anyway.
END_CALL_GRACE_S = float(os.environ.get("GPT_LIVE_END_CALL_GRACE_S", "4"))
CLOSED_TIMEOUT_S = float(os.environ.get("GPT_LIVE_CLOSED_TIMEOUT_S", "6"))
# Appends are capped at 500 tokens; ~3 chars/token keeps a safe margin.
APPEND_MAX_CHARS = 1400
AUDIBLE_PEAK = 300  # int16 peak below this is silence for hang-up timing only

_QUIET_EVENTS = {"session.output_audio.delta", "session.input_transcript.delta", "session.output_transcript.delta"}

ToolRunner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
AudioSink = Callable[[bytes], Awaitable[None]]


class LiveError(RuntimeError):
    pass


@dataclass
class FunctionCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    response_id: str | None
    delegation_id: str | None


@dataclass
class Delegation:
    delegation_id: str | None
    response_id: str | None
    pending: dict[str, asyncio.Task[dict[str, Any]]] = field(default_factory=dict)
    after_handoff: bool = False
    said_something: bool = False


class Observer:
    """No-op hooks. tracing.Tracer implements them; tests may too."""

    def session_started(self, session: dict[str, Any]) -> None: ...
    def transcript(self, role: str, delta: str, start_ms: int, end_ms: int) -> None: ...
    def delegation_created(self, delegation: dict[str, Any], offset_ms: int) -> None: ...
    def response_created(self, response_id: str, delegation_id: str | None) -> None: ...
    def response_done(self, response: dict[str, Any], delegation_id: str | None) -> None: ...
    def backend_text(self, response_id: str | None, text: str) -> None: ...
    def tool_start(self, call: FunctionCall) -> None: ...
    def tool_end(self, call: FunctionCall, result: dict[str, Any]) -> None: ...
    def handoff(self, from_stage: str, to_stage: str) -> None: ...
    def usage(self, seconds: float | None, usage_ratio: float | None) -> None: ...
    def error(self, error: dict[str, Any]) -> None: ...
    def closed(self, reason: str, usage_seconds: float | None) -> None: ...


def _eid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _chunks(text: str, limit: int = APPEND_MAX_CHARS) -> list[str]:
    out: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out or [""]


def _peak(pcm: bytes) -> int:
    if len(pcm) < 2:
        return 0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    return max(abs(min(samples)), abs(max(samples)))


class LiveSession:
    def __init__(
        self,
        pack: Pack,
        *,
        api_key: str,
        on_audio: AudioSink,
        run_tool: ToolRunner,
        observer: Observer | None = None,
        model: str = MODEL,
        backend_model: str = BACKEND_MODEL,
        voice: str = VOICE,
        sample_rate: int = SAMPLE_RATE,
        clock: str | None = None,
    ) -> None:
        self.pack = pack
        self.stage: Stage = pack.stages[pack.start]
        self.model = model
        self.backend_model = backend_model
        self.voice = voice
        self.sample_rate = sample_rate
        self.clock = clock or today_line()
        self.on_audio = on_audio
        self.run_tool = run_tool
        self.obs = observer or Observer()
        self._api_key = api_key
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._delegations: dict[str, Delegation] = {}  # keyed by delegation_id or response_id
        self._latest_delegation: Delegation | None = None
        self._answered: set[str] = set()
        self._handoff_target: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last_audible_mono = 0.0
        self._ending: asyncio.Event = asyncio.Event()
        # Set when the backend finishes a turn with no function calls after end_call:
        # that turn is the summary the live model will speak as the farewell.
        self._backend_done: asyncio.Event = asyncio.Event()
        self._hangup_started = False
        self._closed: asyncio.Event = asyncio.Event()
        self._close_sent = False
        self.session_id: str | None = None
        self.usage_seconds: float | None = None
        self.close_reason: str | None = None

    # ---- lifecycle ---------------------------------------------------------

    def session_config(self) -> dict[str, Any]:
        start = self.stage
        return {
            "model": self.model,
            "instructions": self.pack.live_instructions(self.clock),
            "audio": {
                "format": {"type": "audio/pcm", "rate": self.sample_rate},
                "output": {"voice": self.voice},
            },
            "delegation": {
                "type": "responses",
                "responses": {
                    "model": self.backend_model,
                    "instructions": start.backend_instructions(self.clock),
                    "tools": start.responses_tools(),
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                },
            },
        }

    async def open(self) -> None:
        """Connect, ``session.start``, wait for ``session.started``."""
        self._ws = await connect(
            LIVE_URL,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        self._pump = asyncio.create_task(self._pump_events(), name="gpt-live-pump")
        started = await self._command(
            {"type": "session.start", "session": self.session_config()},
            ack="session.started",
        )
        session = started.get("session") or {}
        self.session_id = session.get("id")
        self.obs.session_started(session)
        log.info(
            "session.started id=%s stage=%s backend=%s rate=%s",
            self.session_id, self.stage.name, self.backend_model, self.sample_rate,
        )

    async def speak_first(self) -> None:
        """GA guide, "Ask the model to speak first": one fresh session.instructions.append
        with ``delegation_id: null``. Not commentary — commentary is paraphrased, and the
        pack's greeting is fixed text the benchmark compares against. Sent, not awaited
        (see ``_post``): the caller's first audio has not arrived yet at this point."""
        for chunk in _chunks(self.pack.speak_first_prompt()):
            await self._post(
                {"type": "session.instructions.append", "delegation_id": None, "content": chunk},
                ack="session.instructions.appended",
            )

    async def send_audio(self, pcm: bytes) -> None:
        if not pcm or self._closed.is_set():
            return
        await self._send(
            {"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode("ascii")}
        )

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def hang_up(self, reason: str = "harness") -> None:
        """Graceful close: ``session.close`` then read through ``session.closed``."""
        if self._close_sent or self._ws is None:
            return
        self._close_sent = True
        log.info("session.close reason=%s", reason)
        with contextlib.suppress(Exception):
            await self._send({"type": "session.close", "event_id": _eid("close")})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._closed.wait(), CLOSED_TIMEOUT_S)
        if not self._closed.is_set():
            log.warning("no session.closed within %.0fs; closing transport", CLOSED_TIMEOUT_S)
            self._finish("transport_timeout")

    async def aclose(self) -> None:
        self._finish(self.close_reason or "harness")
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        for task in list(self._tasks):
            task.cancel()
        pump = getattr(self, "_pump", None)
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(BaseException):
                await pump

    def _spawn(self, coro: Awaitable[Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Wait for in-flight tool/continue/hang-up tasks (tests, shutdown).

        Append reports are excluded: they resolve only when the service says the
        text was injected, which may be after the call is over.
        """
        while True:
            work = [t for t in self._tasks if t.get_name() != "gpt-live-append"]
            if not work:
                return
            await asyncio.gather(*work, return_exceptions=True)

    def _finish(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self.close_reason = self.close_reason or reason
        self._closed.set()
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(LiveError(f"session closed ({reason})"))
        self._waiters.clear()

    # ---- sending -----------------------------------------------------------

    async def _send(self, event: dict[str, Any]) -> None:
        if event.get("type") != "session.input_audio.append" and log.isEnabledFor(logging.DEBUG):
            log.debug("-> %s", json.dumps(event)[:600])
        async with self._send_lock:
            await self._ws.send(json.dumps(event, separators=(",", ":")))

    async def _post(self, event: dict[str, Any], *, ack: str) -> None:
        """Send a ``*.append`` and keep going.

        ``session.instructions.appended`` is not a protocol ack: it reports where the
        text landed on the conversation timeline (``start_ms``/``end_ms``) and only
        fires once the timeline reaches that point, which needs caller audio to be
        flowing. Awaiting it stalls the greeting until the first caller audio and can
        stall a handoff for as long as the caller stays silent. Measured against GA
        on 2026-09-10: appended arrives ~0.7 s after audio starts, and a session
        closed before then answers with ``server_error / context_injection_incomplete``.
        Frames stay ordered on the wire, so a following ``response.create`` still
        arrives after the append.
        """
        eid = event.setdefault("event_id", _eid(event["type"].replace(".", "_")))
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[eid] = fut

        async def report() -> None:
            try:
                ev = await fut
            except LiveError as e:
                log.warning("%s not applied: %s", event["type"], e)
            except asyncio.CancelledError:
                raise
            else:
                log.debug("%s at %s-%sms", ack, ev.get("start_ms"), ev.get("end_ms"))
            finally:
                self._waiters.pop(eid, None)

        self._spawn(report(), name="gpt-live-append")
        await self._send(event)

    async def _command(self, event: dict[str, Any], *, ack: str) -> dict[str, Any]:
        """Send with an event_id and wait for its acknowledgment (or correlated error)."""
        eid = event.setdefault("event_id", _eid(event["type"].replace(".", "_")))
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[eid] = fut
        await self._send(event)
        try:
            return await asyncio.wait_for(fut, ACK_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise LiveError(f"no {ack} for {event['type']} within {ACK_TIMEOUT_S}s") from e
        finally:
            self._waiters.pop(eid, None)

    # ---- receiving ---------------------------------------------------------

    async def _pump_events(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    await self._handle(json.loads(raw))
                except Exception:
                    log.exception("event handler failed")
        except ConnectionClosed as e:
            log.info("transport closed: %s", e)
            self._finish("transport_closed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event pump crashed")
            self._finish("pump_error")
        else:
            self._finish("transport_closed")

    async def _handle(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t not in _QUIET_EVENTS and log.isEnabledFor(logging.DEBUG):
            log.debug("<- %s", json.dumps(ev)[:1500])
        if t == "session.output_audio.delta":
            pcm = base64.b64decode(ev["delta"])
            if _peak(pcm) >= AUDIBLE_PEAK:
                self._last_audible_mono = time.monotonic()
            await self.on_audio(pcm)
        elif t == "session.output_transcript.delta":
            self.obs.transcript("assistant", ev.get("delta", ""), ev.get("start_ms", 0), ev.get("end_ms", 0))
        elif t == "session.input_transcript.delta":
            self.obs.transcript("user", ev.get("delta", ""), ev.get("start_ms", 0), ev.get("end_ms", 0))
        elif t == "session.delegation.created":
            d = ev.get("delegation") or {}
            self._delegation_for(d.get("id"), d.get("response_id"))
            self.obs.delegation_created(d, int(ev.get("offset_ms") or 0))
        elif t == "response.event":
            await self._responses_event(ev.get("delegation_id"), ev.get("event") or {})
        elif t == "session.usage.updated":
            self.usage_seconds = (ev.get("usage") or {}).get("seconds", self.usage_seconds)
            self.obs.usage(self.usage_seconds, (ev.get("context_window") or {}).get("usage_ratio"))
        elif t == "session.closed":
            self.usage_seconds = (ev.get("usage") or {}).get("seconds", self.usage_seconds)
            self.close_reason = str(ev.get("reason") or "closed")
            self.obs.closed(self.close_reason, self.usage_seconds)
            self._finish(self.close_reason)
        elif t == "error":
            err = ev.get("error") or {}
            self.obs.error(err)
            cid = err.get("client_event_id")
            fut = self._waiters.get(cid) if cid else None
            if fut is not None and not fut.done():
                fut.set_exception(LiveError(f"{err.get('code')}: {err.get('message')}"))
            else:
                log.error("live error %s: %s (param=%s)", err.get("code"), err.get("message"), err.get("param"))
        else:
            cid = ev.get("client_event_id")
            fut = self._waiters.get(cid) if cid else None
            if fut is not None and not fut.done():
                fut.set_result(ev)

    def _delegation_for(self, delegation_id: str | None, response_id: str | None) -> Delegation:
        for key in (delegation_id, response_id):
            if key and key in self._delegations:
                d = self._delegations[key]
                d.response_id = response_id or d.response_id
                d.delegation_id = delegation_id or d.delegation_id
                break
        else:
            if delegation_id is None and response_id is None and self._latest_delegation is not None:
                return self._latest_delegation
            d = Delegation(delegation_id=delegation_id, response_id=response_id)
        for key in (d.delegation_id, d.response_id):
            if key:
                self._delegations[key] = d
        self._latest_delegation = d
        return d

    async def _responses_event(self, delegation_id: str | None, inner: dict[str, Any]) -> None:
        it = inner.get("type") or ""
        if it == "response.created":
            rid = (inner.get("response") or {}).get("id")
            self._delegation_for(delegation_id, rid)
            self.obs.response_created(rid or "", delegation_id)
        elif it == "response.output_item.done":
            item = inner.get("item") or {}
            if item.get("type") == "message":
                text = " ".join(
                    c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)
                ).strip()
                d = self._delegation_for(delegation_id, None)
                if text:
                    d.said_something = True
                    self.obs.backend_text(d.response_id, text)
                return
            if item.get("type") != "function_call":
                return
            if item.get("status") not in (None, "completed"):
                return
            call_id = item.get("call_id")
            if not call_id or call_id in self._answered:
                return
            self._answered.add(call_id)
            d = self._delegation_for(delegation_id, None)
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            call = FunctionCall(call_id, item.get("name") or "", args, d.response_id, d.delegation_id)
            d.pending[call_id] = self._spawn(self._execute(call), name=f"tool-{call.name}")
        elif it in ("response.completed", "response.incomplete", "response.failed"):
            resp = inner.get("response") or {}
            d = self._delegation_for(delegation_id, resp.get("id"))
            self.obs.response_done(resp, delegation_id)
            if d.pending or d.said_something:
                d.said_something = False
            elif d.after_handoff:
                log.warning(
                    "backend returned no text and no calls after handoff (response=%s status=%s "
                    "output_tokens=%s); live model has nothing to relay",
                    resp.get("id"), resp.get("status"), (resp.get("usage") or {}).get("output_tokens"),
                )
            d.after_handoff = False
            if it == "response.failed":
                log.error("backend response failed: %s", resp.get("error"))
            if d.pending:
                # Own task: the pump must keep reading, or the acks _continue waits
                # for (session.updated, *.appended) are never read.
                self._spawn(self._continue(d), name="gpt-live-continue")
            elif self._ending.is_set():
                self._backend_done.set()

    async def _continue(self, d: Delegation) -> None:
        """All outputs for this response, then one explicit response.create."""
        pending, d.pending = d.pending, {}
        results = await asyncio.gather(*pending.values())
        for call_id, result in zip(pending, results):
            await self._send(
                {
                    "type": "response.item.create",
                    "event_id": _eid("tool_result"),
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    },
                }
            )
        if self._handoff_target:
            target, self._handoff_target = self._handoff_target, None
            d.after_handoff = True
            try:
                await self._handoff(target)
            except LiveError:
                log.exception("handoff to %s failed; backend keeps stage %s", target, self.stage.name)
        await self._send({"type": "response.create", "event_id": _eid("continue")})
        if self._ending.is_set() and not self._hangup_started:
            self._hangup_started = True
            self._spawn(self._hang_up_after_farewell(), name="gpt-live-hangup")

    # ---- tools -------------------------------------------------------------

    async def _execute(self, call: FunctionCall) -> dict[str, Any]:
        tool = self.stage.tool(call.name) or next(
            (t for st in self.pack.stages.values() for t in st.tools if t.name == call.name), None
        )
        self.obs.tool_start(call)
        log.info("tool %s(%s) stage=%s", call.name, json.dumps(call.arguments), self.stage.name)
        try:
            if tool is None:
                result: dict[str, Any] = {"success": False, "error": f"unknown tool: {call.name}"}
            elif tool.is_handoff:
                # Applied in _continue: the service queues session.update behind the
                # response that is awaiting this call's output, so awaiting the ack
                # here deadlocks until timeout. Output first, then update, then continue.
                self._handoff_target = tool.handoff_to or ""
                # The continuation resumes under the target stage's instructions; say so
                # in the result so the backend answers the caller's pending request
                # instead of treating the transfer itself as the outcome.
                result = {
                    "success": True,
                    "to_agent": self._handoff_target,
                    "note": (
                        f"Transfer complete. You are now the {self._handoff_target} stage and "
                        "your instructions have been replaced. The caller is still on the line "
                        "with the same request; continue it per your new instructions and "
                        "return what the voice assistant should say next."
                    ),
                }
            elif tool.session:
                result = {"success": True} if call.name == "end_call" else await self.run_tool(call.name, call.arguments)
                self._ending.set()
            else:
                result = await self.run_tool(call.name, call.arguments)
        except Exception as e:  # never leave a function call unanswered
            log.exception("tool %s failed", call.name)
            result = {"success": False, "error": f"{type(e).__name__}: {e}"}
        self.obs.tool_end(call, result)
        return result

    async def _handoff(self, target: str) -> dict[str, Any]:
        stage = self.pack.stages[target]
        await self._command(
            {
                "type": "session.update",
                "session": {
                    "delegation": {
                        "type": "responses",
                        "responses": {
                            "instructions": stage.backend_instructions(self.clock),
                            "tools": stage.responses_tools(),
                        },
                    }
                },
            },
            ack="session.updated",
        )
        for chunk in _chunks(stage.handoff_notice()):
            await self._post(
                {"type": "session.instructions.append", "delegation_id": None, "content": chunk},
                ack="session.instructions.appended",
            )
        prev, self.stage = self.stage, stage
        self.obs.handoff(prev.name, target)
        log.info("handoff %s → %s", prev.name, target)
        return {"success": True, "to_agent": target}

    async def _hang_up_after_farewell(self) -> None:
        """end_call → backend finishes its summary turn → live model speaks it → close.

        There is no output-audio-done event, so the spoken part is bounded by audio
        activity: wait for speech to start (grace), then for it to go quiet.
        """
        t0 = time.monotonic()
        deadline = t0 + END_CALL_MAX_S
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._backend_done.wait(), max(0.0, deadline - time.monotonic()))
        t1 = time.monotonic()
        while time.monotonic() < deadline and not self._closed.is_set():
            now = time.monotonic()
            spoke = self._last_audible_mono >= t0
            if spoke and now - self._last_audible_mono >= END_CALL_QUIET_S:
                break
            if not spoke and now - t1 >= END_CALL_GRACE_S:
                break
            await asyncio.sleep(0.1)
        await self.hang_up("end_call")
