"""optional 16 kHz pcm websocket bridge ↔ Deepgram Voice Agent (24 kHz)."""

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import WS_URL, industry_path, load_blueprint, run_tool, set_call_id, settings_payload  # noqa: E402
from report import end_speech_span, start_speech_span, traced_run  # noqa: E402

W, R_OUT, R_CHIRP = 2, 24_000, 16_000
# Let farewell audio finish before tearing down the agent session after end_call.
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "2.5"))


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


def _simulation_result_id(ws) -> str | None:
    """evaluator may send this on the websocket upgrade request."""
    headers = getattr(getattr(ws, "request", None), "headers", None)
    if headers is None:
        return None
    val = headers.get("X-Simulation-Result-Id") or headers.get("x-simulation-result-id")
    return str(val).strip() if val else None


def _strip_wav_header(data: bytes) -> bytes:
    """Defensive: Settings requests container=none, but strip a WAV header if one slips through."""
    if data[:4] == b"RIFF" and len(data) > 44:
        return data[44:]
    return data


async def _bridge(ws, model: str, industry: str) -> None:
    bp = load_blueprint(industry)
    state = {"agent": bp["start"]}
    end = asyncio.Event()
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{model}"
    sim_id = _simulation_result_id(ws)
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)
    set_call_id(sim_id)

    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise SystemExit("need DEEPGRAM_API_KEY")
    settings = settings_payload(bp, model)

    async with traced_run(workflow, simulation_result_id=sim_id, model=model):
        async with websockets.connect(
            WS_URL, additional_headers={"Authorization": f"Token {key}"}
        ) as dg_ws:
            await dg_ws.send(json.dumps(settings))

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
                            pcm, up = audioop.ratecv(msg, W, 1, R_CHIRP, R_OUT, up)
                            if pcm:
                                await dg_ws.send(pcm)
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
                        await dg_ws.close()

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
                    async for msg in dg_ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes):
                            if not msg:
                                continue
                            if utt is None:
                                utt = f"u_{uuid.uuid4().hex[:12]}"
                                speech_otel = start_speech_span(utt, speaker="agent")
                                await ws.send(_event("speech.started", {"utterance_id": utt}))
                            pcm, down = audioop.ratecv(
                                _strip_wav_header(msg), W, 1, R_OUT, R_CHIRP, down
                            )
                            if pcm:
                                await ws.send(pcm)
                            continue

                        try:
                            event = json.loads(msg)
                        except ValueError:
                            continue
                        etype = event.get("type")

                        if etype == "AgentStartedSpeaking" and utt is None:
                            utt = f"u_{uuid.uuid4().hex[:12]}"
                            speech_otel = start_speech_span(utt, speaker="agent")
                            await ws.send(_event("speech.started", {"utterance_id": utt}))
                        elif etype == "AgentAudioDone" and utt:
                            await ws.send(_event("speech.completed", {"utterance_id": utt}))
                            _close_utt()
                        elif etype == "FunctionCallRequest":
                            should_end = False
                            for fn in event.get("functions") or []:
                                name = fn.get("name")
                                try:
                                    args = json.loads(fn.get("arguments") or "{}")
                                except ValueError:
                                    args = {}
                                result, stop = await run_tool(
                                    name, args, bp, state, call_id=fn.get("id")
                                )
                                should_end = should_end or stop
                                await dg_ws.send(
                                    json.dumps(
                                        {
                                            "type": "FunctionCallResponse",
                                            "id": fn.get("id"),
                                            "name": name,
                                            "content": json.dumps(result),
                                        }
                                    )
                                )
                            if should_end:
                                asyncio.create_task(_close_soon(dg_ws, ws, end))
                        elif etype == "Error":
                            print(f"deepgram error: {event}", flush=True)
                finally:
                    if utt is not None:
                        with contextlib.suppress(Exception):
                            await ws.send(_event("speech.completed", {"utterance_id": utt}))
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


async def _close_soon(dg_ws, ws, end: asyncio.Event) -> None:
    """Hang up after a short delay so farewell audio can finish playing."""
    await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
    end.set()
    with contextlib.suppress(Exception):
        await dg_ws.close()
    with contextlib.suppress(Exception):
        await ws.close(1000)


async def _handler(ws, model: str, industry: str) -> None:
    expected = _auth()
    if expected and ws.request.headers.get("Authorization") != expected:
        await ws.close(1008, "unauthorized")
        return
    try:
        await _bridge(ws, model, industry)
    except Exception as e:
        if type(e).__name__.startswith("ConnectionClosed"):
            return
        print(f"chirp bridge error: {type(e).__name__}: {e}", flush=True)
        with contextlib.suppress(Exception):
            await ws.close(1011, "bridge error")


def main(model: str | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=model or os.environ.get("DEEPGRAM_VOICE_AGENT_MODEL", "deepgram-voice-agent"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    a = p.parse_args()
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise SystemExit("need DEEPGRAM_API_KEY")
    industry_path(a.industry)
    print(f"ws↔Deepgram {a.model} × {a.industry} :{a.port} auth={bool(_auth())}", flush=True)

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.model, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
