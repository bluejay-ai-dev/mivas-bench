"""optional 16 kHz pcm websocket bridge ↔ OpenAI Realtime (24 kHz)."""

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

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import build_from_blueprint, industry_path  # noqa: E402
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


async def _bridge(ws, model: str, industry: str) -> None:
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{model}"
    sim_id = _simulation_result_id(ws)
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)
    async with traced_run(workflow, simulation_result_id=sim_id):
        ctx: dict = {}
        up = down = None
        utt: str | None = None
        async with await build_from_blueprint(industry_dir, model).run(context=ctx) as session:
            ctx["session"] = session

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
                                # end_call closed the Realtime socket — normal hangup
                                if type(e).__name__.startswith("ConnectionClosed"):
                                    break
                                raise
                finally:
                    if not getattr(session, "_closed", False):
                        asyncio.create_task(session.close())

            async def outbound() -> None:
                nonlocal down, utt
                try:
                    async for event in session:
                        if event.type == "audio":
                            if utt is None:
                                utt = f"u_{uuid.uuid4().hex[:12]}"
                                await ws.send(_event("speech.started", {"utterance_id": utt}))
                            pcm, down = audioop.ratecv(event.audio.data, W, 1, R_OUT, R_IN, down)
                            if pcm:
                                await ws.send(pcm)
                        elif event.type in {"audio_end", "audio_interrupted"} and utt:
                            await ws.send(_event("speech.completed", {"utterance_id": utt}))
                            utt = None
                finally:
                    if utt is not None:
                        with contextlib.suppress(Exception):
                            await ws.send(_event("speech.completed", {"utterance_id": utt}))
                        utt = None
                    with contextlib.suppress(Exception):
                        await ws.close(1000)

            results = await asyncio.gather(inbound(), outbound(), return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException) and not type(r).__name__.startswith(
                    "ConnectionClosed"
                ):
                    raise r


async def _handler(ws, model: str, industry: str) -> None:
    expected = _auth()
    if expected and ws.request.headers.get("Authorization") != expected:
        await ws.close(1008, "unauthorized")
        return
    try:
        await _bridge(ws, model, industry)
    except Exception as e:
        # Hangups look like ConnectionClosed*; everything else is a real fault.
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
