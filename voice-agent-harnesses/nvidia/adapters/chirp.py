"""CHIRP websocket bridge for the NVIDIA Nemotron cascaded pipeline.

Bluejay dials this FastAPI websocket (16 kHz pcm_s16le). Each connection builds
a Pipecat pipeline (Nemotron ASR → Nemotron LLM → Magpie TTS) loaded from the
industry `agent_blueprint.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chirp_serializer import ChirpFrameSerializer  # noqa: E402
from bot import run_bot  # noqa: E402
from harness import (  # noqa: E402
    SAMPLE_RATE,
    industry_path,
    install_io_executor,
    set_call_id,
    warm_magpie,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    install_io_executor()
    await asyncio.to_thread(warm_magpie)
    yield


app = FastAPI(title="mivas nvidia nemotron chirp bridge", lifespan=_lifespan)


def _auth_expected() -> str | None:
    u = os.environ.get("CHIRP_USER", "").strip()
    p = os.environ.get("CHIRP_PASS", "").strip()
    if not u or not p:
        return None
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}"


def _simulation_result_id(ws: WebSocket) -> str | None:
    val = ws.headers.get("x-simulation-result-id") or ws.headers.get(
        "X-Simulation-Result-Id"
    )
    return str(val) if val else None


@app.websocket("/")
async def chirp(ws: WebSocket) -> None:
    expected = _auth_expected()
    if expected and ws.headers.get("authorization") != expected:
        await ws.close(code=1008)
        return

    await ws.accept()
    industry = os.environ.get("INDUSTRY", "control-industry")
    sim_id = _simulation_result_id(ws)
    if sim_id:
        logger.info("chirp sim_result_id={}", sim_id)
    set_call_id(sim_id)

    async def emit(payload: str | bytes) -> None:
        if isinstance(payload, str):
            await ws.send_text(payload)
        else:
            await ws.send_bytes(payload)

    serializer = ChirpFrameSerializer(
        ChirpFrameSerializer.InputParams(sample_rate=SAMPLE_RATE),
        emit=emit,
    )

    transport = FastAPIWebsocketTransport(
        websocket=ws,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    try:
        # Customer speech spans come from Silero VAD → UserStarted/StoppedSpeaking
        # inside bot.SpeechSpanObserver (Bluejay speech.* shares the socket with
        # the transport reader, so we cannot dual-consume them here).
        await run_bot(transport, industry, simulation_result_id=sim_id)
    except WebSocketDisconnect:
        logger.info("chirp client disconnected")
    except Exception as e:
        if type(e).__name__.startswith("ConnectionClosed"):
            return
        logger.exception("chirp bridge error: {}: {}", type(e).__name__, e)
        raise


def main(model: str | None = None) -> None:
    _ = model  # runtime folder is the model id; unused here
    p = argparse.ArgumentParser()
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    p.add_argument("--model", default=os.environ.get("NEMOTRON_LLM_MODEL", ""))
    a = p.parse_args()
    if not os.environ.get("NVIDIA_API_KEY"):
        raise SystemExit("need NVIDIA_API_KEY")
    os.environ["INDUSTRY"] = a.industry
    industry_path(a.industry)
    logger.info(
        "ws↔Nemotron × {} :{} auth={}",
        a.industry,
        a.port,
        bool(_auth_expected()),
    )
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
