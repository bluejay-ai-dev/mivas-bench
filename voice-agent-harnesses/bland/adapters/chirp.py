"""CHIRP (16 kHz PCM) ↔ Bland stream-v2 (44.1 kHz PCM), plus the tool webhook.

Bland's browser transport is undocumented; this is what probing it established:
raw binary PCM16 mono in **both** directions with no JSON envelope, at **44100 Hz**
(16 kHz in is transcribed as noise), interleaved with JSON status frames
(`{"event":"update","payload":{"type":"assistant"|"human","text":...}}`,
`{"event":"mark"}`). So both directions are resampled with `audioop.ratecv`.

Agent audio is *continuous* — Bland streams silence between turns — so a
silence-gap-on-frames heuristic can't bracket `agent.speech`; an RMS gate can.

One uvicorn app serves both halves so a single cloudflared tunnel covers them:
`/` is CHIRP, `/tool/{name}` is what Bland's pathway Webhook nodes call, which is
where `execute_tool` spans get their real timing.
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

import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    begin_session,
    bind_provider,
    end_session,
    ensure_agent,
    for_provider,
    industry_path,
    load_blueprint,
    provider_id_from_request,
    run_tool,
    session_ws_url,
)
from pcm import PcmPacer  # noqa: E402
from report import end_speech_span, start_speech_span, traced_run  # noqa: E402

W, R_BLAND, R_CHIRP = 2, 44_100, 16_000
# Bland streams silence between turns, so agent turns are bracketed by loudness.
# Hysteresis: a high open threshold, a low hold threshold, and a long hang so
# intra-turn pauses and pathway webhooks do not punch holes in the CHIRP stream.
AGENT_RMS_ON = int(os.environ.get("BLAND_AGENT_RMS_ON", "400"))
# same as ON: Bland's analog silence floor is often 60–150 RMS. A lower hold
# threshold never ends the turn, so Bluejay counts the next user dump as
# agent punctuation (Dana 25829 ms).
AGENT_RMS_OFF = int(os.environ.get("BLAND_AGENT_RMS_OFF", "400"))
AGENT_SILENCE_S = float(os.environ.get("BLAND_AGENT_SILENCE_S", "0.8"))

app = FastAPI(title="mivas bland chirp bridge")
CFG: dict[str, str] = {}


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


@app.post("/tool/{name}")
async def tool_webhook(name: str, request: Request) -> dict:
    """Bland's pathway Webhook nodes call this server-side, mid-call.

    ponytail: the span binds to report.py's module-level active root, i.e. the one
    in-flight call. Benchmark runs are max_concurrent=1; carry the sim id in the
    node body and look the root up per call if that ever stops being true.
    """
    try:
        args = await request.json()
    except Exception:
        args = {}
    for_provider(provider_id_from_request(args, query=request.query_params, headers=request.headers))
    result = await run_tool(name, {k: v for k, v in (args or {}).items() if v not in (None, "")})
    print(f"chirp tool {name} args={args} -> {result}", flush=True)
    return result


@app.websocket("/")
async def chirp(ws: WebSocket) -> None:
    if (expected := _auth()) and ws.headers.get("authorization") != expected:
        await ws.close(1008, "unauthorized")
        return
    await ws.accept()
    sim_id = ws.headers.get("x-simulation-result-id")
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)
    try:
        await _bridge(ws, sim_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"chirp bridge error: {type(e).__name__}: {e}", flush=True)
        with contextlib.suppress(Exception):
            await ws.close(1011, "bridge error")


async def _bridge(ws: WebSocket, sim_id: str | None) -> None:
    model, industry = CFG["model"], CFG["industry"]
    workflow = f"mivas-{Path(industry_path(industry)).name}-{model}"
    url = await session_ws_url(CFG["agent_id"])
    end = asyncio.Event()
    sent = {"to_bland": 0, "to_chirp": 0}
    session_key = uuid.uuid4().hex
    resolved = begin_session(sim_id, session_key=session_key)

    try:
        async with traced_run(workflow, simulation_result_id=sim_id, model=model):
            async with websockets.connect(url, max_size=None) as bland:

                async def inbound() -> None:
                    """chirp 16 kHz pcm → Bland 44.1 kHz; Bluejay speech.* → customer.speech."""
                    up = None
                    customer = None

                    def _close_customer() -> None:
                        nonlocal customer
                        end_speech_span(customer)
                        customer = None

                    try:
                        while not end.is_set():
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                break
                            if (pcm := msg.get("bytes")):
                                out, up = audioop.ratecv(pcm, W, 1, R_CHIRP, R_BLAND, up)
                                if out:
                                    await bland.send(out)
                                    sent["to_bland"] += len(out)
                                continue
                            if not msg.get("text"):
                                continue
                            try:
                                event = json.loads(msg["text"])
                            except json.JSONDecodeError:
                                continue
                            etype = event.get("type")
                            if etype == "speech.started":
                                _close_customer()
                                uid = (event.get("data") or {}).get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                                customer = start_speech_span(uid, speaker="customer")
                            elif etype == "speech.completed":
                                _close_customer()
                    finally:
                        _close_customer()
                        end.set()
                        with contextlib.suppress(Exception):
                            await bland.close()

                async def outbound() -> None:
                    """Bland 44.1 kHz pcm → chirp 16 kHz; RMS gate drives agent.speech.

                    Bland stalls across JSON frames and webhook nodes. While a turn
                    is open the pacer keeps a 20 ms clock (real PCM or silence).
                    Between turns we send nothing so Bluejay does not treat hold
                    noise as an endless agent utterance.
                    """
                    down = None
                    utt: str | None = None
                    speech = None
                    last_loud = 0.0
                    pacer = PcmPacer(ws.send_bytes)
                    pace_task = asyncio.create_task(pacer.run())

                    async def _close_utt() -> None:
                        nonlocal utt, speech
                        if utt:
                            pacer.hold(False)
                            await pacer.wait_until_idle(timeout=2.0)
                            with contextlib.suppress(Exception):
                                await ws.send_text(_event("speech.completed", {"utterance_id": utt}))
                        end_speech_span(speech)
                        utt, speech = None, None

                    try:
                        async for msg in bland:
                            if end.is_set():
                                break
                            if isinstance(msg, str):
                                # `payload` is a dict for update frames, a bare string for callID.
                                # do not close the turn here: a JSON stall with no PCM is
                                # exactly when the pacer must keep filling 20 ms frames.
                                payload = (json.loads(msg) or {}).get("payload")
                                if isinstance(payload, str) and payload.strip():
                                    bind_provider(payload.strip(), resolved)
                                    print(f"bland callID={payload.strip()}", flush=True)
                                elif isinstance(payload, dict) and payload.get("text"):
                                    print(f"bland {payload.get('type')}: {payload['text']}", flush=True)
                                await asyncio.sleep(0)
                                continue
                            if not msg:
                                continue
                            now = time.monotonic()
                            rms = audioop.rms(msg, W)
                            if rms >= AGENT_RMS_ON or (utt is not None and rms >= AGENT_RMS_OFF):
                                last_loud = now
                                if utt is None:
                                    utt = f"u_{uuid.uuid4().hex[:12]}"
                                    speech = start_speech_span(utt, speaker="agent")
                                    pacer.hold(True)
                                    await ws.send_text(_event("speech.started", {"utterance_id": utt}))
                            elif utt and now - last_loud > AGENT_SILENCE_S:
                                await _close_utt()
                            out, down = audioop.ratecv(msg, W, 1, R_BLAND, R_CHIRP, down)
                            if utt is not None and out:
                                pacer.push(out)
                                sent["to_chirp"] += len(out)
                            await asyncio.sleep(0)
                    finally:
                        await _close_utt()
                        pacer.close()
                        with contextlib.suppress(Exception):
                            await pace_task
                        end.set()
                        with contextlib.suppress(Exception):
                            await ws.close(1000)

                tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                print(
                    f"chirp audio bytes to_bland={sent['to_bland']} to_chirp={sent['to_chirp']}",
                    flush=True,
                )
                for t in done:
                    exc = None if t.cancelled() else t.exception()
                    if exc is not None and not type(exc).__name__.startswith(
                        ("ConnectionClosed", "WebSocketDisconnect")
                    ):
                        raise exc
    finally:
        end_session(session_key)


def main(model: str | None = None) -> None:
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--model", default=model or os.environ.get("BLAND_MODEL", "base"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8772")))
    p.add_argument("--public-url", default=os.environ.get("PUBLIC_URL", ""))
    a = p.parse_args()
    if not a.public_url:
        raise SystemExit("need PUBLIC_URL (the https cloudflared URL) — Bland calls tools back over it")

    ids = ensure_agent(a.industry, a.public_url)
    CFG.update(model=a.model, industry=a.industry, agent_id=ids["agent_id"])
    print(
        f"ws↔Bland {a.model} × {a.industry} :{a.port} auth={bool(_auth())} "
        f"agent={ids['agent_id']} pathway={ids['pathway_id']} tools={a.public_url}/tool/*",
        flush=True,
    )
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
