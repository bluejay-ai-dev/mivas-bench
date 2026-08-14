"""optional 16 khz pcm websocket bridge ↔ assemblyai voice agent (24 khz)."""

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
from harness import call_session, WS_URL, industry_path, load_blueprint, run_tool, session_config, set_call_id  # noqa: E402
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
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{model}"
    sim_id = _simulation_result_id(ws)
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)
    set_call_id(sim_id)

    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise SystemExit("need ASSEMBLYAI_API_KEY")

    up = down = None
    utt: str | None = None
    speech_otel = None
    customer_otel = None
    pending: list[dict] = []
    should_end = False
    ready = asyncio.Event()
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

    # call_session freezes this call's DB to S3 on exit; composed here so a
    # raising bridge still snapshots and the body needs no reindent.
    async with traced_run(
        workflow, simulation_result_id=sim_id, model=model
    ), call_session(sim_id):
        async with websockets.connect(f"{WS_URL}?token={key}") as agent_ws:
            await agent_ws.send(
                json.dumps({"type": "session.update", "session": session_config(bp)})
            )

            async def inbound() -> None:
                """chirp 16 khz pcm → assemblyai 24 khz base64 input.audio (only once ready)."""
                nonlocal up, customer_otel
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg and ready.is_set():
                            pcm, up = audioop.ratecv(msg, W, 1, R_CHIRP, R_OUT, up)
                            if pcm:
                                await agent_ws.send(
                                    json.dumps(
                                        {
                                            "type": "input.audio",
                                            "audio": base64.b64encode(pcm).decode(),
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
                        await agent_ws.close()

            async def outbound() -> None:
                """assemblyai reply.audio → chirp 16 khz pcm; drain tool.call on reply.done."""
                nonlocal down, utt, speech_otel, should_end
                try:
                    async for raw in agent_ws:
                        if end.is_set():
                            break
                        event = json.loads(raw)
                        etype = event.get("type")
                        if etype == "session.ready":
                            ready.set()
                        elif etype == "reply.audio":
                            if utt is None:
                                utt = f"u_{uuid.uuid4().hex[:12]}"
                                speech_otel = start_speech_span(utt, speaker="agent")
                                await ws.send(_event("speech.started", {"utterance_id": utt}))
                            pcm, down = audioop.ratecv(
                                base64.b64decode(event["data"]), W, 1, R_OUT, R_CHIRP, down
                            )
                            if pcm:
                                await ws.send(pcm)
                        elif etype == "tool.call":
                            pending.append(event)
                        elif etype == "reply.done":
                            if utt:
                                await ws.send(_event("speech.completed", {"utterance_id": utt}))
                                _close_utt()
                            if pending:
                                calls, pending[:] = list(pending), []
                                for call in calls:
                                    result, stop = await run_tool(
                                        call["name"],
                                        dict(call.get("arguments") or {}),
                                        bp,
                                        state,
                                        call_id=call.get("call_id"),
                                    )
                                    should_end = should_end or stop
                                    await agent_ws.send(
                                        json.dumps(
                                            {
                                                "type": "tool.result",
                                                "call_id": call["call_id"],
                                                "result": json.dumps(result),
                                            }
                                        )
                                    )
                                # let the farewell reply (if any) play before ending
                            elif should_end:
                                with contextlib.suppress(Exception):
                                    await agent_ws.send(json.dumps({"type": "session.end"}))
                        elif etype == "session.ended":
                            end.set()
                            break
                        elif etype == "session.error":
                            print(f"chirp assemblyai error: {event}", flush=True)
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
    p.add_argument("--model", default=model or os.environ.get("ASSEMBLYAI_VOICE_AGENT_MODEL"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    a = p.parse_args()
    if not a.model or not os.environ.get("ASSEMBLYAI_API_KEY"):
        raise SystemExit("need --model/ASSEMBLYAI_VOICE_AGENT_MODEL and ASSEMBLYAI_API_KEY")
    industry_path(a.industry)
    print(f"ws↔AssemblyAI {a.model} × {a.industry} :{a.port} auth={bool(_auth())}", flush=True)

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.model, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
