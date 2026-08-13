"""optional 16 kHz pcm websocket bridge ↔ Gemini Live (24 kHz out)."""

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

import google.genai as genai
from google.genai import types
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import industry_path, live_config, load_blueprint, run_tool, set_call_id  # noqa: E402
from report import end_speech_span, start_speech_span, traced_run  # noqa: E402

W, R_OUT, R_CHIRP = 2, 24_000, 16_000


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


async def _bridge(ws, model: str, industry: str) -> None:
    bp = load_blueprint(industry)
    state = {"agent": bp["start"]}
    down = None
    utt: str | None = None
    speech_otel = None
    end = asyncio.Event()
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{model}"
    sim_id = _simulation_result_id(ws)
    # One state-API namespace per call: concurrent digital humans must not share
    # an identity pin or a DB.
    set_call_id(sim_id)
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)

    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    config = live_config(bp)

    customer_otel = None

    def _close_utt() -> None:
        nonlocal utt, speech_otel
        end_speech_span(speech_otel)
        speech_otel = None
        utt = None

    def _close_customer() -> None:
        nonlocal customer_otel
        end_speech_span(customer_otel)
        customer_otel = None

    async with traced_run(workflow, simulation_result_id=sim_id, model=model):
        async with client.aio.live.connect(model=model, config=config) as session:
            # Live waits for a turn before speaking — nudge the scripted greeting.
            await session.send_realtime_input(
                text="[Call connected. Greet the caller now per your instructions.]"
            )

            async def inbound() -> None:
                nonlocal customer_otel
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=msg, mime_type="audio/pcm;rate=16000"
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

            async def outbound() -> None:
                nonlocal down, utt, speech_otel
                try:
                    while not end.is_set():
                        async for response in session.receive():
                            if end.is_set():
                                break
                            if response.data:
                                if utt is None:
                                    utt = f"u_{uuid.uuid4().hex[:12]}"
                                    speech_otel = start_speech_span(utt, speaker="agent")
                                    await ws.send(
                                        _event("speech.started", {"utterance_id": utt})
                                    )
                                pcm, down = audioop.ratecv(
                                    response.data, W, 1, R_OUT, R_CHIRP, down
                                )
                                if pcm:
                                    await ws.send(pcm)
                            sc = response.server_content
                            if sc is not None and getattr(sc, "turn_complete", False) and utt:
                                await ws.send(
                                    _event("speech.completed", {"utterance_id": utt})
                                )
                                _close_utt()
                            if response.tool_call:
                                replies = []
                                should_end = False
                                for fc in response.tool_call.function_calls or []:
                                    args = dict(fc.args or {})
                                    result, stop = await run_tool(
                                        fc.name,
                                        args,
                                        bp,
                                        state,
                                        call_id=getattr(fc, "id", None),
                                    )
                                    should_end = should_end or stop
                                    replies.append(
                                        types.FunctionResponse(
                                            id=fc.id, name=fc.name, response=result
                                        )
                                    )
                                if replies:
                                    await session.send_tool_response(
                                        function_responses=replies
                                    )
                                if should_end:
                                    end.set()
                                    break
                        else:
                            continue
                        break
                finally:
                    if utt:
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
    p.add_argument("--model", default=model or os.environ.get("GEMINI_LIVE_MODEL"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    a = p.parse_args()
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not a.model or not key:
        raise SystemExit("need --model/GEMINI_LIVE_MODEL and GOOGLE_API_KEY")
    industry_path(a.industry)
    print(f"ws↔Gemini {a.model} × {a.industry} :{a.port} auth={bool(_auth())}", flush=True)

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.model, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
