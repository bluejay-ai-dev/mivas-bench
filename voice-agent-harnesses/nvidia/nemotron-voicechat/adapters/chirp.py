"""CHIRP (16 kHz pcm) ↔ Nemotron VoiceChat Realtime WebSocket (24 kHz).

Protocol: OpenAI Realtime-compatible events (session.update, input_audio_buffer.append,
response.output_audio.delta, response.function_call_arguments.done, …).
"""

from __future__ import annotations

import argparse
import audioop
import asyncio
import base64
import contextlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import websockets
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness import industry_path, load_blueprint  # noqa: E402
from report import end_speech_span, start_speech_span, traced_run  # noqa: E402
from voicechat import (  # noqa: E402
    MODEL,
    SAMPLE_RATE,
    handle_function_call,
    session_update,
    ws_url,
)

W, R_VC, R_CHIRP = 2, SAMPLE_RATE, 16_000
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "2.5"))


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


def _eid() -> str:
    return str(uuid.uuid4())


def _simulation_result_id(ws) -> str | None:
    headers = getattr(getattr(ws, "request", None), "headers", None)
    if headers is None:
        return None
    val = headers.get("X-Simulation-Result-Id") or headers.get("x-simulation-result-id")
    return str(val).strip() if val else None


async def _close_soon(vc_ws, ws, end: asyncio.Event) -> None:
    await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
    end.set()
    with contextlib.suppress(Exception):
        await vc_ws.send(json.dumps({"type": "session.close", "event_id": _eid()}))
    with contextlib.suppress(Exception):
        await vc_ws.close()
    with contextlib.suppress(Exception):
        await ws.close(1000)


async def _bridge(ws, industry: str) -> None:
    bp = load_blueprint(industry)
    state = {"agent": bp["start"]}
    end = asyncio.Event()
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{MODEL}"
    sim_id = _simulation_result_id(ws)
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)

    url = ws_url()
    update = session_update(bp)

    async with traced_run(workflow, simulation_result_id=sim_id, model=MODEL):
        async with websockets.connect(url) as vc_ws:
            # Drain session.created, then configure.
            raw = await asyncio.wait_for(vc_ws.recv(), timeout=60)
            print(f"voicechat {json.loads(raw).get('type')}", flush=True)
            await vc_ws.send(json.dumps(update))

            async def inbound() -> None:
                up = None
                customer_otel = None

                def _close_customer() -> None:
                    nonlocal customer_otel
                    end_speech_span(customer_otel)
                    customer_otel = None

                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            pcm, up = audioop.ratecv(msg, W, 1, R_CHIRP, R_VC, up)
                            if pcm:
                                await vc_ws.send(
                                    json.dumps(
                                        {
                                            "type": "input_audio_buffer.append",
                                            "event_id": _eid(),
                                            "audio": base64.b64encode(pcm).decode("ascii"),
                                        }
                                    )
                                )
                            continue
                        if not isinstance(msg, str):
                            continue
                        try:
                            event = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        data = event.get("data") or {}
                        if etype == "speech.started":
                            _close_customer()
                            uid = data.get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                            customer_otel = start_speech_span(uid, speaker="customer")
                        elif etype == "speech.completed":
                            _close_customer()
                finally:
                    _close_customer()
                    end.set()
                    with contextlib.suppress(Exception):
                        await vc_ws.send(
                            json.dumps({"type": "session.close", "event_id": _eid()})
                        )
                    with contextlib.suppress(Exception):
                        await vc_ws.close()

            async def outbound() -> None:
                down = None
                utt: str | None = None
                speech_otel = None

                def _close_utt() -> None:
                    nonlocal utt, speech_otel
                    end_speech_span(speech_otel)
                    speech_otel = None
                    utt = None

                try:
                    async for raw in vc_ws:
                        if end.is_set():
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")

                        if etype == "response.output_audio.delta":
                            b64 = event.get("delta") or ""
                            if not b64:
                                continue
                            pcm24 = base64.b64decode(b64)
                            if utt is None:
                                utt = f"u_{uuid.uuid4().hex[:12]}"
                                speech_otel = start_speech_span(utt, speaker="agent")
                                await ws.send(
                                    _event("speech.started", {"utterance_id": utt})
                                )
                            pcm, down = audioop.ratecv(pcm24, W, 1, R_VC, R_CHIRP, down)
                            if pcm:
                                await ws.send(pcm)

                        elif etype in {
                            "response.output_audio.done",
                            "response.done",
                        } and utt:
                            await ws.send(
                                _event("speech.completed", {"utterance_id": utt})
                            )
                            _close_utt()

                        elif etype == "response.function_call_arguments.done":
                            result, stop, reply = await handle_function_call(
                                event.get("name", ""),
                                event.get("arguments") or "{}",
                                event.get("call_id", ""),
                                bp,
                                state,
                            )
                            print(
                                f"voicechat tool {event.get('name')} -> {result.get('success')}",
                                flush=True,
                            )
                            await vc_ws.send(json.dumps(reply))
                            if stop:
                                asyncio.create_task(_close_soon(vc_ws, ws, end))

                        elif etype == "error":
                            print(f"voicechat error: {event}", flush=True)
                        elif etype == "session.end":
                            end.set()
                            break
                finally:
                    if utt is not None:
                        with contextlib.suppress(Exception):
                            await ws.send(
                                _event("speech.completed", {"utterance_id": utt})
                            )
                        _close_utt()
                    end.set()
                    with contextlib.suppress(Exception):
                        await ws.close(1000)

            tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if isinstance(exc, BaseException) and not type(exc).__name__.startswith(
                    "ConnectionClosed"
                ):
                    raise exc


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


def main(model: str | None = None) -> None:
    _ = model
    p = argparse.ArgumentParser()
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    p.add_argument("--model", default=MODEL)
    a = p.parse_args()
    industry_path(a.industry)
    print(
        f"ws↔VoiceChat {a.model} × {a.industry} chirp=:{a.port} "
        f"upstream={ws_url()} auth={bool(_auth())}",
        flush=True,
    )

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
