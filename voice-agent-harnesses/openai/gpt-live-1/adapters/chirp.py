"""Bluejay CHIRP ↔ GPT-Live 1. Everything Bluejay-specific lives here.

CHIRP (Bluejay's websocket agent transport):
  * upgrade headers: ``Authorization: Basic CHIRP_USER:CHIRP_PASS``,
    ``X-Simulation-Result-Id`` (the call's Bluejay id)
  * binary frames: caller PCM16 mono 16 kHz, both directions
  * JSON frames: ``speech.started`` / ``speech.completed`` utterance markers.
    Inbound ones come from Bluejay's VAD and are logged only. Outbound ones
    bracket the agent's audible speech so Bluejay can time it.

The session is opened at 16 kHz PCM — a supported format, and the one CHIRP
carries — so audio is passed through byte-for-byte: no resampling, no pacing,
no gating. The model is full duplex and owns barge-in.
The provider streams a continuous realtime signal (100 ms deltas, silence
included). A 200 ms playout buffer was measured against Bluejay's
agent_audio_dropouts during the GPT-Live preview and moved it not at all, so
there is none here; per-call stream gaps are logged instead
(see _AgentSpeech.stream_report) so the finding can be re-checked per run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live import LiveSession, MODEL, _peak, AUDIBLE_PEAK  # noqa: E402
from pack import load_pack  # noqa: E402
from tools import call_session, run_tool, set_call_id  # noqa: E402
from tracing import Tracer, post_trace_ids, provider  # noqa: E402

log = logging.getLogger("mivas.gpt-live.chirp")

# Agent utterance marker closes after this much inaudible output.
UTTERANCE_QUIET_S = float(os.environ.get("GPT_LIVE_CHIRP_UTTERANCE_QUIET_S", "0.6"))


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _marker(kind: str, utterance_id: str) -> str:
    return json.dumps(
        {"type": kind, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000),
         "data": {"utterance_id": utterance_id}},
        separators=(",", ":"),
    )


def _simulation_result_id(ws) -> str | None:
    headers = getattr(getattr(ws, "request", None), "headers", None)
    val = headers.get("X-Simulation-Result-Id") if headers is not None else None
    return str(val).strip() if val else None


class _AgentSpeech:
    """Outbound agent audio: pass-through + speech.started/completed markers."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.utt: str | None = None
        self.last_audible = 0.0
        self.bytes_out = 0
        # Provider stream shape, for telling jitter (mid-speech) from pauses (between turns).
        self.in_bytes = 0
        self.first_in = 0.0
        self.last_in = 0.0
        self.gaps: list[tuple[int, bool]] = []  # (gap_ms, was_speaking)

    async def audio(self, pcm: bytes) -> None:
        now = time.monotonic()
        if self.last_in:
            gap = int((now - self.last_in) * 1000)
            if gap > 250:
                self.gaps.append((gap, self.utt is not None))
        else:
            self.first_in = now
        self.last_in = now
        self.in_bytes += len(pcm)
        await self._emit(pcm)

    def stream_report(self) -> str:
        span_ms = int((self.last_in - self.first_in) * 1000) if self.first_in else 0
        mid = [g for g, speaking in self.gaps if speaking]
        return (
            f"provider_audio_ms={self.in_bytes // 32} span_ms={span_ms} "
            f"gaps>250ms={len(self.gaps)} mid_speech={len(mid)} max_gap_ms={max((g for g, _ in self.gaps), default=0)}"
        )

    async def _emit(self, frame: bytes) -> None:
        self.bytes_out += len(frame)
        if _peak(frame) >= AUDIBLE_PEAK:
            self.last_audible = time.monotonic()
            if self.utt is None:
                self.utt = f"u_{uuid.uuid4().hex[:12]}"
                await self.ws.send(_marker("speech.started", self.utt))
        await self.ws.send(frame)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            if self.utt and time.monotonic() - self.last_audible >= UTTERANCE_QUIET_S:
                await self.end()

    async def end(self) -> None:
        if self.utt:
            utt, self.utt = self.utt, None
            with contextlib.suppress(Exception):
                await self.ws.send(_marker("speech.completed", utt))


async def _bridge(ws, industry: str) -> None:
    sim_id = _simulation_result_id(ws)
    set_call_id(sim_id)
    pack = load_pack(industry)
    tracer = Tracer(sim_id, model=MODEL, backend_model=os.environ.get("GPT_LIVE_BACKEND_MODEL", "gpt-5.6-terra")) if provider() else None
    speech = _AgentSpeech(ws)
    log.info("chirp accept sim=%s industry=%s", sim_id or "-", pack.industry)

    if tracer:
        tracer.open()
    live = LiveSession(
        pack,
        api_key=os.environ["OPENAI_API_KEY"],
        on_audio=speech.audio,
        run_tool=run_tool,
        observer=tracer,
    )
    bytes_in = 0
    try:
        async with call_session(sim_id):
            await live.open()
            await live.speak_first()
            watcher = asyncio.create_task(speech.watch())

            async def inbound() -> None:
                nonlocal bytes_in
                async for msg in ws:
                    if isinstance(msg, bytes):
                        bytes_in += len(msg)
                        await live.send_audio(msg)
                    else:
                        with contextlib.suppress(ValueError):
                            log.debug("chirp %s", json.loads(msg).get("type"))
                # Bluejay hung up.
                await live.hang_up("caller_hung_up")

            in_task = asyncio.create_task(inbound())
            await live.wait_closed()
            await speech.end()
            watcher.cancel()
            in_task.cancel()
            await asyncio.gather(watcher, in_task, return_exceptions=True)
    finally:
        await live.aclose()
        with contextlib.suppress(Exception):
            await ws.close(1000)
        log.info(
            "call done sim=%s session=%s reason=%s in=%dB out=%dB live_seconds=%s %s",
            sim_id, live.session_id, live.close_reason, bytes_in, speech.bytes_out,
            live.usage_seconds, speech.stream_report(),
        )
        if tracer:
            tracer.close()
            if sim_id and tracer.trace_id:
                await post_trace_ids(sim_id, tracer.trace_id)


async def _handler(ws, industry: str) -> None:
    expected = _auth()
    if expected and ws.request.headers.get("Authorization") != expected:
        await ws.close(1008, "unauthorized")
        return
    try:
        await _bridge(ws, industry)
    except Exception:
        log.exception("bridge failed")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8769")))
    p.add_argument("--model", default=MODEL, help="ignored unless it names this runtime's model")
    a = p.parse_args()
    if a.model != MODEL:
        raise SystemExit(f"this runtime speaks {MODEL} only (got --model {a.model})")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY required")
    logging.basicConfig(
        level=os.environ.get("GPT_LIVE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("chirp↔%s × %s :%s auth=%s", MODEL, a.industry, a.port, bool(_auth()))

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.industry), a.host, a.port, max_size=None):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
