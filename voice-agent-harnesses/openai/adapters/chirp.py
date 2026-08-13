"""CHIRP (16 kHz pcm_s16le) ↔ OpenAI Realtime (24 kHz).

Tracing: Realtime session events are proxied through ``report.RealtimeEventTracer``
into a Bluejay OTel ``voice.call`` tree (turns, transcripts, tools, handoffs).
CHIRP inbound ``speech.*`` frames open ``customer.speech`` spans.
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

from agents.realtime import RealtimeModelSendRawMessage
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import build_from_blueprint, industry_path, log_ws_accept, set_call_id  # noqa: E402
from report import traced_run  # noqa: E402

W, R_IN, R_OUT = 2, 16_000, 24_000


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


def _simulation_result_id(ws) -> str | None:
    """Bluejay sends this on the CHIRP WebSocket upgrade request."""
    headers = getattr(getattr(ws, "request", None), "headers", None)
    if headers is None:
        return None
    val = headers.get("X-Simulation-Result-Id") or headers.get("x-simulation-result-id")
    return str(val) if val else None


# Makes the agent open the call: `semantic_vad` only responds to caller audio, so a
# digital human with `speaks_first: false` deadlocks both sides into silence. Bare, with
# no conversation item, so the greeting comes from the agent's own instructions and no
# phantom user item / `customer.speech` span lands in the trace. A raw message the SDK
# can't validate is dropped with a log line, not an error — test_nudge_greeting.py is the
# check that this one still converts.
NUDGE_GREETING = RealtimeModelSendRawMessage(message={"type": "response.create"})


async def _bridge(ws, model: str, industry: str) -> None:
    industry_dir = industry_path(industry)
    # Must match Realtime tracing.workflow_name pattern ^[A-Za-z0-9_ -]+$
    workflow = f"mivas {Path(industry_dir).name} {model}".replace(".", "-").replace("/", " ")
    sim_id = _simulation_result_id(ws)
    resolved = set_call_id(sim_id)
    log_ws_accept(resolved)
    async with traced_run(workflow, simulation_result_id=sim_id) as tracer:
        ctx: dict = {}
        up = down = None
        utt: str | None = None
        async with await build_from_blueprint(industry_dir, model).run(
            context=ctx,
            model_config=(
                {
                    "initial_model_settings": {
                        "tracing": {
                            "workflow_name": workflow,
                            "group_id": str(sim_id),
                            "metadata": {
                                "bluejay.simulation_result_id": str(sim_id),
                            },
                        }
                    }
                }
                if sim_id
                else None
            ),
        ) as session:
            ctx["session"] = session
            events = tracer.wrap(session) if tracer is not None else session
            await session.model.send_event(NUDGE_GREETING)

            async def inbound() -> None:
                nonlocal up
                try:
                    async for msg in ws:
                        if getattr(session, "_closed", False):
                            break
                        if isinstance(msg, bytes) and msg:
                            pcm, up = audioop.ratecv(msg, W, 1, R_IN, R_OUT, up)
                            if not pcm:
                                continue
                            try:
                                await session.send_audio(pcm)
                            except Exception as e:
                                if type(e).__name__.startswith("ConnectionClosed"):
                                    break
                                raise
                            continue
                        if not isinstance(msg, str):
                            continue
                        try:
                            event = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        data = event.get("data") or {}
                        if tracer is None:
                            continue
                        if etype == "speech.started":
                            uid = data.get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                            tracer.start_customer_speech(uid)
                        elif etype == "speech.completed":
                            tracer.end_customer_speech()
                finally:
                    if tracer is not None:
                        tracer.end_customer_speech()
                    if not getattr(session, "_closed", False):
                        asyncio.create_task(session.close())

            async def outbound() -> None:
                nonlocal down, utt
                try:
                    async for event in events:
                        if event.type == "audio":
                            if utt is None:
                                utt = f"u_{uuid.uuid4().hex[:12]}"
                                await ws.send(
                                    _event("speech.started", {"utterance_id": utt})
                                )
                            pcm, down = audioop.ratecv(
                                event.audio.data, W, 1, R_OUT, R_IN, down
                            )
                            if pcm:
                                await ws.send(pcm)
                        elif event.type in {"audio_end", "audio_interrupted"} and utt:
                            await ws.send(
                                _event("speech.completed", {"utterance_id": utt})
                            )
                            utt = None
                finally:
                    if utt is not None:
                        with contextlib.suppress(Exception):
                            await ws.send(
                                _event("speech.completed", {"utterance_id": utt})
                            )
                        utt = None
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
        raise


def main(model: str | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=model or os.environ.get("OPENAI_REALTIME_MODEL"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    a = p.parse_args()
    if not a.model or not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("need --model/OPENAI_REALTIME_MODEL and OPENAI_API_KEY")
    print(f"ws↔OpenAI {a.model} × {a.industry} :{a.port} auth={bool(_auth())}", flush=True)

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.model, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
