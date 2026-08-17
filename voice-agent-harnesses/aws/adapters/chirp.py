"""CHIRP (16 kHz pcm_s16le) ↔ Amazon Nova 2 Sonic (16 kHz in / 24 kHz out).

Audio policy matches the OpenAI chirp adapter:
  - Always forward inbound DH PCM. Never gate on agent speech.
  - CHIRP speech.started / speech.completed are OTel only (echo VAD).
  - Provider turn-taking owns barge-in.
  - Speak-first: silent audio keepalive, then interactive USER text until audio.

Nova keeps one USER audio content stream open per prompt. When the caller is
quiet we feed silent PCM (VoiceChat duplex keepalive) so the stream does not
idle out. Multi-agent is a new Bedrock stream per blueprint agent because
tools are fixed at promptStart; the target is seeded with prior ASR.
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
    REGION,
    handle_function_call,
    handoff_role,
    infer_schedule_appointment,
    industry_path,
    load_blueprint,
    log_ws_accept,
    open_session,
    run_tool,
    set_call_id,
)
from report import traced_run  # noqa: E402

W, R_IN, R_OUT = 2, INPUT_RATE, OUTPUT_RATE
NUDGE_RETRY_DELAY_S = float(os.environ.get("MIVAS_NUDGE_RETRY_DELAY_S", "3"))
NUDGE_MAX_ATTEMPTS = int(os.environ.get("MIVAS_NUDGE_MAX_ATTEMPTS", "5"))
# Closing one Bedrock bidirectional stream can cancel CRT futures on other
# live streams in this process. Serialize close+open across calls.
_STREAM_LOCK = asyncio.Lock()


def _merge_user_asr(prev: str, text: str) -> str:
    """Keep the full caller turn when ASR arrives as several short finals."""
    t = (text or "").strip()
    p = (prev or "").strip()
    if not t:
        return p
    if not p:
        return t
    if t.lower() in p.lower():
        return p
    if p.lower() in t.lower():
        return t
    return f"{p} {t}".strip()


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


async def _nudge_until_open(session, opened: asyncio.Event, end: asyncio.Event) -> None:
    for _ in range(NUDGE_MAX_ATTEMPTS):
        if opened.is_set() or end.is_set() or not session.is_active:
            return
        with contextlib.suppress(Exception):
            await session.nudge_speak_first()
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

    holder: dict[str, Any] = {"s": None}
    try:
        async with traced_run(workflow, simulation_result_id=sim_id, model=model) as tracer:
            if tracer is not None:
                state["_otel_root"] = tracer.root
            gen = {"v": 0}
            opened = asyncio.Event()
            handled: set[str] = set()
            inferred_booking = {"v": False}
            last_user_asr = {"v": ""}
            last_agent_text = {"v": ""}
            turn = {"agent": None}
            down = {"state": None}
            pending_user: list[bytes] = []
            agent_out = {"bytes": 0}

            async def start_agent() -> None:
                if turn["agent"] is not None:
                    return
                turn["agent"] = f"u_{uuid.uuid4().hex[:12]}"
                if tracer is not None:
                    tracer.user_message(last_user_asr["v"])  # caller turn this response answers
                    tracer.on_agent_audio()  # open model span + stamp TTFT
                with contextlib.suppress(Exception):
                    await ws.send(_event("speech.started", {"utterance_id": turn["agent"]}))

            async def end_agent(*, transcript: str | None = None, why: str = "") -> None:
                if turn["agent"] is None:
                    return
                text = (transcript or last_agent_text["v"] or "").strip()
                with contextlib.suppress(Exception):
                    await ws.send(_event("speech.completed", {"utterance_id": turn["agent"]}))
                if text:
                    last_agent_text["v"] = text
                _ = why
                turn["agent"] = None

            async def close_after_delay() -> None:
                await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
                end.set()
                s = holder["s"]
                if s is not None:
                    with contextlib.suppress(Exception):
                        await s.close()
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

            async def flush_pending(s) -> None:
                frames = list(pending_user)
                pending_user.clear()
                if s is None or not s.is_active:
                    return
                for pcm in frames:
                    with contextlib.suppress(Exception):
                        await s.send_pcm(pcm)

            async def switch_agent(role: str) -> None:
                old = holder["s"]
                holder["s"] = None
                gen["v"] += 1
                seed_preview = (last_user_asr["v"] or "")[:80]
                print(
                    f"handoff new stream → {role} gen={gen['v']} seed={seed_preview!r}",
                    flush=True,
                )
                async with _STREAM_LOCK:
                    if old is not None:
                        with contextlib.suppress(Exception):
                            await old.close()
                        await asyncio.sleep(0.2)
                    new = await open_session(role, bp, model=model, generation=gen["v"])
                holder["s"] = new
                opened.clear()
                await flush_pending(new)
                await new.seed_handoff(
                    user_said=last_user_asr["v"],
                    prior_agent_said=last_agent_text["v"],
                )
                asyncio.create_task(_nudge_until_open(new, opened, end))

            async def dispatch_tool(name: str, arguments: dict, call_id: str) -> None:
                if not name or call_id in handled:
                    return
                handled.add(call_id)
                if tracer is not None:
                    state["_otel_root"] = tracer.current_turn()  # nest execute_tool under the turn
                await end_agent()
                result, stop = await handle_function_call(
                    name, arguments, call_id, bp, state
                )
                if name == "schedule_appointment":
                    inferred_booking["v"] = True
                print(f"tool {name} -> {result.get('success')} agent={state['agent']}", flush=True)
                role = handoff_role(result, bp)
                s = holder["s"]
                if role:
                    await switch_agent(role)
                    return
                if s is not None and s.is_active:
                    with contextlib.suppress(Exception):
                        await s.send_tool_result(call_id, result)
                if stop:
                    asyncio.create_task(close_after_delay())

            async def inbound() -> None:
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        s = holder["s"]
                        if isinstance(msg, bytes) and msg:
                            if s is not None and s.is_active:
                                with contextlib.suppress(Exception):
                                    await s.send_pcm(msg)
                            else:
                                pending_user.append(msg)
                            continue
                        if not isinstance(msg, str):
                            continue
                        try:
                            ev = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        et = ev.get("type")
                        if tracer is None:
                            continue
                        # CHIRP speech.* are the caller-turn boundary (Nova has no
                        # separate speech_started event); they no longer spawn spans.
                        if et == "speech.started":
                            tracer.on_caller_start()
                        elif et == "speech.completed":
                            tracer.on_caller_stop()
                finally:
                    print("chirp inbound end", flush=True)
                    end.set()

            async def outbound() -> None:
                try:
                    while not end.is_set():
                        s = holder["s"]
                        if s is None:
                            await asyncio.sleep(0.05)
                            continue
                        ev = await s.get_event(timeout=0.25)
                        if end.is_set():
                            return
                        if ev is None:
                            if holder["s"] is s and not s.is_active:
                                print("nova stream ended", flush=True)
                                end.set()
                            continue
                        if ev.get("type") == "_timeout":
                            continue
                        if ev.get("generation") != gen["v"]:
                            continue
                        et = ev.get("type")
                        if et == "audio":
                            pcm24 = ev.get("pcm") or b""
                            if not pcm24:
                                continue
                            pcm16, down["state"] = audioop.ratecv(
                                pcm24, W, 1, R_OUT, R_IN, down["state"]
                            )
                            if not pcm16:
                                continue
                            opened.set()
                            await start_agent()
                            agent_out["bytes"] += len(pcm16)
                            with contextlib.suppress(Exception):
                                await ws.send(pcm16)
                        elif et == "text":
                            role = ev.get("role") or ""
                            text = (ev.get("content") or "").strip()
                            if not text:
                                continue
                            if role == "USER":
                                last_user_asr["v"] = _merge_user_asr(last_user_asr["v"], text)
                                print(f"USER_ASR {last_user_asr['v'][:160]}", flush=True)
                            elif role == "ASSISTANT":
                                print(f"nova[{state['agent']}] {text[:160]}", flush=True)
                        elif et == "turn_end":
                            tr = (ev.get("transcript") or "").strip()
                            if tr:
                                last_agent_text["v"] = tr
                                await maybe_infer_booking(tr)
                            if tracer is not None:
                                tracer.set_output(tr)
                            await end_agent(transcript=tr)
                        elif et == "usage":
                            if tracer is not None:
                                tracer.record_usage(ev.get("usage") or {})
                        elif et == "interrupted":
                            if tracer is not None:
                                tracer.interrupted()
                            await end_agent(why="barge")
                            down["state"] = None
                        elif et == "tool_use":
                            await dispatch_tool(
                                ev.get("name") or "",
                                ev.get("arguments") or {},
                                ev.get("id") or _eid(),
                            )
                        elif et == "error":
                            print(f"nova error: {ev.get('error')}", flush=True)
                except Exception as e:
                    print(f"chirp outbound error: {type(e).__name__}: {e}", flush=True)
                    raise
                finally:
                    print(
                        f"chirp outbound end agent_out={agent_out['bytes']}B",
                        flush=True,
                    )
                    await end_agent()
                    end.set()

            inbound_task = asyncio.create_task(inbound())
            try:
                holder["s"] = await open_session(
                    bp["start"], bp, model=model, generation=gen["v"], speak_first=True
                )
            except Exception as e:
                print(f"nova open_session failed: {type(e).__name__}: {e}", flush=True)
                end.set()
                inbound_task.cancel()
                await asyncio.gather(inbound_task, return_exceptions=True)
                return
            s0 = holder["s"]
            await flush_pending(s0)
            outbound_task = asyncio.create_task(outbound())
            nudge_task = asyncio.create_task(
                _nudge_until_open(holder["s"], opened, end)
            )
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
        s = holder.get("s")
        if s is not None:
            with contextlib.suppress(Exception):
                await s.close()


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
    p.add_argument("--model", default=model or os.environ.get("NOVA_SONIC_MODEL", MODEL))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8774")))
    a = p.parse_args()
    industry_path(a.industry)
    print(
        f"ws↔Nova Sonic {a.model} × {a.industry} :{a.port} "
        f"region={REGION} auth={bool(_auth())}",
        flush=True,
    )

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.model, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
