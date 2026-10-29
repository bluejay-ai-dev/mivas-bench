"""Bluejay CHIRP (16 kHz pcm_s16le) ↔ Phonic STS WebSocket. Every Bluejay specific lives here.

- Caller audio goes to Phonic `audio_chunk` byte-for-byte (16 kHz both ways, <=20 ms
  per message; Phonic allows 40), never gated or muted. Phonic VAD owns turn-taking
  and barge-in.
- Agent audio is forwarded to CHIRP as it arrives (Phonic paces it at real time), except the
  all-zero chunks Phonic streams between turns.
- Speak-first is native: the entry agent carries the pack greeting as its welcome message.
- Multi-agent: handoffs are Phonic transfer tools. On `transfer_to_agent` the bridge records
  the handoff span and the new stage; Phonic switches prompt and tools itself. The target
  does not speak on its own, so the bridge starts its turn with `generate_reply` once the
  transfer turn's speech ends (unless the caller is already talking).
- Websocket tools run in their own task (nothing awaited in the provider read loop).
- Hang-up (`end_call`, human-escalation session tools, `assistant_ended_conversation`):
  both sockets close once the agent audio already forwarded has played out.
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
from typing import Any

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "runtime"))  # repo runtime/ when not in-pod
from chirp_health import process_request as _health  # noqa: E402
from harness import (  # noqa: E402
    INTELLIGENCE,
    MODEL,
    URL,
    agent_name,
    config_message,
    connect,
    industry_path,
    load_pack,
    log_ws_accept,
    provision,
    record_transfer,
    run_tool,
    set_call_id,
    stage_of,
    tool_call_output,
)
from report import traced_run  # noqa: E402

W = 2  # bytes per pcm_s16le sample
CHUNK = 640  # 20 ms at 16 kHz


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _chirp_event(t: str, data: dict) -> str:
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


async def _bridge(ws, industry: str) -> None:
    pack = load_pack(industry)
    sim_id = _simulation_result_id(ws)
    set_call_id(sim_id)
    log_ws_accept(sim_id)
    tag = f"[{sim_id}]"
    model = f"{MODEL}:{INTELLIGENCE}"
    workflow = f"mivas {pack.industry} phonic {model}".replace(".", "-")
    t0 = time.monotonic()

    def log(msg: str) -> None:
        print(f"{tag} +{(time.monotonic() - t0) * 1000:.0f}ms {msg}", flush=True)

    async with traced_run(workflow, simulation_result_id=sim_id, model=MODEL) as tracer, connect() as ph:
        if tracer is not None:
            tracer.root.set_attribute("phonic.intelligence_level", INTELLIGENCE)
        await ph.send(json.dumps(config_message(pack)))
        conv_id = None
        while True:  # before the pumps start, so awaiting here blocks nothing
            ev = json.loads(await asyncio.wait_for(ph.recv(), 30))
            et = str(ev.get("type", "")).strip()
            if et == "error":
                raise RuntimeError(f"config rejected: {ev.get('error')} {ev.get('param_errors')}")
            if et == "conversation_created":
                conv_id = ev.get("conversation_id")
            if et == "ready_to_start_conversation":
                break
        stage = {"name": pack.start}
        if tracer is not None:
            tracer.agent = agent_name(pack.start)
        log(f"session ready model={model} project={config_message(pack)['project']} agent={agent_name(pack.start)}")

        end = asyncio.Event()
        play_end = [0.0]  # monotonic time the forwarded agent audio finishes playing
        talk = {"agent": False, "caller": False, "resume": False}
        utt: list[str | None] = [None]
        spoken: list[str] = []
        heard: list[str] = []  # Phonic `input_text` arrives word by word
        first = {"caller": False, "agent": False}

        def note_conversation(cid: Any) -> None:
            nonlocal conv_id
            if cid and cid != conv_id:
                conv_id = cid
            if conv_id and tracer is not None:
                # Phonic's API has no cost field: billed minutes come from GET /v1/conversations/{id}
                tracer.root.set_attribute("phonic.conversation_id", str(conv_id))

        note_conversation(conv_id)

        async def speech_done() -> None:
            if utt[0] is not None:
                with contextlib.suppress(Exception):
                    await ws.send(_chirp_event("speech.completed", {"utterance_id": utt[0]}))
                utt[0] = None

        async def resume_after_transfer() -> None:
            # Measured 2026-10-27: after transfer_to_agent the target agent stays silent until the
            # caller speaks or Phonic's 45 s no-input poke fires, so start its turn once the
            # transfer turn's speech ends (skipped when the caller's own turn will do it).
            if talk["resume"] and not talk["agent"] and not talk["caller"]:
                talk["resume"] = False
                log(f"resume {stage['name']} via generate_reply")
                await ph.send(json.dumps({"type": "generate_reply"}))

        async def hang_up(reason: str) -> None:
            wait = max(0.0, play_end[0] - time.monotonic())
            log(f"hang up ({reason}) after {wait:.2f}s playout")
            await asyncio.sleep(wait)
            end.set()

        async def run_call(ev: dict[str, Any]) -> None:
            name, args = ev.get("tool_name"), ev.get("parameters") or {}
            parent = tracer.current_turn() if tracer is not None else None
            result, hangup = await run_tool(pack, stage["name"], name, args, call_id=ev.get("tool_call_id"), parent=parent)
            log(f"tool {name} {json.dumps(args)[:160]} -> {json.dumps(result)[:160]}")
            await ph.send(json.dumps(tool_call_output(ev["tool_call_id"], result)))
            if hangup:
                await hang_up(name)

        async def guarded(coro) -> None:
            try:
                await coro
            except Exception as e:  # noqa: BLE001
                log(f"tool task failed: {type(e).__name__}: {e}")
                end.set()

        async def inbound() -> None:
            async for msg in ws:
                if isinstance(msg, bytes) and msg:
                    if not first["caller"]:
                        first["caller"] = True
                        log("first caller audio")
                    for i in range(0, len(msg), CHUNK):
                        await ph.send(json.dumps({"type": "audio_chunk", "audio": base64.b64encode(msg[i:i + CHUNK]).decode("ascii")}))

        async def outbound() -> None:
            async for raw in ph:
                ev = json.loads(raw)
                et = str(ev.get("type", "")).strip()
                if tracer is not None:
                    tracer.handle_raw(et, ev)
                if et == "audio_chunk":
                    # Phonic streams real-time audio between turns too (silence), so utterance
                    # boundaries come from assistant_started/finished_speaking, not from chunks.
                    if ev.get("text"):
                        spoken.append(ev["text"])
                    pcm = base64.b64decode(ev.get("audio") or "")
                    # Drop the digital silence Phonic streams between turns: forwarded, it keeps any
                    # receive stall as a permanent playback lag (a 20 s one on 2026-10-27).
                    if not pcm or (not talk["agent"] and not pcm.strip(b"\0")):
                        continue
                    await ws.send(pcm)
                    now = time.monotonic()
                    play_end[0] = max(play_end[0], now) + len(pcm) / (W * 16_000)
                elif et == "assistant_started_speaking":
                    talk["agent"] = True
                    talk["resume"] = False
                    if not first["agent"]:
                        first["agent"] = True
                        log("first agent speech")
                    if utt[0] is None:
                        utt[0] = f"u_{uuid.uuid4().hex[:12]}"
                        await ws.send(_chirp_event("speech.started", {"utterance_id": utt[0]}))
                elif et in ("assistant_finished_speaking", "interrupted_response"):
                    talk["agent"] = False
                    await speech_done()
                    text = (ev.get("text") if et == "interrupted_response" else "".join(spoken)) or ""
                    if text.strip():
                        log(f"agent[{stage['name']}]{' (interrupted)' if et == 'interrupted_response' else ''} {text.strip()[:200]}")
                    spoken.clear()
                    await resume_after_transfer()
                elif et == "input_text":
                    if (ev.get("text") or "").strip():
                        heard.append(ev["text"].strip())
                elif et == "user_started_speaking":
                    talk["caller"] = True
                    talk["resume"] = False
                elif et == "user_finished_speaking":
                    talk["caller"] = False
                    if heard:
                        log(f"caller {' '.join(heard)[:200]}")
                        heard.clear()
                elif et == "tool_call":
                    asyncio.create_task(guarded(run_call(ev)))
                elif et == "transfer_to_agent":
                    target_agent = str(ev.get("agent") or "")
                    entry = next(
                        (e for e in pack.tools[stage["name"]] if e.get("handoff") and agent_name(e["handoff_to"]) == target_agent),
                        None,
                    )
                    name = entry["name"] if entry else f"transfer_to_{stage_of(target_agent, pack)}"
                    parent = tracer.current_turn() if tracer is not None else None
                    target = record_transfer(pack, stage["name"], name, target_agent, parent=parent)
                    log(f"handoff {stage['name']} -> {target} via {name}")
                    stage["name"] = target
                    talk["resume"] = True
                    await resume_after_transfer()
                elif et == "conversation_created":
                    note_conversation(ev.get("conversation_id"))
                elif et == "assistant_ended_conversation":
                    await hang_up("assistant_ended_conversation")
                elif et == "tool_call_interrupted":
                    log(f"tool interrupted {ev.get('tool_name')}")
                elif et == "assistant_chose_not_to_respond":
                    log(f"agent[{stage['name']}] chose not to respond")
                elif et in ("error", "warning"):
                    log(f"provider {et} {json.dumps(ev.get(et))[:300]}")

        tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound()), asyncio.create_task(end.wait())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await speech_done()
        for t in done:
            if not t.cancelled() and t.exception() is not None:
                exc = t.exception()
                if not type(exc).__name__.startswith("ConnectionClosed"):
                    log(f"bridge task failed: {type(exc).__name__}: {exc}")
        close = getattr(ph, "close_code", None)
        log(f"call done conversation={conv_id} final_stage={stage['name']} provider_close={close} {getattr(ph, 'close_reason', '') or ''}".rstrip())
        with contextlib.suppress(Exception):
            await ws.close(1000)


async def _handler(ws, industry: str) -> None:
    expected = _auth()
    if expected and ws.request.headers.get("Authorization") != expected:
        await ws.close(1008, "unauthorized")
        return
    try:
        await _bridge(ws, industry)
    except Exception as e:
        if type(e).__name__.startswith("ConnectionClosed"):
            return
        print(f"chirp bridge error: {type(e).__name__}: {e}", flush=True)
        with contextlib.suppress(Exception):
            await ws.close(1011, "bridge error")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8778")))
    p.add_argument("--no-provision", action="store_true", help="skip upserting the pack's Phonic agents/tools")
    a = p.parse_args()
    industry = str(industry_path(a.industry)) if Path(industry_path(a.industry)).is_dir() else a.industry
    if not a.no_provision:
        print(f"phonic provision {provision(load_pack(industry))}", flush=True)
    print(f"ws↔Phonic {MODEL}:{INTELLIGENCE} × {a.industry} :{a.port} upstream={URL} auth={bool(_auth())}", flush=True)

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, industry), a.host, a.port, process_request=_health):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
