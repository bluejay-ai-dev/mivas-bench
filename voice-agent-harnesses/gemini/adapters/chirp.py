"""optional 16 kHz pcm websocket bridge ↔ Gemini Live (24 kHz out)."""

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

import google.genai as genai
from google.genai import errors as genai_errors
from google.genai import types
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import call_session, industry_path, live_config, load_blueprint, run_tool, set_call_id  # noqa: E402
from report import traced_run  # noqa: E402

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
    down = None
    utt: str | None = None
    end = asyncio.Event()
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{model}"
    sim_id = _simulation_result_id(ws)
    # One state-API namespace per call: concurrent digital humans must not share
    # an identity pin or a DB.
    set_call_id(sim_id)
    if sim_id:
        print(f"chirp sim_result_id={sim_id}", flush=True)

    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    resume_handle: str | None = None
    reconnects = 0
    MAX_RECONNECTS = 3

    # call_session freezes this call's DB to S3 on exit; composed here so a
    # raising bridge still snapshots and the body needs no reindent.
    async with traced_run(
        workflow, simulation_result_id=sim_id, model=model
    ) as tr, call_session(sim_id):
      # Live can drop a session mid-call (the native-audio preview closes with
      # 1007 CONTENT_TYPE_AUDIO). The caller's websocket is still open, so
      # reconnect on the resumption handle and keep the call going rather than
      # hanging up on them.
      while not end.is_set():
        async with client.aio.live.connect(
            model=model, config=live_config(bp, resume=resume_handle)
        ) as session:
            # Live waits for a turn before speaking, so the opening needs a nudge.
            # It has to be a client *turn*: send_realtime_input is for audio and
            # video chunks. A resumed session already carries the conversation and
            # needs nothing; a reconnect without a handle lost it, so the model is
            # told where the call had got to instead of greeting again.
            nudge = None
            if reconnects == 0:
                nudge = "[Call connected. Greet the caller now per your instructions.]"
            elif resume_handle is None:
                nudge = (
                    f"[The audio link dropped for a moment and is back. You are "
                    f"mid-call as the {state['agent']} agent. Do not greet or "
                    f"introduce yourself again — ask the caller to repeat their "
                    f"last point and carry on.]"
                )
            if nudge:
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=nudge)]),
                    turn_complete=True,
                )

            async def inbound() -> None:
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=msg, mime_type="audio/pcm;rate=16000"
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
                        # CHIRP speech.* mark caller-turn boundaries → realtime_session turn spans.
                        if tr is not None and etype == "speech.started":
                            tr.start_turn()
                        elif tr is not None and etype == "speech.completed":
                            tr.mark_ref()
                finally:
                    end.set()
                    print(
                        f"chirp inbound end sim={sim_id} "
                        f"close_code={getattr(ws, 'close_code', None)} "
                        f"reason={getattr(ws, 'close_reason', None)!r}",
                        flush=True,
                    )

            async def outbound() -> None:
                nonlocal down, utt, resume_handle
                try:
                    while not end.is_set():
                        async for response in session.receive():
                            if end.is_set():
                                break
                            if tr is not None:
                                tr.bump_event()
                                if getattr(response, "usage_metadata", None):
                                    tr.record_usage(response.usage_metadata)
                            if response.data:
                                if utt is None:
                                    utt = f"u_{uuid.uuid4().hex[:12]}"
                                    if tr is not None:
                                        tr.on_model_audio()
                                    await ws.send(
                                        _event("speech.started", {"utterance_id": utt})
                                    )
                                pcm, down = audioop.ratecv(
                                    response.data, W, 1, R_OUT, R_CHIRP, down
                                )
                                if pcm:
                                    await ws.send(pcm)
                            upd = getattr(response, "session_resumption_update", None)
                            if upd is not None and getattr(upd, "new_handle", None):
                                resume_handle = upd.new_handle
                            sc = response.server_content
                            if sc is not None and tr is not None:
                                it = getattr(sc, "input_transcription", None)
                                if it is not None and getattr(it, "text", None):
                                    tr.user_message(it.text)
                                ot = getattr(sc, "output_transcription", None)
                                if ot is not None and getattr(ot, "text", None):
                                    tr.add_output(ot.text)
                                if getattr(sc, "interrupted", False):
                                    tr.interrupted()
                            if sc is not None and getattr(sc, "turn_complete", False):
                                if utt:
                                    await ws.send(
                                        _event("speech.completed", {"utterance_id": utt})
                                    )
                                    utt = None
                                if tr is not None:
                                    tr.finish_model()
                            if response.tool_call:
                                replies = []
                                should_end = False
                                for fc in response.tool_call.function_calls or []:
                                    args = dict(fc.args or {})
                                    result, stop = await run_tool(
                                        fc.name,
                                        args,
                                        bp,
                                        state,
                                        call_id=getattr(fc, "id", None),
                                    )
                                    should_end = should_end or stop
                                    replies.append(
                                        types.FunctionResponse(
                                            id=fc.id, name=fc.name, response=result
                                        )
                                    )
                                if replies:
                                    await session.send_tool_response(
                                        function_responses=replies
                                    )
                                if should_end:
                                    end.set()
                                    break
                        else:
                            continue
                        break
                finally:
                    utt = None
                    end.set()

            tasks = [
                asyncio.create_task(inbound(), name="inbound"),
                asyncio.create_task(outbound(), name="outbound"),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            # While the caller's socket is still open, a closed Live socket is
            # the Gemini side dropping, not a hangup — resume instead of ending.
            # The same drop surfaces as ConnectionClosed on the sending task and
            # as APIError on the receiving one, so both count.
            caller_gone = getattr(ws, "close_code", None) is not None
            gemini_dropped = False
            fatal: BaseException | None = None
            for t in done:
                exc = None if t.cancelled() else t.exception()
                print(
                    f"chirp call end sim={sim_id} first={t.get_name()} "
                    f"exc={type(exc).__name__ if exc else 'none'}: {exc}",
                    flush=True,
                )
                if exc is None:
                    continue
                if type(exc).__name__.startswith("ConnectionClosed") or isinstance(
                    exc, genai_errors.APIError
                ):
                    gemini_dropped = gemini_dropped or not caller_gone
                else:
                    fatal = fatal or exc
            if fatal is not None and not gemini_dropped:
                raise fatal

            if not gemini_dropped or reconnects >= MAX_RECONNECTS:
                break
            reconnects += 1
            print(
                f"chirp resume sim={sim_id} attempt={reconnects} "
                f"handle={'yes' if resume_handle else 'no'}",
                flush=True,
            )
            end.clear()

      with contextlib.suppress(Exception):
          await ws.close(1000)


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
    p.add_argument("--model", default=model or os.environ.get("GEMINI_LIVE_MODEL"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    a = p.parse_args()
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not a.model or not key:
        raise SystemExit("need --model/GEMINI_LIVE_MODEL and GOOGLE_API_KEY")
    industry_path(a.industry)
    print(f"ws↔Gemini {a.model} × {a.industry} :{a.port} auth={bool(_auth())}", flush=True)

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.model, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
