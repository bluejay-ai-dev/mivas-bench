"""Twilio ConversationRelay bridge (TwiML + WebSocket) ↔ GPT-4.1.

Named adapters/chirp.py to match the MIVAS family layout. Unlike PCM CHIRP
harnesses, this process speaks the ConversationRelay JSON protocol:

  Twilio SIP/phone call → GET/POST /  (TwiML <ConversationRelay>)
                        → WS /ws      (setup / prompt / interrupt → text / end)

Soft multi-agent: one OpenAI chat session; handoff swaps system + tools while
keeping history. Speak-first is ConversationRelay welcomeGreeting.

Env:
  OPENAI_API_KEY, PUBLIC_URL|HOST, INDUSTRY, TOOL_SERVER_URL
  CHIRP_PORT (default 8773), TWILIO_LLM_MODEL, TWILIO_WELCOME_GREETING
  BLUEJAY_* for OTel
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    END_CALL_CLOSE_DELAY_S,
    MODEL,
    apply_system_for_active_agent,
    api_key,
    booking_confirm_line,
    extract_appointment_date,
    handoff_target,
    load_blueprint,
    maybe_infer_booking,
    openai_tools_for_agent,
    public_base_url,
    run_tool,
    transcript_blob,
    truncate_assistant_on_interrupt,
    twiml_connect,
    welcome_greeting,
    ws_public_url,
)
from report import (  # noqa: E402
    end_speech_span,
    start_speech_span,
    traced_run,
)


def _industry() -> str:
    return os.environ.get("INDUSTRY", "control-industry")


def build_app(industry: str | None = None) -> FastAPI:
    industry_name = industry or _industry()
    bp = load_blueprint(industry_name)
    app = FastAPI(title="mivas-twilio-conversationrelay")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "industry": bp["industry_dir"].name,
            "model": MODEL,
            "start": bp["start"],
            "public_url": public_base_url() or None,
        }

    @app.api_route("/", methods=["GET", "POST"])
    async def twiml(request: Request) -> Response:
        # Prefer PUBLIC_URL/HOST; else derive from the inbound Host (cloudflared).
        try:
            ws = ws_public_url("/ws")
        except SystemExit:
            host = request.headers.get("host") or request.url.netloc
            if not host:
                return PlainTextResponse("need PUBLIC_URL/HOST or Host header", status_code=500)
            scheme = "wss" if request.url.scheme in ("https", "wss") or "trycloudflare.com" in host else "ws"
            # Cloudflare terminates TLS; local uvicorn sees http — force wss for public hosts.
            if "trycloudflare.com" in host or request.headers.get("x-forwarded-proto") == "https":
                scheme = "wss"
            ws = f"{scheme}://{host}/ws"

        # Bluejay SIP → Twilio SIP Domain forwards X-* SIP headers as SipHeader_*.
        sim_id = ""
        try:
            form = await request.form()
            form_map = {str(k): str(v) for k, v in form.items()}
        except Exception:
            form_map = {}
        for key in (
            "SipHeader_X-Simulation-Result-Id",
            "SipHeader_X-Simulation-Result-ID",
            "SipHeader_X-Simulation-Result-id",
        ):
            if form_map.get(key):
                sim_id = form_map[key].strip()
                break
        if not sim_id:
            sim_id = (request.query_params.get("simulation_result_id") or "").strip()
        params: dict[str, str] = {}
        if sim_id:
            sep = "&" if "?" in ws else "?"
            ws = f"{ws}{sep}simulation_result_id={sim_id}"
            params["simulation_result_id"] = sim_id

        body = twiml_connect(ws_url=ws, parameters=params or None)
        print(
            f"TwiML → ConversationRelay url={ws} sim={sim_id or '-'} "
            f"welcome={welcome_greeting()!r}",
            flush=True,
        )
        return Response(content=body, media_type="application/xml")

    @app.websocket("/ws")
    async def conversation_relay(ws: WebSocket) -> None:
        await ws.accept()
        t0 = time.monotonic()

        def log(msg: str) -> None:
            print(f"t+{int((time.monotonic() - t0) * 1000)}ms {msg}", flush=True)

        sim_id = _sim_id_from_ws(ws)
        call_sid: str | None = None
        state: dict[str, Any] = {
            "agent": bp["start"],
            "mid_call": False,
            "scheduled": False,
            "ending": False,
            "confirm_spoken": False,
        }
        messages: list[dict[str, Any]] = []
        apply_system_for_active_agent(messages, bp, state)
        prompt_buf: list[str] = []

        workflow = f"mivas twilio {bp['industry_dir'].name} {MODEL}".replace(".", "-")
        log(f"WS open sim={sim_id} industry={bp['industry_dir'].name}")

        async with traced_run(workflow, simulation_result_id=sim_id, model=MODEL) as root:
            state["_otel_root"] = root
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log(f"bad json: {raw[:120]!r}")
                        continue
                    mtype = msg.get("type")
                    if mtype == "setup":
                        call_sid = msg.get("callSid")
                        custom = msg.get("customParameters") or {}
                        if not sim_id:
                            sim_id = (
                                str(custom.get("simulation_result_id") or "").strip()
                                or None
                            )
                        log(f"setup callSid={call_sid} sim={sim_id} from={msg.get('from')}")
                    elif mtype == "prompt":
                        text = (msg.get("voicePrompt") or "").strip()
                        last = bool(msg.get("last", True))
                        if text:
                            prompt_buf.append(text)
                        if not last:
                            continue
                        user_text = " ".join(prompt_buf).strip()
                        prompt_buf.clear()
                        if not user_text:
                            continue
                        await _handle_prompt(ws, bp, state, messages, user_text, log=log)
                    elif mtype == "interrupt":
                        spoken = msg.get("utteranceUntilInterrupt") or ""
                        log(f"interrupt spoken={spoken!r}")
                        truncate_assistant_on_interrupt(messages, spoken)
                    elif mtype == "error":
                        log(f"CR error: {msg.get('description')}")
                    elif mtype == "dtmf":
                        log(f"dtmf digit={msg.get('digit')}")
                    else:
                        log(f"ignore type={mtype}")
            except WebSocketDisconnect:
                log(f"WS closed callSid={call_sid}")
            except Exception as e:  # noqa: BLE001
                log(f"WS error: {type(e).__name__}: {e}")
                raise

    return app


def _sim_id_from_ws(ws: WebSocket) -> str | None:
    q = parse_qs(ws.scope.get("query_string", b"").decode())
    for key in ("simulation_result_id", "sim_id"):
        vals = q.get(key) or []
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    headers = dict(ws.headers) if hasattr(ws, "headers") else {}
    for key in ("x-simulation-result-id", "X-Simulation-Result-Id"):
        val = headers.get(key) or headers.get(key.lower())
        if val:
            return str(val).strip()
    return None


async def _send_text(ws: WebSocket, token: str, *, last: bool) -> None:
    await ws.send_text(
        json.dumps({"type": "text", "token": token, "last": last}, separators=(",", ":"))
    )


async def _speak(ws: WebSocket, text: str, state: dict[str, Any], *, log) -> None:
    text = (text or "").strip()
    if not text:
        return
    agent_span = start_speech_span(
        str(uuid.uuid4()), speaker="agent", parent=state.get("_otel_root")
    )
    if agent_span is not None:
        agent_span.set_attribute("mivas.transcript", text[:4000])
    await _send_text(ws, text, last=False)
    await _send_text(ws, "", last=True)
    end_speech_span(agent_span)
    log(f"AGENT_TTS {text!r}")


async def _send_end(ws: WebSocket, reason: str = "done") -> None:
    await ws.send_text(
        json.dumps({"type": "end", "handoffData": reason}, separators=(",", ":"))
    )


async def _ensure_booking_from_history(
    bp: dict[str, Any], state: dict[str, Any], messages: list[dict[str, Any]], *, log
) -> None:
    if state.get("scheduled") or state.get("agent") != "scheduler":
        return
    date = extract_appointment_date(transcript_blob(messages))
    if not date:
        return
    log(f"auto schedule_appointment from history date={date}")
    await run_tool("schedule_appointment", {"date": date}, bp, state, call_id="history")


async def _handle_prompt(
    ws: WebSocket,
    bp: dict[str, Any],
    state: dict[str, Any],
    messages: list[dict[str, Any]],
    user_text: str,
    *,
    log,
) -> None:
    if state.get("ending"):
        return

    utt = str(uuid.uuid4())
    cust = start_speech_span(utt, speaker="customer", parent=state.get("_otel_root"))
    if cust is not None:
        cust.set_attribute("mivas.transcript", user_text[:4000])
    end_speech_span(cust)
    log(f"USER_ASR {user_text!r} agent={state['agent']}")

    messages.append({"role": "user", "content": user_text})
    apply_system_for_active_agent(messages, bp, state)

    # If scheduler already has a date preference in history, nudge it once.
    if (
        state.get("agent") == "scheduler"
        and state.get("mid_call")
        and not state.get("date_nudge")
    ):
        pref = extract_appointment_date(transcript_blob(messages))
        if pref:
            state["date_nudge"] = pref
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"The caller already preferred {pref}. Confirm that date briefly, "
                        f"call schedule_appointment with date={pref}, speak the booking "
                        f"confirmation, then end_call. Do not re-ask when they want to schedule."
                    ),
                }
            )

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key())
    stop = False

    # Non-streaming: avoid speaking handoff/tool filler mid-turn.
    for _ in range(8):
        tools = openai_tools_for_agent(bp, state["agent"])
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "temperature": 0.3,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = await client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        assistant_text = (msg.content or "").strip()
        tool_calls = list(msg.tool_calls or [])

        has_handoff = any(
            handoff_target(bp, state["agent"], tc.function.name)
            for tc in tool_calls
            if tc.function and tc.function.name
        )

        # Speak only when not a silent handoff turn (pack says don't announce handoff).
        if assistant_text and not has_handoff:
            await _speak(ws, assistant_text, state, log=log)
            if state.get("scheduled") and "scheduled" in assistant_text.lower():
                state["confirm_spoken"] = True

        if tool_calls:
            openai_tool_calls = []
            for tc in tool_calls:
                openai_tool_calls.append(
                    {
                        "id": tc.id or f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": tc.function.name if tc.function else "",
                            "arguments": (tc.function.arguments if tc.function else None)
                            or "{}",
                        },
                    }
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text or None,
                    "tool_calls": openai_tool_calls,
                }
            )
            for tc in openai_tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                log(f"TOOL {name} args={args}")
                result, should_stop = await run_tool(
                    name, args, bp, state, call_id=tc["id"]
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result),
                    }
                )
                log(f"TOOL_RESULT {name} → {result} agent={state['agent']}")

                # After a successful booking, always speak a clear confirm before hangup.
                if (
                    name == "schedule_appointment"
                    and isinstance(result, dict)
                    and result.get("success")
                    and not state.get("confirm_spoken")
                ):
                    date = str(
                        state.get("scheduled_date")
                        or result.get("date")
                        or args.get("date")
                        or ""
                    )
                    if date:
                        line = booking_confirm_line(date)
                        await _speak(ws, line, state, log=log)
                        messages.append({"role": "assistant", "content": line})
                        state["confirm_spoken"] = True

                if should_stop:
                    stop = True
                apply_system_for_active_agent(messages, bp, state)
            if stop:
                break
            continue

        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
            await maybe_infer_booking(assistant_text, bp, state)
            if state.get("scheduled") and not state.get("confirm_spoken"):
                date = str(state.get("scheduled_date") or "")
                if date:
                    line = booking_confirm_line(date)
                    await _speak(ws, line, state, log=log)
                    messages.append({"role": "assistant", "content": line})
                    state["confirm_spoken"] = True
        break

    if stop and not state.get("ending"):
        await _ensure_booking_from_history(bp, state, messages, log=log)
        if state.get("agent") == "scheduler" and not state.get("scheduled"):
            log("blocked end_call — still unscheduled; continuing")
            return
        if state.get("scheduled") and not state.get("confirm_spoken"):
            date = str(state.get("scheduled_date") or "")
            if date:
                line = booking_confirm_line(date)
                await _speak(ws, line, state, log=log)
                state["confirm_spoken"] = True
        state["ending"] = True
        await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
        await _send_end(ws, reason="end_call")
        log("sent end")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Twilio ConversationRelay ↔ GPT-4.1")
    parser.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    parser.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CHIRP_PORT", "8773")),
    )
    args = parser.parse_args(argv)
    os.environ.setdefault("INDUSTRY", args.industry)
    api_key()
    app = build_app(args.industry)
    public = public_base_url()
    print(
        f"Twilio ConversationRelay server\n"
        f"  http://{args.host}:{args.port}/\n"
        f"  ws://{args.host}:{args.port}/ws\n"
        f"  industry={args.industry} model={MODEL}\n"
        f"  public={public or '(set PUBLIC_URL/HOST for TwiML)'}\n"
        f"  welcome={welcome_greeting()!r}",
        flush=True,
    )
    if public:
        print(f"  ConversationRelay url={ws_public_url('/ws')}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
