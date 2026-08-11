"""16 kHz pcm websocket bridge ↔ ElevenLabs Conversational AI (native multi-agent).

Both sides are pcm_16000, so unlike the Gemini/AssemblyAI bridges this one does
no resampling — chirp bytes go straight to `user_audio_chunk`, and `audio_event`
bytes go straight back to chirp.
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

import websockets
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import ensure_agents, get_signed_url, industry_path, load_blueprint, run_tool  # noqa: E402
from report import (  # noqa: E402
    end_speech_span,
    finish_tool_span,
    start_speech_span,
    tool_span,
    traced_run,
)

POLL_TIMEOUT_S = 0.25
SILENCE_GAP_S = 0.9


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
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{model}"
    sim_id = _simulation_result_id(ws)
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)

    ids = ensure_agents(industry_dir)
    signed_url = await get_signed_url(ids["receptionist_id"])

    utt: str | None = None
    speech_otel = None
    customer_otel = None
    end = asyncio.Event()

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
        async with websockets.connect(signed_url) as agent_ws:
            await agent_ws.send(json.dumps({"type": "conversation_initiation_client_data"}))

            async def inbound() -> None:
                """chirp pcm + Bluejay speech.* → ElevenLabs; customer.speech spans."""
                nonlocal customer_otel
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            await agent_ws.send(
                                json.dumps({"user_audio_chunk": base64.b64encode(msg).decode()})
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
                """ElevenLabs audio/tool events → chirp 16 khz pcm + speech.* + tool exec.

                `agent_response_complete`/`interruption` close the utterance when they
                arrive, but ElevenLabs doesn't reliably emit `agent_response_complete`
                promptly (notably for a scripted `first_message` turn), so a silence-gap
                fallback (no `audio` chunk for SILENCE_GAP_S) is the real signal.
                """
                nonlocal utt, speech_otel
                last_audio_ts: float | None = None
                try:
                    while not end.is_set():
                        try:
                            raw = await asyncio.wait_for(agent_ws.recv(), timeout=POLL_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            if utt and last_audio_ts is not None:
                                if time.monotonic() - last_audio_ts > SILENCE_GAP_S:
                                    await ws.send(_event("speech.completed", {"utterance_id": utt}))
                                    _close_utt()
                            continue
                        event = json.loads(raw)
                        etype = event.get("type")
                        if etype == "audio":
                            if utt is None:
                                utt = f"u_{uuid.uuid4().hex[:12]}"
                                speech_otel = start_speech_span(utt, speaker="agent")
                                await ws.send(_event("speech.started", {"utterance_id": utt}))
                            last_audio_ts = time.monotonic()
                            pcm = base64.b64decode(event["audio_event"]["audio_base_64"])
                            if pcm:
                                await ws.send(pcm)
                        elif etype in ("agent_response_complete", "interruption"):
                            if utt:
                                await ws.send(_event("speech.completed", {"utterance_id": utt}))
                                _close_utt()
                        elif etype == "ping":
                            ev = event.get("ping_event", {})
                            await agent_ws.send(
                                json.dumps({"type": "pong", "event_id": ev.get("event_id")})
                            )
                        elif etype == "client_tool_call":
                            call = event.get("client_tool_call", {})
                            tool_name = call.get("tool_name")
                            result = await run_tool(
                                tool_name,
                                dict(call.get("parameters") or {}),
                                call_id=call.get("tool_call_id"),
                            )
                            is_error = not bool(result.get("success", True))
                            print(
                                f"chirp tool {tool_name} error={is_error}",
                                flush=True,
                            )
                            await agent_ws.send(
                                json.dumps(
                                    {
                                        "type": "client_tool_result",
                                        "tool_call_id": call.get("tool_call_id"),
                                        "result": json.dumps(result),
                                        "is_error": is_error,
                                    }
                                )
                            )
                        elif etype == "agent_tool_response":
                            # System tools (transfer_to_agent / end_call) run server-side
                            # and never hit client_tool_call — this is the only event that
                            # exposes them, so emit an execute_tool span for Bluejay.
                            # Payload is identity + outcome only (tool_name, tool_call_id,
                            # tool_type, is_error, is_called, is_blocked) — no arguments.
                            # Client tools are skipped: run_tool already spans those.
                            payload = event.get("agent_tool_response") or {}
                            tool_name = payload.get("tool_name")
                            print(f"chirp elevenlabs agent_tool_response {payload}", flush=True)
                            fired = payload.get("is_called", True) and not payload.get("is_blocked")
                            if fired and tool_name in ("transfer_to_agent", "end_call"):
                                ok = not payload.get("is_error")
                                params = {"tool_type": payload.get("tool_type", "system")}
                                with tool_span(
                                    tool_name, params, call_id=payload.get("tool_call_id")
                                ) as span:
                                    finish_tool_span(
                                        span,
                                        {"success": ok, "source": "elevenlabs_system_tool"},
                                        ok=ok,
                                        name=tool_name,
                                        parameters=params,
                                    )
                        elif etype in ("client_error", "guardrail_triggered"):
                            print(f"chirp elevenlabs {etype}: {event}", flush=True)
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
    p.add_argument("--model", default=model or os.environ.get("ELEVENLABS_CONVAI_MODEL", "elevenlabs-convai"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    a = p.parse_args()
    if not os.environ.get("ELEVENLABS_API_KEY"):
        raise SystemExit("need ELEVENLABS_API_KEY")
    industry_path(a.industry)
    print(f"ws↔ElevenLabs {a.model} × {a.industry} :{a.port} auth={bool(_auth())}", flush=True)

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.model, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
