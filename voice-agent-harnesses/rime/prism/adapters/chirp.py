"""CHIRP (16 kHz pcm_s16le) <-> Rime Prism (16 kHz in / 24 kHz out).

Audio policy:
  - Forward inbound DH PCM once the Prism session is ready, continuously
    (Prism's turn detection wants silence too). Never gate on agent speech.
  - Prism owns turn detection and barge-in. On input_audio_buffer.speech_started
    the pacer drops the queued reply audio and the harness reports how much
    the caller heard with conversation.item.truncate.
  - Reply audio arrives faster than realtime; PcmPacer sends it at 16 kHz
    realtime so Bluejay's arrival-timestamped recording is neither compressed
    nor choppy, and "bytes sent" doubles as "milliseconds heard".
  - Speak-first: one nudge (response.create, prism_cause proactive) carrying
    the pack greeting, re-sent until Prism produces audio.

Tool rounds follow the Prism contract exactly: every
response.function_call_arguments.done is dispatched, its result is returned as
a function_call_output, and the continuation is requested only after the
parent's response.done plus one conversation.item.created per result, so a
second write in the same reply is never dropped.

Soft multi-agent: one Prism WebSocket per call; a handoff tool returns
{"success": true, "role": <agent>} and the harness swaps instructions + tools
with session.update before continuing. The handoff tool itself is recorded as
an execute_tool span so the verifier sees the hop.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import contextlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "runtime"))  # repo runtime/ when not in-pod
from chirp_health import process_request as _health  # noqa: E402
from harness import (  # noqa: E402
    INPUT_RATE,
    MODEL,
    OUTPUT_RATE,
    connect_prism,
    greeting_nudge,
    handle_function_call,
    handoff_role,
    industry_path,
    load_blueprint,
    log_ws_accept,
    session_update_for_agent,
    set_call_id,
    tool_continuation,
    ws_url,
)
from pcm import PcmPacer  # noqa: E402
from report import traced_run  # noqa: E402

W, R_IN, R_OUT = 2, INPUT_RATE, OUTPUT_RATE
BYTES_PER_MS = R_IN * W // 1000  # 32 bytes of 16 kHz s16le per millisecond
NUDGE_RETRY_DELAY_S = float(os.environ.get("MIVAS_NUDGE_RETRY_DELAY_S", "3"))
NUDGE_MAX_ATTEMPTS = int(os.environ.get("MIVAS_NUDGE_MAX_ATTEMPTS", "5"))
# A session tool (end_call, transfer_to_human) lets the agent say its farewell
# through one tool continuation; the socket closes once that audio has played,
# or at this cap if Prism never replies.
FAREWELL_MAX_S = float(os.environ.get("MIVAS_FAREWELL_MAX_S", "20"))
SESSION_SETUP_TIMEOUT_S = float(os.environ.get("RIME_SESSION_SETUP_TIMEOUT_S", "10"))
CONNECT_RETRY_S = float(os.environ.get("RIME_CONNECT_RETRY_S", "12"))

_LOG_ON = (os.environ.get("RIME_CHIRP_LOG") or "full").strip().lower() not in {"0", "off", "false", "no"}


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


def _simulation_result_id(ws) -> str | None:
    headers = getattr(getattr(ws, "request", None), "headers", None)
    if headers is None:
        return None
    val = headers.get("X-Simulation-Result-Id") or headers.get("x-simulation-result-id")
    return str(val).strip() if val else None


def _eid() -> str:
    return uuid.uuid4().hex[:12]


class _Round:
    """One Prism reply that made tool calls: continue after done + every ack."""

    __slots__ = ("calls", "acked", "done", "continued", "handoff", "stop")

    def __init__(self) -> None:
        self.calls: set[str] = set()
        self.acked: set[str] = set()
        self.done = False
        self.continued = False
        self.handoff: str | None = None
        self.stop = False

    def ready(self) -> bool:
        return self.done and bool(self.calls) and self.calls <= self.acked and not self.continued


async def _connect_with_retry(log) -> tuple[Any, Any]:
    """Open the Prism socket, retrying an upgrade refused with 429 or 503.

    Prism answers 503 when it has no room and 429 "Account admission refused"
    when the account's concurrent-conversation allowance is used up. Both are
    transient from the caller's point of view, so keep trying for a bounded
    window instead of handing the digital human a dead line at once."""
    from websockets.exceptions import InvalidStatus

    deadline = time.monotonic() + CONNECT_RETRY_S
    attempt = 0
    while True:
        attempt += 1
        cm = connect_prism()
        try:
            prism = await cm.__aenter__()
            return prism, cm
        except InvalidStatus as e:
            status = e.response.status_code
            body = (e.response.body or b"")[:120].decode(errors="replace").strip()
            log(f"prism upgrade refused HTTP {status} {body!r} (attempt {attempt})")
            if status not in (429, 503) or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(min(1.0 * attempt, 3.0))


async def _bridge(ws, industry: str) -> None:
    bp = load_blueprint(industry)
    state: dict[str, Any] = {"agent": bp["start"]}
    end = asyncio.Event()
    sim_id = _simulation_result_id(ws)
    set_call_id(sim_id)
    log_ws_accept(sim_id)
    t_accept = time.monotonic()

    def _ms() -> int:
        return int((time.monotonic() - t_accept) * 1000)

    def _log(msg: str) -> None:
        if _LOG_ON:
            print(f"t+{_ms()}ms sim={sim_id or '-'} {msg}", flush=True)

    workflow = f"mivas {Path(industry_path(industry)).name} {MODEL}".replace(".", "-").replace("/", " ")
    _log(f"chirp start={bp['start']} agents={list(bp['agents'])}")

    prism_cm = None
    pacer: PcmPacer | None = None
    pacer_task: asyncio.Task | None = None
    try:
        async with traced_run(workflow, simulation_result_id=sim_id, model=MODEL) as tracer:
            if tracer is not None:
                state["_otel_root"] = tracer.root

            prism, prism_cm = await _connect_with_retry(_log)
            _log("prism connected")
            created = json.loads(await asyncio.wait_for(prism.recv(), timeout=SESSION_SETUP_TIMEOUT_S))
            if created.get("type") != "session.created":
                raise RuntimeError(f"expected session.created, got {created.get('type')}")
            await prism.send(json.dumps(session_update_for_agent(bp, bp["start"], sim_id=sim_id)))
            while True:
                ev = json.loads(await asyncio.wait_for(prism.recv(), timeout=SESSION_SETUP_TIMEOUT_S))
                if tracer is not None:
                    tracer.handle_raw(ev.get("type"), ev)
                if ev.get("type") == "session.updated":
                    break
                if ev.get("type") == "error":
                    raise RuntimeError(f"session.update refused: {ev.get('error')}")
            ready = True
            _log(f"prism session ready agent={bp['start']}")

            opened = asyncio.Event()          # first reply audio reached the pacer
            typed_ready = asyncio.Event()     # prism.typed_input.ready seen since last clear
            updated_events: dict[str, asyncio.Event] = {}
            rounds: dict[str, _Round] = {}
            call_round: dict[str, str] = {}
            handled: set[str] = set()
            cancelled: set[str] = set()
            closing = {"parent": None, "this": None, "task": None}
            texts: dict[str, str] = {}
            play = {"item": None, "resp": None, "pushed": 0, "sent": 0}
            cur = {"resp": None, "msg_item": None, "down": None}
            turn = {"utt": None}

            async def _paced_send(frame: bytes) -> None:
                await ws.send(frame)
                play["sent"] += len(frame)

            pacer = PcmPacer(_paced_send)
            pacer_task = asyncio.create_task(pacer.run())

            async def send_prism(ev: dict[str, Any]) -> None:
                with contextlib.suppress(Exception):
                    await prism.send(json.dumps(ev))

            async def start_agent() -> None:
                if turn["utt"] is not None:
                    return
                turn["utt"] = f"u_{_eid()}"
                await ws.send(_event("speech.started", {"utterance_id": turn["utt"]}))

            async def end_agent() -> None:
                if turn["utt"] is None:
                    return
                with contextlib.suppress(Exception):
                    await ws.send(_event("speech.completed", {"utterance_id": turn["utt"]}))
                turn["utt"] = None

            async def close_all(why: str) -> None:
                if end.is_set():
                    return
                _log(f"closing: {why}")
                end.set()
                opened.set()
                with contextlib.suppress(Exception):
                    await end_agent()
                if pacer is not None:
                    pacer.close()
                with contextlib.suppress(Exception):
                    await prism.close()
                with contextlib.suppress(Exception):
                    await ws.close(1000)

            async def after_reply_audio(resp_id: str, drain_token: str | None) -> None:
                """speech.completed once the paced audio has left, then the playback receipt."""
                await pacer.wait_until_idle()
                if play["resp"] == resp_id:
                    await end_agent()
                    if drain_token and resp_id not in cancelled:
                        await send_prism({"type": "prism.playback.drained", "drain_token": drain_token})
                if closing["this"] == resp_id or (
                    closing["parent"] is not None and resp_id != closing["parent"]
                ):
                    await close_all("farewell played")

            async def on_speech_started(ev: dict[str, Any]) -> None:
                typed_ready.clear()
                resp_id = play["resp"]
                if resp_id is None or play["pushed"] <= play["sent"] and pacer._idle.is_set():
                    return
                heard_ms = play["sent"] // BYTES_PER_MS
                cancelled.add(resp_id)
                pacer.clear()
                await end_agent()
                if play["item"]:
                    await send_prism({
                        "type": "conversation.item.truncate",
                        "event_id": f"trunc_{_eid()}",
                        "item_id": play["item"],
                        "content_index": 0,
                        "audio_end_ms": int(heard_ms),
                    })
                _log(f"barge-in: truncated {play['item']} at {heard_ms}ms "
                     f"(pushed {play['pushed'] // BYTES_PER_MS}ms)")
                play["resp"] = None

            async def switch_agent(role: str) -> None:
                upd = session_update_for_agent(bp, role, mid_call=True)
                waiter = asyncio.Event()
                updated_events[upd["event_id"]] = waiter
                await send_prism(upd)
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=SESSION_SETUP_TIMEOUT_S)
                    _log(f"handoff applied: now {role}")
                except asyncio.TimeoutError:
                    _log(f"handoff session.update for {role} not acknowledged in time; continuing")
                finally:
                    updated_events.pop(upd["event_id"], None)

            async def maybe_continue(resp_id: str) -> None:
                """Request the tool continuation once the round is complete.

                Runs as its own task: switch_agent waits for a session.updated that
                only the outbound reader can deliver, so awaiting it inside the
                reader deadlocks until the timeout (10 s of dead air per handoff)."""
                r = rounds.get(resp_id)
                if r is None or not r.ready():
                    return
                r.continued = True
                if r.handoff:
                    await switch_agent(r.handoff)
                if r.stop:
                    async def farewell_cap() -> None:
                        await asyncio.sleep(FAREWELL_MAX_S)
                        await close_all("farewell cap")

                    closing["task"] = asyncio.create_task(farewell_cap())
                    if texts.get(resp_id, "").strip():
                        # The reply that called end_call already spoke the goodbye
                        # (Prism says it in the same reply); a continuation makes
                        # it say goodbye twice. Close once that audio has played.
                        closing["this"] = resp_id
                        _log(f"session tool with spoken farewell; closing after {resp_id} plays")
                        if play["resp"] != resp_id and pacer._idle.is_set():
                            await close_all("farewell already played")
                        return
                    closing["parent"] = resp_id
                await send_prism(tool_continuation(resp_id))
                _log(f"tool continuation sent parent={resp_id} agent={state['agent']}"
                     + (" (closing after farewell)" if r.stop else ""))

            def register_call(resp_id: str, call_id: str) -> bool:
                """Book the call on its round synchronously, in the reader. The
                response.done that follows function_call_arguments.done can be
                handled before a spawned task ever runs; a round registered late
                never sees its done flag and the continuation is never sent."""
                if call_id in handled:
                    return False
                handled.add(call_id)
                r = rounds.setdefault(resp_id, _Round())
                r.calls.add(call_id)
                call_round[call_id] = resp_id
                return True

            async def dispatch_tool(resp_id: str, call_id: str, name: str, arguments: str) -> None:
                r = rounds[resp_id]
                if tracer is not None:
                    state["_otel_root"] = tracer.current_turn()  # tools nest under the turn
                t0 = time.monotonic()
                result, stop, reply = await handle_function_call(name, arguments, call_id, bp, state)
                took = int((time.monotonic() - t0) * 1000)
                role = handoff_role(result, bp)
                if role:
                    r.handoff = role
                if stop:
                    r.stop = True
                await send_prism(reply)
                _log(f"tool {name} args={arguments[:160]} -> ok={bool(result.get('ok') or result.get('success'))} "
                     f"{took}ms agent={state['agent']}" + (f" handoff->{role}" if role else "")
                     + (" stop" if stop else ""))
                await maybe_continue(resp_id)

            async def nudge_until_open() -> None:
                for attempt in range(NUDGE_MAX_ATTEMPTS):
                    if opened.is_set() or end.is_set():
                        return
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(typed_ready.wait(), timeout=2.0)
                    if opened.is_set() or end.is_set():
                        return
                    typed_ready.clear()
                    await send_prism(greeting_nudge(bp))
                    _log(f"greeting nudge sent (attempt {attempt + 1})")
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(opened.wait(), timeout=NUDGE_RETRY_DELAY_S)

            async def inbound() -> None:
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg and ready:
                            # Prism input is 16 kHz pcm16: no resample.
                            await send_prism({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(msg).decode("ascii"),
                            })
                        # CHIRP speech.* frames are not traced; turn spans come
                        # from Prism's own input_audio_buffer.speech_started.
                finally:
                    end.set()

            async def outbound() -> None:
                try:
                    async for raw in prism:
                        if end.is_set():
                            break
                        try:
                            ev = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        et = ev.get("type")
                        if tracer is not None:
                            tracer.handle_raw(et, ev)
                        if et == "response.audio.delta":
                            b = ev.get("delta") or ""
                            rid = ev.get("response_id")
                            if not b or rid in cancelled:
                                continue
                            if play["resp"] != rid:
                                # A new reply's audio closes the previous utterance first, so
                                # Bluejay never sees two replies under one utterance id or an
                                # utterance that is never completed (its digital human would
                                # then wait on the agent and nudge "are you still there").
                                if turn["utt"] is not None:
                                    await pacer.wait_until_idle()
                                    await end_agent()
                                play.update(item=ev.get("item_id") or cur["msg_item"], resp=rid, pushed=0, sent=0)
                                if not opened.is_set():
                                    _log("first reply audio")
                                opened.set()
                            await start_agent()
                            pcm16, cur["down"] = audioop.ratecv(base64.b64decode(b), W, 1, R_OUT, R_IN, cur["down"])
                            if pcm16:
                                play["pushed"] += len(pcm16)
                                pacer.push(pcm16)
                        elif et == "response.created":
                            rid = (ev.get("response") or {}).get("id")
                            cur["resp"] = rid
                            cur["msg_item"] = None
                            typed_ready.clear()
                        elif et == "response.output_item.added":
                            item = ev.get("item") or {}
                            if item.get("type") == "message":
                                cur["msg_item"] = item.get("id")
                        elif et == "response.function_call_arguments.done":
                            rid = ev.get("response_id") or cur["resp"] or ""
                            cid = ev.get("call_id") or f"call_{_eid()}"
                            name = ev.get("name", "")
                            if name and register_call(rid, cid):
                                asyncio.create_task(dispatch_tool(rid, cid, name, ev.get("arguments") or "{}"))
                        elif et == "response.text.done":
                            tr = (ev.get("text") or "").strip()
                            texts[ev.get("response_id") or cur["resp"] or ""] = tr
                            if tr:
                                _log(f"prism[{state['agent']}] {tr[:200]}")
                        elif et == "conversation.item.input_audio_transcription.completed":
                            tr = (ev.get("transcript") or "").strip()
                            if tr:
                                _log(f"USER_ASR {tr[:200]}")
                        elif et == "input_audio_buffer.speech_started":
                            await on_speech_started(ev)
                        elif et == "response.done":
                            resp = ev.get("response") or {}
                            rid = resp.get("id") or cur["resp"] or ""
                            status = resp.get("status")
                            reason = (resp.get("status_details") or {}).get("reason")
                            usage = resp.get("usage") or {}
                            _log(f"response.done {rid} {status}/{reason} in={usage.get('input_tokens')} "
                                 f"out={usage.get('output_tokens')}")
                            r = rounds.get(rid)
                            if r is not None:
                                r.done = True
                                asyncio.create_task(maybe_continue(rid))
                            token = (resp.get("metadata") or {}).get("prism_drain_token")
                            if play["resp"] == rid or closing["this"] == rid or (
                                closing["parent"] is not None and rid != closing["parent"]
                            ):
                                asyncio.create_task(after_reply_audio(rid, token))
                        elif et == "conversation.item.created":
                            item = ev.get("item") or {}
                            cid = item.get("call_id")
                            if item.get("type") == "function_call_output" and cid in call_round:
                                rid = call_round[cid]
                                rounds[rid].acked.add(cid)
                                asyncio.create_task(maybe_continue(rid))
                        elif et == "session.updated":
                            waiter = updated_events.get(ev.get("prism_request_event_id") or "")
                            if waiter is not None:
                                waiter.set()
                        elif et == "prism.typed_input.ready":
                            typed_ready.set()
                        elif et == "session.idle_timeout":
                            _log("prism idle timeout")
                        elif et == "error":
                            err = ev.get("error") or {}
                            _log(f"prism error code={err.get('code')} scope={err.get('scope')} "
                                 f"event_id={err.get('event_id')} msg={str(err.get('message'))[:200]}")
                            if err.get("scope") == "session":
                                await close_all(f"session error {err.get('code')}")
                finally:
                    with contextlib.suppress(Exception):
                        await end_agent()
                    end.set()

            outbound_task = asyncio.create_task(outbound())
            inbound_task = asyncio.create_task(inbound())
            nudge_task = asyncio.create_task(nudge_until_open())
            await asyncio.wait({inbound_task, outbound_task}, return_when=asyncio.FIRST_COMPLETED)
            await close_all("socket ended")
            for t in (inbound_task, outbound_task, nudge_task, closing["task"]):
                if t is not None:
                    t.cancel()
            await asyncio.gather(
                inbound_task, outbound_task, nudge_task, return_exceptions=True
            )
    finally:
        if pacer is not None:
            pacer.close()
        if pacer_task is not None:
            pacer_task.cancel()
            await asyncio.gather(pacer_task, return_exceptions=True)
        if prism_cm is not None:
            with contextlib.suppress(Exception):
                await prism_cm.__aexit__(None, None, None)
        _log("chirp bridge finished")


async def _handler(ws, industry: str) -> None:
    expected = _auth()
    if expected and ws.request.headers.get("Authorization") != expected:
        await ws.close(1008, "unauthorized")
        return
    try:
        await _bridge(ws, industry)
    except Exception as e:
        if type(e).__name__.startswith("ConnectionClosed"):
            return
        print(f"chirp bridge error: {type(e).__name__}: {e}", flush=True)
        with contextlib.suppress(Exception):
            await ws.close(1011, "bridge error")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=MODEL)
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8770")))
    a = p.parse_args()
    industry_path(a.industry)
    if not os.environ.get("RIME_API_KEY", "").strip():
        raise SystemExit("need RIME_API_KEY")
    print(
        f"ws↔Prism {a.model} × {a.industry} :{a.port} upstream={ws_url()} auth={bool(_auth())}",
        flush=True,
    )

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.industry), a.host, a.port, process_request=_health):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
