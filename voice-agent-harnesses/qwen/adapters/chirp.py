"""CHIRP (16 kHz pcm_s16le) ↔ Qwen-Audio Realtime (16 kHz in / 24 kHz out).

Audio policy matches the OpenAI chirp adapter:
  - Always forward inbound DH PCM. Never gate on agent speech.
  - CHIRP speech.started / speech.completed are OTel only (echo VAD).
  - Provider server_vad owns barge-in.
  - Speak-first: seed a user text item, then re-nudge response.create until audio.

Soft multi-agent: one Qwen-Audio WebSocket per call; handoff is session.update
on that socket (history stays). Voice is only honored on the first update.

Docs: https://help.aliyun.com/en/model-studio/qwen-audio-realtime-user-guides
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
from typing import Any

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    END_CALL_CLOSE_DELAY_S,
    INPUT_RATE,
    MODEL,
    OUTPUT_RATE,
    configure_session,
    connect_qwen,
    handle_function_call,
    handoff_nudge_event,
    handoff_role,
    infer_schedule_appointment,
    industry_path,
    load_blueprint,
    log_ws_accept,
    nudge_greeting,
    run_tool,
    speak_first_seed,
    session_update_for_agent,
    set_call_id,
    ws_url,
)
from report import traced_run  # noqa: E402

W, R_IN, R_OUT = 2, INPUT_RATE, OUTPUT_RATE
NUDGE_RETRY_DELAY_S = float(os.environ.get("MIVAS_NUDGE_RETRY_DELAY_S", "3"))
NUDGE_MAX_ATTEMPTS = int(os.environ.get("MIVAS_NUDGE_MAX_ATTEMPTS", "5"))


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


def _simulation_result_id(ws) -> str | None:
    headers = getattr(getattr(ws, "request", None), "headers", None)
    if headers is None:
        return None
    val = headers.get("X-Simulation-Result-Id") or headers.get("x-simulation-result-id")
    return str(val).strip() if val else None


def _eid() -> str:
    return str(uuid.uuid4())


async def _open_session(agent: str, bp: dict[str, Any], model: str) -> tuple[Any, Any]:
    cm = connect_qwen(model)
    qwen = await cm.__aenter__()
    try:
        await asyncio.wait_for(qwen.recv(), timeout=30)
        await configure_session(qwen, agent, bp, timeout=30)
    except Exception:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)
        raise
    return qwen, cm


async def _nudge_until_open(qwen, opened: asyncio.Event, end: asyncio.Event) -> None:
    seeded = False
    for _ in range(NUDGE_MAX_ATTEMPTS):
        if opened.is_set() or end.is_set():
            return
        with contextlib.suppress(Exception):
            if not seeded:
                await qwen.send(json.dumps(speak_first_seed()))
                seeded = True
            await qwen.send(json.dumps(nudge_greeting()))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(opened.wait(), timeout=NUDGE_RETRY_DELAY_S)


async def _bridge(ws, model: str, industry: str) -> None:
    bp = load_blueprint(industry)
    state = {"agent": bp["start"]}
    end = asyncio.Event()
    sim_id = _simulation_result_id(ws)
    set_call_id(sim_id)
    log_ws_accept(sim_id)
    workflow = f"mivas {Path(industry_path(industry)).name} {model}".replace(".", "-").replace("/", " ")
    print(f"chirp sim={sim_id} start={bp['start']} agents={list(bp['agents'])}", flush=True)

    qwen_cm = None
    try:
        async with traced_run(workflow, simulation_result_id=sim_id) as tracer:
            if tracer is not None:
                state["_otel_root"] = tracer.root
            qwen, qwen_cm = await _open_session(bp["start"], bp, model)

            opened = asyncio.Event()
            reconfiguring = {"v": False}
            handled: set[str] = set()
            inferred_booking = {"v": False}
            turn = {"agent": None}

            async def start_agent() -> None:
                if turn["agent"] is not None:
                    return
                turn["agent"] = f"u_{uuid.uuid4().hex[:12]}"
                if tracer is not None:
                    tracer.start_agent_speech(turn["agent"])
                await ws.send(_event("speech.started", {"utterance_id": turn["agent"]}))

            async def end_agent(*, transcript: str | None = None) -> None:
                if turn["agent"] is None:
                    return
                with contextlib.suppress(Exception):
                    await ws.send(_event("speech.completed", {"utterance_id": turn["agent"]}))
                if tracer is not None:
                    tracer.end_agent_speech(transcript=transcript)
                turn["agent"] = None

            async def close_after_delay() -> None:
                await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
                end.set()
                with contextlib.suppress(Exception):
                    await qwen.close()
                with contextlib.suppress(Exception):
                    await ws.close(1000)

            async def maybe_infer_booking(transcript: str) -> None:
                if inferred_booking["v"] or "schedule_appointment" not in {
                    t["name"] for t in bp["agents"][state["agent"]]["tools"]
                }:
                    return
                args = infer_schedule_appointment(transcript)
                if not args:
                    return
                inferred_booking["v"] = True
                await run_tool("schedule_appointment", args, bp, state, call_id=f"infer_{_eid()}")
                print(f"inferred schedule_appointment {args}", flush=True)

            async def dispatch_tool(name: str, arguments: str, call_id: str) -> None:
                if not name or call_id in handled:
                    return
                handled.add(call_id)
                await end_agent()
                result, stop, reply = await handle_function_call(
                    name, arguments, call_id, bp, state
                )
                if name == "schedule_appointment":
                    inferred_booking["v"] = True
                with contextlib.suppress(Exception):
                    await qwen.send(json.dumps(reply))
                print(f"tool {name} -> {result.get('success')} agent={state['agent']}", flush=True)
                role = handoff_role(result, bp)
                if role:
                    reconfiguring["v"] = True
                    with contextlib.suppress(Exception):
                        await qwen.send(json.dumps(session_update_for_agent(bp, role, mid_call=True)))
                elif stop:
                    asyncio.create_task(close_after_delay())
                else:
                    with contextlib.suppress(Exception):
                        await qwen.send(json.dumps(nudge_greeting()))

            async def inbound() -> None:
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            # Qwen-Audio input is already 16 kHz pcm — no resample.
                            with contextlib.suppress(Exception):
                                await qwen.send(json.dumps({
                                    "type": "input_audio_buffer.append",
                                    "event_id": _eid(),
                                    "audio": base64.b64encode(msg).decode("ascii"),
                                }))
                            continue
                        if not isinstance(msg, str):
                            continue
                        try:
                            ev = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        et = ev.get("type")
                        data = ev.get("data") or {}
                        if tracer is None:
                            continue
                        if et == "speech.started":
                            tracer.start_customer_speech(
                                data.get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                            )
                        elif et == "speech.completed":
                            tracer.end_customer_speech()
                finally:
                    if tracer is not None:
                        tracer.end_customer_speech()
                    end.set()

            async def outbound() -> None:
                down = None
                try:
                    async for raw in qwen:
                        if end.is_set():
                            break
                        try:
                            ev = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        et = ev.get("type")
                        if et == "response.audio.delta":
                            b = ev.get("delta") or ""
                            if not b:
                                continue
                            opened.set()
                            await start_agent()
                            pcm24 = base64.b64decode(b)
                            pcm16, down = audioop.ratecv(pcm24, W, 1, R_OUT, R_IN, down)
                            if pcm16:
                                await ws.send(pcm16)
                        elif et in ("response.audio.done", "response.done"):
                            await end_agent()
                        elif et == "response.created":
                            opened.set()
                        elif et == "response.audio_transcript.done":
                            tr = (ev.get("transcript") or "").strip()
                            if tr:
                                print(f"qwen[{state['agent']}] {tr[:160]}", flush=True)
                                await maybe_infer_booking(tr)
                        elif et == "conversation.item.input_audio_transcription.completed":
                            tr = (ev.get("transcript") or "").strip()
                            if tr:
                                print(f"USER_ASR {tr[:160]}", flush=True)
                        elif et == "response.function_call_arguments.done":
                            await dispatch_tool(
                                ev.get("name", ""),
                                ev.get("arguments") or "{}",
                                ev.get("call_id") or _eid(),
                            )
                        elif et == "session.updated" and reconfiguring["v"]:
                            reconfiguring["v"] = False
                            with contextlib.suppress(Exception):
                                await qwen.send(json.dumps(handoff_nudge_event()))
                        elif et == "error":
                            print(
                                f"qwen error: {(ev.get('error') or {}).get('message') or ev}",
                                flush=True,
                            )
                finally:
                    await end_agent()
                    end.set()

            outbound_task = asyncio.create_task(outbound())
            inbound_task = asyncio.create_task(inbound())
            nudge_task = asyncio.create_task(_nudge_until_open(qwen, opened, end))
            await asyncio.wait({inbound_task, outbound_task}, return_when=asyncio.FIRST_COMPLETED)
            end.set()
            opened.set()
            nudge_task.cancel()
            for t in (inbound_task, outbound_task, nudge_task):
                t.cancel()
            await asyncio.gather(
                inbound_task, outbound_task, nudge_task, return_exceptions=True
            )
    finally:
        if qwen_cm is not None:
            with contextlib.suppress(Exception):
                await qwen_cm.__aexit__(None, None, None)


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
    p.add_argument("--model", default=model or os.environ.get("QWEN_AUDIO_MODEL", MODEL))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8769")))
    a = p.parse_args()
    industry_path(a.industry)
    print(
        f"ws↔Qwen-Audio {a.model} × {a.industry} :{a.port} "
        f"upstream={ws_url(a.model)} auth={bool(_auth())}",
        flush=True,
    )

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.model, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
