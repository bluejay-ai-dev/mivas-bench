"""16 kHz pcm websocket bridge ↔ Retell web call (LiveKit transport).

Retell has no audio websocket of its own any more: `POST /v2/create-web-call`
hands back a LiveKit JWT and the call lives in a LiveKit room. So we publish a
microphone track fed by CHIRP pcm and subscribe to the agent's track for the
return path. LiveKit resamples both directions for us (`AudioSource(16000)` /
`AudioStream(sample_rate=16000)`), so there is no `audioop` here.

Agent audio frames are continuous, so agent turns are bracketed by Retell's
`agent_start_talking` / `agent_stop_talking` data messages, not by frame gaps.

One FastAPI app serves both halves on one port so a single cloudflared tunnel
covers them: `/` is the CHIRP websocket, `/tool/{name}` is the Retell webhook
that runs `schedule_appointment` (Retell tools execute platform-side, so the
`execute_tool` span is born there).
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

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from typing import Any
from livekit import rtc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    begin_session,
    bind_provider,
    create_web_call,
    end_session,
    ensure_agent,
    for_provider,
    industry_path,
    livekit_url,
    load_blueprint,
    report_platform_tools,
    run_tool,
    unbind_provider,
)
from report import (  # noqa: E402
    end_speech_span,
    start_speech_span,
    traced_run,
)

RATE, CHANNELS = 16_000, 1
# Below this, an agent "turn" is a Retell state blip, not speech.
BLIP_S = 0.15
app = FastAPI(title="mivas retell chirp bridge")
# ponytail: one agent id + one active call per process — benchmark runs are
# sequential (max_concurrent=1). Per-call routing needs a call_id → root map.
CFG: dict[str, Any] = {}


@app.post("/tool/{name}")
async def tool_webhook(name: str, request: Request) -> dict:
    """Retell custom tool → `{call, name, args}`; the return value goes to the LLM."""
    body = await request.json()
    args = dict(body.get("args") or {})
    call_id = (body.get("call") or {}).get("call_id")
    for_provider(call_id)
    result = await run_tool(name, args, call_id=call_id)
    print(f"chirp tool {name} args={args} -> {result}", flush=True)
    return result


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


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
    sim_id = ws.headers.get("x-simulation-result-id")
    sim_id = str(sim_id).strip() if sim_id else None
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)

    session_key = uuid.uuid4().hex
    resolved = begin_session(sim_id, session_key=session_key)
    call = create_web_call(CFG["agent_id"])
    bind_provider(call["call_id"], resolved)
    print(f"chirp retell call_id={call['call_id']}", flush=True)

    end = asyncio.Event()
    outq: asyncio.Queue[bytes | str] = asyncio.Queue()
    agent_otel = None
    agent_utt: str | None = None
    agent_started: float = 0.0
    customer_otel = None
    agent_bytes = 0

    try:
        async with traced_run(CFG["workflow"], simulation_result_id=sim_id, model=CFG["model"]):
            room = rtc.Room()

            def _agent_start() -> None:
                nonlocal agent_otel, agent_utt, agent_started
                if agent_utt is not None:
                    return
                agent_utt = f"u_{uuid.uuid4().hex[:12]}"
                agent_started = time.monotonic()
                agent_otel = start_speech_span(agent_utt, speaker="agent")
                outq.put_nowait(_event("speech.started", {"utterance_id": agent_utt}))

            def _agent_stop(*, force: bool = False) -> None:
                """Retell fires a spurious start/stop pair ~250 ms before each real turn;
                a sub-BLIP_S utterance is that artifact, so hold the span open for the
                real audio instead of littering the waterfall with 20 ms spans."""
                nonlocal agent_otel, agent_utt
                if agent_utt is None:
                    return
                if not force and time.monotonic() - agent_started < BLIP_S:
                    return
                end_speech_span(agent_otel)
                outq.put_nowait(_event("speech.completed", {"utterance_id": agent_utt}))
                agent_otel, agent_utt = None, None

            @room.on("data_received")
            def _on_data(packet: rtc.DataPacket) -> None:
                try:
                    event = json.loads(bytes(packet.data).decode())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return
                etype = event.get("event_type")
                if etype == "agent_start_talking":
                    _agent_start()
                elif etype == "agent_stop_talking":
                    _agent_stop()
                elif etype not in ("update", None):
                    print(f"chirp retell data {etype}", flush=True)

            @room.on("track_subscribed")
            def _on_track(track: rtc.Track, *_a) -> None:
                if track.kind == rtc.TrackKind.KIND_AUDIO:
                    asyncio.create_task(_pump(track))

            @room.on("disconnected")
            def _on_disconnected(*_a) -> None:
                end.set()

            @room.on("participant_disconnected")
            def _on_participant_gone(*_a) -> None:
                end.set()

            async def _pump(track: rtc.Track) -> None:
                nonlocal agent_bytes
                async for ev in rtc.AudioStream(track, sample_rate=RATE, num_channels=CHANNELS):
                    pcm = bytes(ev.frame.data)
                    agent_bytes += len(pcm)
                    outq.put_nowait(pcm)

            await room.connect(
                livekit_url(), call["access_token"], options=rtc.RoomOptions(auto_subscribe=True)
            )
            source = rtc.AudioSource(RATE, CHANNELS)
            await room.local_participant.publish_track(
                rtc.LocalAudioTrack.create_audio_track("chirp", source),
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )

            async def inbound() -> None:
                """chirp pcm → LiveKit mic track; Bluejay speech.* → customer.speech spans."""
                nonlocal customer_otel
                caller_bytes = 0
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        pcm = msg.get("bytes")
                        if pcm:
                            caller_bytes += len(pcm)
                            await source.capture_frame(
                                rtc.AudioFrame(pcm, RATE, CHANNELS, len(pcm) // 2)
                            )
                            continue
                        text = msg.get("text")
                        if not text:
                            continue
                        try:
                            event = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        if etype == "speech.started":
                            end_speech_span(customer_otel)
                            uid = (event.get("data") or {}).get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                            customer_otel = start_speech_span(uid, speaker="customer")
                        elif etype == "speech.completed":
                            end_speech_span(customer_otel)
                            customer_otel = None
                finally:
                    end_speech_span(customer_otel)
                    customer_otel = None
                    print(f"chirp caller_bytes={caller_bytes}", flush=True)
                    end.set()

            async def sender() -> None:
                """single queue keeps speech.* text frames ordered against agent pcm."""
                while True:
                    item = await outq.get()
                    if isinstance(item, bytes):
                        await ws.send_bytes(item)
                    else:
                        await ws.send_text(item)

            tasks = [
                asyncio.create_task(inbound()),
                asyncio.create_task(sender()),
                asyncio.create_task(end.wait()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            _agent_stop(force=True)
            end_speech_span(customer_otel)
            await room.disconnect()
            print(f"chirp agent_bytes={agent_bytes}", flush=True)
            # the edge transition and end_call ran inside Retell — read them off the
            # call record so they land on the waterfall next to the webhook tool.
            backfilled = await report_platform_tools(call["call_id"], CFG["bp"])
            print(f"chirp platform tools {backfilled}", flush=True)
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    raise t.exception()  # type: ignore[misc]
    finally:
        unbind_provider(call["call_id"])
        end_session(session_key)


def main(model: str | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=model or os.environ.get("RETELL_RUNTIME", "retell"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8771")))
    a = p.parse_args()

    public_url = os.environ.get("PUBLIC_URL", "").strip()
    if not public_url:
        raise SystemExit("need PUBLIC_URL (cloudflared https url) — Retell calls tools over HTTPS")

    industry_dir = industry_path(a.industry)
    ids = ensure_agent(industry_dir, public_url)
    CFG.update(
        agent_id=ids["agent_id"],
        bp=load_blueprint(industry_dir),
        model=a.model,
        workflow=f"mivas-{Path(industry_dir).name}-{a.model}",
    )
    print(
        f"ws↔Retell {a.model} × {a.industry} :{a.port} auth={bool(_auth())} "
        f"agent={ids['agent_id']} llm={ids['llm_id']} tools→{public_url}/tool/*",
        flush=True,
    )
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
