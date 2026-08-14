"""16 kHz pcm websocket bridge ↔ Vapi, plus the tool webhook Vapi calls back on.

Both are served by one FastAPI app so a single cloudflared tunnel covers them:
`/` is the CHIRP socket Bluejay dials, `/tool/{name}` is what Vapi POSTs when the
squad runs `schedule_appointment`. Vapi's websocket transport is raw 16 kHz
pcm_s16le both directions, so no resampling either way.

ElevenLabs Flash dumps TTS in irregular bursts. Bluejay timestamps CHIRP frames
by arrival, so those bursts record as choppy speech — outbound PCM is paced at
realtime 20 ms frames. Inbound CHIRP is re-chunked to the same size for Flux.

Turn boundaries still cannot use a silence-gap heuristic — `agent.speech` is
bracketed by Vapi's `speech-update` (role=assistant, started/stopped) instead.
`customer.speech` comes from Bluejay's inbound `speech.started`/`speech.completed`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    begin_session,
    bind_provider,
    end_session,
    ensure_squad,
    for_provider,
    industry_path,
    load_blueprint,
    run_tool,
    start_websocket_call,
    unbind_provider,
    webhook_tool_names,
)
from pcm import PcmPacer, take_frames  # noqa: E402
from report import (  # noqa: E402
    bind_call,
    end_speech_span,
    finish_tool_span,
    start_speech_span,
    tool_span,
    traced_run,
)

app = FastAPI(title="mivas vapi chirp bridge")
_cfg: dict[str, Any] = {}


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


def _msg_type(event: dict) -> str | None:
    """Vapi sends server messages either bare or wrapped in `message`."""
    return event.get("type") or ((event.get("message") or {}).get("type") if isinstance(event.get("message"), dict) else None)


def _payload(event: dict) -> dict:
    inner = event.get("message")
    return inner if isinstance(inner, dict) and inner.get("type") else event


@app.post("/tool/{name}")
async def tool_webhook(name: str, request: Request) -> dict[str, Any]:
    """Vapi tool-calls webhook → run_tool (which emits the execute_tool span).

    Spans and state both key on `message.call.id` (the conversation), so any number
    of calls can be in flight. Note the entries of `toolCallList` carry their own
    ids — those identify the tool call, not the conversation.
    """
    body = await request.json()
    message = _payload(body)
    vapi_call_id = ((message.get("call") or {}).get("id")) or None
    for_provider(vapi_call_id)
    calls = message.get("toolCallList") or []
    results = []
    for call in calls:
        # live payload nests name/arguments under `function`; top level is the fallback
        fn = call.get("function") or {}
        args = fn.get("arguments") or call.get("arguments") or {}
        if isinstance(args, str):
            with contextlib.suppress(json.JSONDecodeError):
                args = json.loads(args)
        tool_name = fn.get("name") or call.get("name") or name
        result = await run_tool(
            tool_name, dict(args), call_id=call.get("id"), vapi_call_id=vapi_call_id
        )
        print(f"chirp tool {tool_name} args={args} -> {result}", flush=True)
        results.append({"toolCallId": call.get("id"), "result": json.dumps(result)})
    return {"results": results}


@app.websocket("/")
async def chirp(ws: WebSocket) -> None:
    expected = _auth()
    if expected and ws.headers.get("authorization") != expected:
        await ws.close(1008, "unauthorized")
        return
    await ws.accept()
    try:
        await _bridge(ws)
    except Exception as e:
        print(f"chirp bridge error: {type(e).__name__}: {e}", flush=True)
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


async def _bridge(ws: WebSocket) -> None:
    model, industry = _cfg["model"], _cfg["industry"]
    workflow = f"mivas-{Path(industry_path(industry)).name}-{model}"
    sim_id = ws.headers.get("x-simulation-result-id")
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)

    session_key = uuid.uuid4().hex
    resolved = begin_session(sim_id, session_key=session_key)
    provider_id: str | None = None

    utt: str | None = None
    speech_otel = None
    customer_otel = None
    seen_tools: set[str] = set()
    end = asyncio.Event()
    audio_in = audio_out = 0

    try:
        async with traced_run(workflow, simulation_result_id=sim_id, model=model):
            call_url, call_id = await asyncio.to_thread(start_websocket_call, _cfg["squad_id"])
            # webhook tool calls arrive with no OTel/ContextVar context; this is how they
            # find this bridge's trace root instead of whichever one started last
            bind_call(call_id)
            bind_provider(call_id, resolved)
            provider_id = call_id
            print(f"chirp vapi call={call_id}", flush=True)
            async with websockets.connect(call_url, max_size=None) as vapi_ws:
                pacer = PcmPacer(ws.send_bytes)
                pacer_task = asyncio.create_task(pacer.run())

                async def inbound() -> None:
                    """chirp pcm + Bluejay speech.* → Vapi; customer.speech spans."""
                    nonlocal customer_otel, audio_in
                    in_buf = bytearray()
                    try:
                        while not end.is_set():
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                break
                            if msg.get("bytes"):
                                audio_in += len(msg["bytes"])
                                for frame in take_frames(in_buf, msg["bytes"]):
                                    await vapi_ws.send(frame)
                                continue
                            if not msg.get("text"):
                                continue
                            try:
                                event = json.loads(msg["text"])
                            except json.JSONDecodeError:
                                continue
                            etype = event.get("type")
                            data = event.get("data") or {}
                            if etype == "speech.started":
                                end_speech_span(customer_otel)
                                customer_otel = start_speech_span(
                                    data.get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}",
                                    speaker="customer",
                                )
                            elif etype == "speech.completed":
                                end_speech_span(customer_otel)
                                customer_otel = None
                    finally:
                        end_speech_span(customer_otel)
                        customer_otel = None
                        end.set()
                        with contextlib.suppress(Exception):
                            await vapi_ws.close()

                async def outbound() -> None:
                    """Vapi audio + events → chirp pcm, speech.*, agent.speech spans."""
                    nonlocal utt, speech_otel, audio_out
                    try:
                        async for raw in vapi_ws:
                            if end.is_set():
                                break
                            if isinstance(raw, bytes):
                                audio_out += len(raw)
                                pacer.push(raw)
                                continue
                            try:
                                event = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            etype = _msg_type(event)
                            body = _payload(event)
                            if etype == "speech-update" and body.get("role") == "assistant":
                                if body.get("status") == "started" and utt is None:
                                    utt = f"u_{uuid.uuid4().hex[:12]}"
                                    speech_otel = start_speech_span(utt, speaker="agent")
                                    await ws.send_text(_event("speech.started", {"utterance_id": utt}))
                                elif body.get("status") == "stopped" and utt:
                                    # drain paced audio before closing the utterance
                                    await pacer.wait_until_idle()
                                    await ws.send_text(_event("speech.completed", {"utterance_id": utt}))
                                    end_speech_span(speech_otel)
                                    speech_otel, utt = None, None
                            elif etype == "conversation-update":
                                _trace_server_tools(body, seen_tools)
                            elif etype == "transcript" and body.get("transcriptType") == "final":
                                print(
                                    f"chirp transcript [{body.get('role')}] {body.get('transcript')}",
                                    flush=True,
                                )
                            elif etype in ("hangup", "end-of-call-report"):
                                print(f"chirp vapi {etype}", flush=True)
                                break
                            elif etype == "status-update" and body.get("status") == "ended":
                                print(f"chirp vapi ended: {body.get('endedReason')}", flush=True)
                                break
                            elif etype == "error":
                                print(f"chirp vapi error: {body}", flush=True)
                                break
                    finally:
                        pacer.close()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(pacer_task, timeout=5)
                        if utt:
                            await ws.send_text(_event("speech.completed", {"utterance_id": utt}))
                            end_speech_span(speech_otel)
                            speech_otel, utt = None, None
                        end.set()
                        with contextlib.suppress(Exception):
                            await ws.close(1000)

                tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                pacer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(pacer_task, timeout=5)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                print(f"chirp audio in={audio_in}B out={audio_out}B", flush=True)
                for t in done:
                    exc = None if t.cancelled() else t.exception()
                    if exc is not None and not type(exc).__name__.startswith("ConnectionClosed"):
                        raise exc
    finally:
        unbind_provider(provider_id)
        end_session(session_key)


def _trace_server_tools(body: dict, seen: set[str]) -> None:
    """Handoff and endCall run entirely server-side, so their only trace is the
    tool_calls entry in `conversation-update`. Emit a zero-width execute_tool span
    the first time each shows up so the timeline shows the full flow."""
    for message in body.get("messages") or []:
        for call in message.get("toolCalls") or message.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = fn.get("name") or call.get("name")
            key = str(call.get("id") or name)
            # webhook tools already have a live span from /tool/{name}
            if not name or name in _cfg.get("webhook_tools", ()) or key in seen:
                continue
            seen.add(key)
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                with contextlib.suppress(json.JSONDecodeError):
                    args = json.loads(args)
            print(f"chirp vapi server tool {name}", flush=True)
            with tool_span(name, args) as span:
                # tool_span already records name + arguments; finish_tool_span takes
                # only (span, output, *, ok).
                finish_tool_span(span, {"success": True, "source": "vapi"}, ok=True)


def main(model: str | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=model or os.environ.get("VAPI_MODEL", "vapi-flux-gpt4.1-flash2.5"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8770")))
    a = p.parse_args()
    public_url = os.environ.get("PUBLIC_URL", "").strip()
    if not public_url:
        raise SystemExit("need PUBLIC_URL (cloudflared https url) — Vapi tool webhooks point at it")

    ids = ensure_squad(a.industry, public_url)
    _cfg.update(
        model=a.model,
        industry=a.industry,
        squad_id=ids["squad_id"],
        webhook_tools=webhook_tool_names(load_blueprint(a.industry)),
    )
    print(
        f"ws↔Vapi {a.model} × {a.industry} :{a.port} auth={bool(_auth())} "
        f"squad={ids['squad_id']} tools→{public_url}/tool/",
        flush=True,
    )
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
