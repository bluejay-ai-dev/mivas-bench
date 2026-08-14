"""16 kHz pcm CHIRP bridge ↔ Cartesia Line agent stream, plus the tool webhook.

Cartesia's stream API is all JSON text frames with base64 audio inside, and both
directions are pcm_16000 (verified: media_output arrives paced at ~32 kB/s), so
no resampling. Unlike the audio-only providers it also streams turn structure —
`turn_started` / `turn_ended` with role and final text — so `agent.speech` spans
are bracketed by real turn events instead of a silence-gap heuristic.

The Line agent runs on Cartesia's infrastructure, so its `schedule_appointment`
tool reaches the industry state by POSTing back to `/tool/{name}` here. That is
also where the `execute_tool` span comes from, which is why the websocket and
the webhook share one uvicorn port: one cloudflared tunnel covers both.
Line-native tools (handoff, end_call) never leave Cartesia, so those spans are
reconstructed from `turn_ended.tool_calls`.
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

import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    begin_session,
    bind_provider,
    end_session,
    ensure_agent,
    for_provider,
    industry_name,
    load_blueprint,
    provider_id_from_request,
    run_tool,
)
from report import (  # noqa: E402
    end_speech_span,
    finish_tool_span,
    start_speech_span,
    tool_span,
    traced_run,
)

STREAM_URL = "wss://api.cartesia.ai/agents/stream/{agent_id}?cartesia_version={version}"
CARTESIA_VERSION = os.environ.get("CARTESIA_VERSION", "2026-03-01")

app = FastAPI()
# ponytail: one active call at a time — the webhook attaches its execute_tool
# span to report.py's module-level root. Benchmark runs are max_concurrent=1.
STATE: dict[str, Any] = {}


def _start_config() -> dict[str, str]:
    # Line's own voice. `output_format` is accepted but ignored (output is always
    # pcm_16000, verified by paced byte rate), so there is nothing else to set.
    cfg = {"input_format": "pcm_16000"}
    if os.environ.get("CARTESIA_VOICE_ID"):
        cfg["voice_id"] = os.environ["CARTESIA_VOICE_ID"]
    return cfg


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


def _authorized(ws: WebSocket) -> bool:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    if not (u and p):
        return True
    expected = base64.b64encode(f"{u}:{p}".encode()).decode()
    return ws.headers.get("authorization") == f"Basic {expected}"


@app.post("/tool/{name}")
async def tool_webhook(name: str, request: Request) -> dict[str, Any]:
    """Line's http_server_tool calls this. SQLite only — the execute_tool span is
    emitted on the CHIRP WebSocket from turn_ended.tool_calls so it stays on the
    voice.call tree even when ALB sends this POST to a sibling replica."""
    args = await request.json() if await request.body() else {}
    for_provider(provider_id_from_request(args, query=request.query_params, headers=request.headers))
    result = await run_tool(name, dict(args), emit_span=False)
    print(f"chirp tool {name} args={args} -> {result}", flush=True)
    return result


@app.websocket("/")
async def chirp(ws: WebSocket) -> None:
    if not _authorized(ws):
        await ws.close(1008, "unauthorized")
        return
    await ws.accept()
    try:
        await _bridge(ws)
    except Exception as e:
        if not type(e).__name__.startswith(("ConnectionClosed", "WebSocketDisconnect")):
            print(f"chirp bridge error: {type(e).__name__}: {e}", flush=True)
        with contextlib.suppress(Exception):
            await ws.close(1011)


async def _bridge(ws: WebSocket) -> None:
    industry, model = STATE["industry"], STATE["model"]
    workflow = f"mivas-{industry_name(industry)}-{model}"
    sim_id = ws.headers.get("x-simulation-result-id")
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)

    session_key = uuid.uuid4().hex
    resolved = begin_session(sim_id, session_key=session_key)

    url = STREAM_URL.format(agent_id=STATE["agent_id"], version=CARTESIA_VERSION)
    end = asyncio.Event()
    agent_span = None
    customer_span = None
    utt: str | None = None
    audio = {"in": 0, "out": 0}
    seen_tools: set[str] = set()

    try:
        async with traced_run(workflow, simulation_result_id=sim_id, model=model):
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {os.environ['CARTESIA_API_KEY']}"}
            ) as agent_ws:
                await agent_ws.send(
                    json.dumps(
                        {
                            "event": "start",
                            "stream_id": resolved,
                            "config": _start_config(),
                        }
                    )
                )

                async def inbound() -> None:
                    """Bluejay pcm → media_input; Bluejay speech.* → customer.speech."""
                    nonlocal customer_span
                    try:
                        while not end.is_set():
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                break
                            if (pcm := msg.get("bytes")):
                                audio["in"] += len(pcm)
                                await agent_ws.send(
                                    json.dumps(
                                        {
                                            "event": "media_input",
                                            "media": {"payload": base64.b64encode(pcm).decode()},
                                        }
                                    )
                                )
                                continue
                            if not msg.get("text"):
                                continue
                            try:
                                event = json.loads(msg["text"])
                            except json.JSONDecodeError:
                                continue
                            if event.get("type") == "speech.started":
                                end_speech_span(customer_span)
                                uid = (event.get("data") or {}).get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                                customer_span = start_speech_span(uid, speaker="customer")
                                print(f"chirp customer speech.started {uid}", flush=True)
                            elif event.get("type") == "speech.completed":
                                end_speech_span(customer_span)
                                customer_span = None
                    finally:
                        end_speech_span(customer_span)
                        customer_span = None
                        end.set()
                        with contextlib.suppress(Exception):
                            await agent_ws.close()

                async def outbound() -> None:
                    """Cartesia turn/audio events → chirp pcm + speech.* + tool spans."""
                    nonlocal agent_span, utt
                    try:
                        async for raw in agent_ws:
                            if end.is_set():
                                break
                            event = json.loads(raw)
                            etype = event.get("event")
                            if etype == "media_output":
                                pcm = base64.b64decode(event["media"]["payload"])
                                if pcm:
                                    audio["out"] += len(pcm)
                                    await ws.send_bytes(pcm)
                            elif etype == "turn_started" and event["turn_started"]["role"] == "assistant":
                                utt = f"u_{uuid.uuid4().hex[:12]}"
                                agent_span = start_speech_span(utt, speaker="agent")
                                await ws.send_text(_event("speech.started", {"utterance_id": utt}))
                            elif etype == "turn_ended" and event["turn_ended"]["role"] == "assistant":
                                turn = event["turn_ended"]
                                print(f"chirp agent: {turn.get('text')!r}", flush=True)
                                if agent_span is not None:
                                    agent_span.set_attribute("mivas.speech.text", turn.get("text") or "")
                                end_speech_span(agent_span)
                                agent_span = None
                                if utt:
                                    await ws.send_text(_event("speech.completed", {"utterance_id": utt}))
                                    utt = None
                                _record_line_tools(turn.get("tool_calls") or [], seen_tools)
                            elif etype == "turn_ended":
                                turn = event["turn_ended"]
                                print(f"chirp {turn['role']}: {turn.get('text')!r}", flush=True)
                            elif etype == "clear":
                                # Cartesia barge-in: the caller started talking over the agent.
                                print(f"chirp clear (interrupted={bool(utt)})", flush=True)
                                if utt:
                                    end_speech_span(agent_span)
                                    agent_span = None
                                    await ws.send_text(_event("speech.completed", {"utterance_id": utt}))
                                    utt = None
                            elif etype == "ack":
                                for key in ("call_id", "stream_id"):
                                    cid = event.get(key)
                                    if cid:
                                        bind_provider(str(cid), resolved)
                                print(
                                    f"chirp cartesia call_id={event.get('call_id')} "
                                    f"stream_id={event.get('stream_id')}",
                                    flush=True,
                                )
                            elif etype in ("error", "call_ended", "end_call"):
                                print(f"chirp cartesia {etype}: {raw[:300]}", flush=True)
                                break
                    finally:
                        end_speech_span(agent_span)
                        agent_span = None
                        end.set()
                        with contextlib.suppress(Exception):
                            await ws.close(1000)

                tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                print(f"chirp audio bytes in={audio['in']} out={audio['out']}", flush=True)
                for t in done:
                    exc = None if t.cancelled() else t.exception()
                    if exc is not None and not type(exc).__name__.startswith(
                        ("ConnectionClosed", "WebSocketDisconnect")
                    ):
                        raise exc
    finally:
        end_session(session_key)


def _record_line_tools(calls: list[dict[str, Any]], seen: set[str]) -> None:
    """Every Line tool on this turn, including http_server_tool.

    Handoff/end_call never hit our webhook. schedule_appointment does, but that
    POST lands on a random replica whose process has the wrong (or no)
    voice.call root. Reconstruct the execute_tool span here so Bluejay actuals
    attach to this call's trace. Dedupe on Line's tool-call id — a handoff is
    reported again on the target agent's first turn.
    """
    for call in calls:
        name = call.get("name") or call.get("tool_name")
        if not name:
            continue
        key = str(call.get("id") or name)
        if key in seen:
            continue
        seen.add(key)
        args = call.get("arguments") or call.get("args") or {}
        result = call.get("result")
        if result is None:
            result = {"success": True, "source": "line"}
        ok = True
        if isinstance(result, dict):
            ok = bool(result.get("ok", result.get("success", True)))
        print(f"chirp line tool {name} {args}", flush=True)
        with tool_span(name, args, call_id=call.get("id")) as span:
            finish_tool_span(span, result, ok=ok)


def main(model: str | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=model or os.environ.get("CARTESIA_LINE_MODEL", "line"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8773")))
    a = p.parse_args()
    if not os.environ.get("CARTESIA_API_KEY"):
        raise SystemExit("need CARTESIA_API_KEY")
    if not os.environ.get("PUBLIC_URL"):
        raise SystemExit("need PUBLIC_URL — the Line agent's tools POST to {PUBLIC_URL}/tool/<name>")

    bp = load_blueprint(a.industry)
    STATE.update(
        industry=a.industry,
        model=a.model,
        agent_id=ensure_agent(a.industry),
        native_tools={
            t["name"]
            for agent in bp["agents"].values()
            for t in agent["tools"]
            if t.get("handoff") or t.get("session")
        },
    )
    print(
        f"ws↔Cartesia Line {STATE['agent_id']} × {a.industry} :{a.port} "
        f"tools→{os.environ['PUBLIC_URL']}/tool/",
        flush=True,
    )
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
