"""CHIRP (16 kHz pcm_s16le) ↔ Qwen Omni Realtime (16 kHz in / 24 kHz out).

Soft multi-agent: one Omni WebSocket for the call. Handoff swaps session
instructions + tools via `session.update` on the same socket (history kept).

Speak-first: after session.updated, send `{"type":"response.create"}`.

Audio barge-in (critical):
  Bluejay CHIRP `speech.started` often fires on *agent echo* in the mixed
  recording path. Muting/cancelling on that signal hard-chops agent PCM
  ("Hey," then silence) and starves Omni of the DH's full utterance.
  Correct policy (matches OpenAI chirp + Grok):
    - Always forward inbound DH PCM to Omni (16 kHz, no resample).
    - Always forward agent PCM to Bluejay unless Omni reports
      `input_audio_buffer.speech_started` (real user barge-in), or CHIRP
      VAD fires *and* recent inbound RMS is loud *and* we are outside the
      post-TTS echo-suppress window.
    - Let Omni `server_vad` own turn-taking; do not fight it with CHIRP VAD.

Observability: QWEN_CHIRP_LOG=full|audio|off (default full).
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness import (  # noqa: E402
    END_CALL_CLOSE_DELAY_S,
    INPUT_SAMPLE_RATE,
    MODEL,
    OUTPUT_SAMPLE_RATE,
    configure_session,
    connect_qwen,
    handle_function_call,
    handoff_role,
    industry_path,
    infer_schedule_appointment,
    load_blueprint,
    nudge_greeting,
    run_tool,
    session_update_for_agent,
    tool_names,
    ws_url,
)
from report import end_speech_span, start_speech_span, traced_run  # noqa: E402

W, R_OMNI_IN, R_OMNI_OUT, R_CHIRP = 2, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE, 16_000

ECHO_SUPPRESS_S = float(os.environ.get("QWEN_ECHO_SUPPRESS_S", "0.85"))
USER_RMS_ON = int(os.environ.get("QWEN_USER_RMS_ON", "350"))
USER_LIVE_S = float(os.environ.get("QWEN_USER_LIVE_S", "0.35"))
CHOP_WARN_MS = int(os.environ.get("QWEN_CHOP_WARN_MS", "400"))

_LOG_LEVEL = (os.environ.get("QWEN_CHIRP_LOG") or "full").strip().lower()
_LOG_ON = _LOG_LEVEL not in {"0", "off", "false", "no"}
_LOG_AUDIO = _LOG_LEVEL in {"audio", "pcm", "all"}

_call_t0: float | None = None


def _ms() -> int:
    if _call_t0 is None:
        return 0
    return int((time.monotonic() - _call_t0) * 1000)


def _log(msg: str) -> None:
    if _LOG_ON:
        print(f"t+{_ms()}ms {msg}", flush=True)


def _auth() -> str | None:
    u, p = os.environ.get("CHIRP_USER", "").strip(), os.environ.get("CHIRP_PASS", "").strip()
    return f"Basic {base64.b64encode(f'{u}:{p}'.encode()).decode()}" if u and p else None


def _event(t: str, data: dict) -> str:
    return json.dumps(
        {"type": t, "id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000), "data": data},
        separators=(",", ":"),
    )


def _eid() -> str:
    return str(uuid.uuid4())


def _simulation_result_id(ws) -> str | None:
    headers = getattr(getattr(ws, "request", None), "headers", None)
    if headers is None:
        return None
    val = headers.get("X-Simulation-Result-Id") or headers.get("x-simulation-result-id")
    return str(val).strip() if val else None


class _AudioStats:
    def __init__(self) -> None:
        self.in_bytes = 0
        self.in_frames = 0
        self.in_peak_rms = 0
        self.out_bytes = 0
        self.out_frames = 0
        self.out_peak_rms = 0
        self.out_muted_bytes = 0
        self.out_muted_frames = 0
        self.chops = 0
        self.echo_ignores = 0
        self.barge_ins = 0
        self.chirp_speech_starts = 0
        self.omni_speech_starts = 0
        self._last_summary = 0.0

    def note_in(self, n: int, rms: int) -> None:
        self.in_bytes += n
        self.in_frames += 1
        if rms > self.in_peak_rms:
            self.in_peak_rms = rms

    def note_out(self, n: int, rms: int, *, muted: bool) -> None:
        if muted:
            self.out_muted_bytes += n
            self.out_muted_frames += 1
        else:
            self.out_bytes += n
            self.out_frames += 1
            if rms > self.out_peak_rms:
                self.out_peak_rms = rms

    def maybe_summary(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_summary < 2.0:
            return
        self._last_summary = now
        _log(
            "AUDIO "
            f"in={self.in_bytes}B/{self.in_frames}f peak_rms={self.in_peak_rms} | "
            f"out={self.out_bytes}B/{self.out_frames}f peak_rms={self.out_peak_rms} "
            f"muted={self.out_muted_bytes}B/{self.out_muted_frames}f | "
            f"chops={self.chops} barge={self.barge_ins} echo_ign={self.echo_ignores} "
            f"chirp_vad={self.chirp_speech_starts} omni_vad={self.omni_speech_starts}"
        )


class _Turns:
    def __init__(self, ws, root, stats: _AudioStats) -> None:
        self.ws = ws
        self.root = root
        self.stats = stats
        self.agent_utt: str | None = None
        self.agent_span = None
        self.agent_text: list[str] = []
        self.agent_started_mono: float | None = None
        self.agent_out_bytes = 0
        self.customer_utt: str | None = None
        self.customer_span = None

    async def start_agent(self) -> None:
        if self.agent_utt is not None:
            return
        await self.end_customer()
        self.agent_utt = f"u_{uuid.uuid4().hex[:12]}"
        self.agent_text = []
        self.agent_started_mono = time.monotonic()
        self.agent_out_bytes = 0
        self.agent_span = start_speech_span(
            self.agent_utt, speaker="agent", parent=self.root
        )
        await self.ws.send(_event("speech.started", {"utterance_id": self.agent_utt}))
        _log(f"agent.speech START uid={self.agent_utt}")

    async def end_agent(self, *, why: str = "") -> None:
        if self.agent_utt is None:
            return
        dur_ms = 0
        if self.agent_started_mono is not None:
            dur_ms = int((time.monotonic() - self.agent_started_mono) * 1000)
        text = "".join(self.agent_text).strip()
        if text and self.agent_span is not None:
            with contextlib.suppress(Exception):
                self.agent_span.set_attribute("mivas.transcript", text[:500])
        chop = dur_ms < CHOP_WARN_MS and why.startswith("barge")
        if chop:
            self.stats.chops += 1
        _log(
            f"agent.speech END uid={self.agent_utt} why={why} "
            f"dur_ms={dur_ms} out_bytes={self.agent_out_bytes} "
            f"text={text[:80]!r}{' CHOP' if chop else ''}"
        )
        with contextlib.suppress(Exception):
            await self.ws.send(
                _event("speech.completed", {"utterance_id": self.agent_utt})
            )
        end_speech_span(self.agent_span)
        self.agent_utt = None
        self.agent_span = None
        self.agent_text = []
        self.agent_started_mono = None
        self.agent_out_bytes = 0

    def note_agent_text(self, delta: str) -> None:
        if delta and self.agent_utt is not None:
            self.agent_text.append(delta)

    def note_agent_bytes(self, n: int) -> None:
        self.agent_out_bytes += n

    async def start_customer(self, uid: str, *, why: str = "") -> None:
        if self.customer_utt is not None:
            end_speech_span(self.customer_span)
        self.customer_utt = uid
        self.customer_span = start_speech_span(
            uid, speaker="customer", parent=self.root
        )
        _log(f"customer.speech START uid={uid} why={why}")

    async def end_customer(self, *, why: str = "") -> None:
        if self.customer_utt is None:
            return
        _log(f"customer.speech END uid={self.customer_utt} why={why}")
        end_speech_span(self.customer_span)
        self.customer_utt = None
        self.customer_span = None

    async def close(self) -> None:
        await self.end_agent(why="close")
        await self.end_customer(why="close")


async def _close_all(omni, chirp_ws, end: asyncio.Event) -> None:
    await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
    end.set()
    with contextlib.suppress(Exception):
        await omni.close()
    with contextlib.suppress(Exception):
        await chirp_ws.close(1000)


async def _bridge(ws, industry: str, model: str) -> None:
    global _call_t0
    _call_t0 = time.monotonic()
    stats = _AudioStats()

    bp = load_blueprint(industry)
    state = {"agent": bp["start"]}
    end = asyncio.Event()
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{model}"
    sim_id = _simulation_result_id(ws)
    if sim_id:
        _log(f"chirp sim_result_id={sim_id}")

    _log(
        f"qwen ws={ws_url(model)} start={bp['start']} agents={list(bp['agents'])} "
        f"echo_suppress={ECHO_SUPPRESS_S}s user_rms_on={USER_RMS_ON}"
    )

    cm = connect_qwen(model)
    omni = await cm.__aenter__()
    try:
        async with traced_run(
            workflow, simulation_result_id=sim_id, model=model
        ) as otel_root:
            state["_otel_root"] = otel_root
            raw0 = await asyncio.wait_for(omni.recv(), timeout=60)
            first = json.loads(raw0) if isinstance(raw0, str) else {}
            _log(f"qwen {first.get('type')}")
            updated = await configure_session(omni, bp["start"], bp)
            n = len((updated.get("session") or {}).get("tools") or [])
            _log(f"qwen session.updated agent={bp['start']} tools_registered={n}")

            turns = _Turns(ws, otel_root, stats)
            ctl = {
                "customer_speaking": False,
                "omni_user_speaking": False,
                "forward_agent": True,
                "pending_fn": 0,
                "need_continue": False,
                "audio_done": True,
                "response_active": False,
                "last_agent_loud": 0.0,
                "last_user_loud": 0.0,
                "mute_why": "",
                "last_user_asr": "",
                "last_spoken": "",
            }
            spoken: list[str] = []

            def _agent_echo_risk(now: float | None = None) -> bool:
                now = now if now is not None else time.monotonic()
                return bool(ctl["last_agent_loud"]) and (
                    now - float(ctl["last_agent_loud"]) < ECHO_SUPPRESS_S
                )

            def _user_loud_recent(now: float | None = None) -> bool:
                now = now if now is not None else time.monotonic()
                return bool(ctl["last_user_loud"]) and (
                    now - float(ctl["last_user_loud"]) < USER_LIVE_S
                )

            def _ctl_snap() -> str:
                return (
                    f"active={state['agent']} chirp_vad={ctl['customer_speaking']} "
                    f"omni_vad={ctl['omni_user_speaking']} fwd={ctl['forward_agent']} "
                    f"resp={ctl['response_active']} echo={_agent_echo_risk()} "
                    f"user_loud={_user_loud_recent()} mute_why={ctl['mute_why']!r} "
                    f"agent_utt={turns.agent_utt}"
                )

            async def _send_pcm16_to_omni(pcm16: bytes) -> None:
                if not pcm16 or end.is_set():
                    return
                await omni.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "event_id": _eid(),
                            "audio": base64.b64encode(pcm16).decode("ascii"),
                        }
                    )
                )

            async def _set_forward(forward: bool, *, why: str) -> None:
                if ctl["forward_agent"] == forward and ctl["mute_why"] == (
                    "" if forward else why
                ):
                    return
                prev = ctl["forward_agent"]
                ctl["forward_agent"] = forward
                ctl["mute_why"] = "" if forward else why
                _log(
                    f"FORWARD {'ON' if forward else 'MUTE'} "
                    f"prev={prev} why={why} {_ctl_snap()}"
                )

            async def _cancel_active(*, why: str) -> None:
                if not ctl["response_active"]:
                    _log(f"cancel SKIP (no active response) why={why}")
                    return
                ctl["response_active"] = False
                _log(f"cancel SEND why={why} {_ctl_snap()}")
                with contextlib.suppress(Exception):
                    await omni.send(
                        json.dumps({"type": "response.cancel", "event_id": _eid()})
                    )

            async def _on_real_barge_in(*, why: str) -> None:
                stats.barge_ins += 1
                await _set_forward(False, why=why)
                if turns.agent_utt is not None or ctl["response_active"]:
                    await turns.end_agent(why=f"barge:{why}")
                    await _cancel_active(why=why)

            async def inbound() -> None:
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            # ALWAYS forward DH PCM — Omni input is already 16 kHz.
                            rms = audioop.rms(msg, W) if len(msg) >= W else 0
                            stats.note_in(len(msg), rms)
                            if rms >= USER_RMS_ON:
                                ctl["last_user_loud"] = time.monotonic()
                            await _send_pcm16_to_omni(msg)
                            if _LOG_AUDIO:
                                _log(
                                    f"user→qwen bytes={len(msg)} rms={rms} "
                                    f"active={state['agent']}",
                                )
                            stats.maybe_summary()
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
                            stats.chirp_speech_starts += 1
                            uid = data.get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                            if _agent_echo_risk() and not _user_loud_recent():
                                stats.echo_ignores += 1
                                _log(
                                    f"CHIRP speech.started IGNORED echo uid={uid} "
                                    f"{_ctl_snap()}"
                                )
                                await turns.start_customer(uid, why="chirp_echo_ignored")
                                continue
                            ctl["customer_speaking"] = True
                            await turns.start_customer(uid, why="chirp_speech.started")
                            if _user_loud_recent() and (
                                turns.agent_utt is not None or ctl["response_active"]
                            ):
                                await _on_real_barge_in(why="chirp_vad+user_rms")
                            else:
                                _log(
                                    f"CHIRP speech.started trace-only uid={uid} "
                                    f"{_ctl_snap()}"
                                )
                        elif etype == "speech.completed":
                            ctl["customer_speaking"] = False
                            _log(
                                f"CHIRP speech.completed uid={data.get('utterance_id')} "
                                f"{_ctl_snap()}"
                            )
                            await turns.end_customer(why="chirp_speech.completed")
                            if (
                                not ctl["omni_user_speaking"]
                                and not ctl["forward_agent"]
                                and ctl["mute_why"].startswith("chirp_")
                            ):
                                await _set_forward(True, why="chirp_speech.completed")
                finally:
                    ctl["customer_speaking"] = False
                    await turns.end_customer(why="inbound_exit")
                    end.set()

            handled_tools: set[str] = set()

            async def _apply_soft_handoff(role: str) -> None:
                """Swap instructions+tools on the live Omni session.

                Fire-and-forget `session.update` — do NOT recv here. The outbound
                loop owns the socket and will log session.updated. Competing recv
                would drop audio / FC events mid-handoff.
                """
                _log(
                    f"qwen soft handoff → {role} "
                    f"tools={tool_names(bp, role)} "
                    f"user={str(ctl.get('last_user_asr') or '')[:80]!r}"
                )
                await _set_forward(True, why="handoff")
                with contextlib.suppress(Exception):
                    await omni.send(json.dumps(session_update_for_agent(bp, role)))
                    _log(f"qwen soft handoff session.update sent → {role}")
                with contextlib.suppress(Exception):
                    await omni.send(json.dumps(nudge_greeting()))
                    _log(f"qwen soft handoff nudge → {role}")

            async def _dispatch_tool(
                *,
                name: str,
                arguments: str | dict,
                call_id: str,
                notify_model: bool = True,
                source: str = "fc",
            ) -> None:
                if not name:
                    return
                args_key = (
                    json.dumps(arguments, sort_keys=True, separators=(",", ":"))
                    if isinstance(arguments, dict)
                    else str(arguments)
                )
                key = (
                    f"{name}:{args_key}"
                    if name == "schedule_appointment"
                    else f"{name}:{args_key}:{call_id}"
                )
                if key in handled_tools:
                    return
                if name == "schedule_appointment" and any(
                    k.startswith("schedule_appointment:") for k in handled_tools
                ):
                    return
                handled_tools.add(key)
                if turns.agent_utt is not None:
                    await turns.end_agent(why=f"tool:{name}")
                ctl["pending_fn"] += 1
                prev_agent = state["agent"]
                if notify_model:
                    result, stop, reply = await handle_function_call(
                        name, arguments, call_id, bp, state
                    )
                    with contextlib.suppress(Exception):
                        await omni.send(json.dumps(reply))
                else:
                    if isinstance(arguments, str):
                        try:
                            args = json.loads(arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                    else:
                        args = dict(arguments or {})
                    result, stop = await run_tool(
                        name, args, bp, state, call_id=call_id
                    )
                ctl["pending_fn"] -= 1
                _log(
                    f"qwen tool {name} -> {result.get('success')} "
                    f"source={source} agent={prev_agent}→{state['agent']}"
                )
                role = handoff_role(result, bp)
                if role and role != prev_agent:
                    # Soft handoff: keep history; retarget tools on same WS.
                    await _apply_soft_handoff(role)
                elif stop:
                    asyncio.create_task(_close_all(omni, ws, end))
                else:
                    ctl["need_continue"] = True
                    if ctl["pending_fn"] == 0 and ctl["audio_done"]:
                        ctl["need_continue"] = False
                        with contextlib.suppress(Exception):
                            await omni.send(json.dumps(nudge_greeting()))

            async def _maybe_infer_schedule(transcript: str) -> None:
                agent = state["agent"]
                if "schedule_appointment" not in tool_names(bp, agent):
                    return
                if any(k.startswith("schedule_appointment:") for k in handled_tools):
                    return
                blob = " ".join(spoken[-6:] + [transcript]).strip()
                args = infer_schedule_appointment(blob)
                if not args:
                    return
                _log(
                    f"qwen infer schedule_appointment {args} "
                    f"from={transcript[:80]!r}"
                )
                await _dispatch_tool(
                    name="schedule_appointment",
                    arguments=args,
                    call_id=f"infer_{_eid()[:8]}",
                    notify_model=False,
                    source="infer",
                )

            async def _forward_agent_pcm(pcm24: bytes, down_state):
                pcm16, down_state = audioop.ratecv(
                    pcm24, W, 1, R_OMNI_OUT, R_CHIRP, down_state
                )
                if not pcm16:
                    return down_state
                rms = audioop.rms(pcm16, W) if len(pcm16) >= W else 0
                muted = not ctl["forward_agent"]
                stats.note_out(len(pcm16), rms, muted=muted)
                if muted:
                    if _LOG_AUDIO:
                        _log(
                            f"agent→chirp DROP bytes={len(pcm16)} rms={rms} "
                            f"why={ctl['mute_why']}"
                        )
                    return down_state
                if rms >= 200:
                    ctl["last_agent_loud"] = time.monotonic()
                ctl["response_active"] = True
                ctl["audio_done"] = False
                if turns.agent_utt is None:
                    await turns.start_agent()
                turns.note_agent_bytes(len(pcm16))
                await ws.send(pcm16)
                if _LOG_AUDIO:
                    _log(f"agent→chirp bytes={len(pcm16)} rms={rms}")
                stats.maybe_summary()
                return down_state

            async def outbound() -> None:
                down = None
                try:
                    async for raw in omni:
                        if end.is_set():
                            break
                        if isinstance(raw, bytes):
                            down = await _forward_agent_pcm(raw, down)
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")

                        if etype == "input_audio_buffer.speech_started":
                            stats.omni_speech_starts += 1
                            ctl["omni_user_speaking"] = True
                            _log(f"qwen VAD speech_started {_ctl_snap()}")
                            await _on_real_barge_in(why="omni_vad")
                            continue
                        if etype == "input_audio_buffer.speech_stopped":
                            ctl["omni_user_speaking"] = False
                            _log(f"qwen VAD speech_stopped {_ctl_snap()}")
                            await _set_forward(True, why="omni_vad_stopped")
                            continue

                        if etype in {
                            "response.output_audio.delta",
                            "response.audio.delta",
                        }:
                            if not ctl["forward_agent"] and not ctl["omni_user_speaking"]:
                                await _set_forward(True, why="agent_audio_resume")
                            b64 = event.get("delta") or event.get("audio") or ""
                            if not b64:
                                continue
                            pcm24 = base64.b64decode(b64)
                            down = await _forward_agent_pcm(pcm24, down)

                        elif etype in {
                            "response.output_audio.done",
                            "response.audio.done",
                            "response.done",
                        }:
                            await turns.end_agent(why=etype)
                            ctl["audio_done"] = True
                            ctl["response_active"] = False
                            if (
                                ctl["need_continue"]
                                and ctl["pending_fn"] == 0
                                and not end.is_set()
                            ):
                                ctl["need_continue"] = False
                                with contextlib.suppress(Exception):
                                    await omni.send(json.dumps(nudge_greeting()))

                        elif etype in {
                            "response.output_audio_transcript.delta",
                            "response.audio_transcript.delta",
                            "response.output_text.delta",
                            "response.text.delta",
                        }:
                            turns.note_agent_text(event.get("delta") or "")

                        elif etype in {
                            "response.output_audio_transcript.done",
                            "response.audio_transcript.done",
                        }:
                            tr = (event.get("transcript") or "").strip()
                            if tr:
                                _log(f"qwen transcript={tr[:160]}")
                                spoken.append(tr)
                                ctl["last_spoken"] = tr
                                if tr not in "".join(turns.agent_text):
                                    turns.note_agent_text(tr)
                                await _maybe_infer_schedule(tr)

                        elif etype in {
                            "conversation.item.input_audio_transcription.completed",
                            "conversation.item.input_audio_transcription.updated",
                        }:
                            tr = (event.get("transcript") or "").strip()
                            if tr and etype.endswith("completed"):
                                ctl["last_user_asr"] = tr
                            tag = (
                                "USER_ASR"
                                if etype.endswith("completed")
                                else "USER_ASR_partial"
                            )
                            _log(f"qwen {tag}={tr[:200]!r} {_ctl_snap()}")

                        elif etype == "response.function_call_arguments.done":
                            await _dispatch_tool(
                                name=event.get("name", ""),
                                arguments=event.get("arguments") or "{}",
                                call_id=event.get("call_id") or _eid(),
                                source="fc",
                            )

                        elif etype == "error":
                            err = (event.get("error") or {}).get("message") or event
                            _log(f"qwen error: {err}")
                        elif etype == "session.end":
                            _log("qwen session.end")
                            end.set()
                            break
                        elif etype in {
                            "response.created",
                            "response.cancelled",
                            "rate_limits.updated",
                            "session.updated",
                        }:
                            if etype == "response.cancelled":
                                ctl["response_active"] = False
                                _log(f"qwen response.cancelled {_ctl_snap()}")
                            elif etype == "response.created":
                                _log("qwen response.created")
                            elif etype == "session.updated":
                                tools = (event.get("session") or {}).get("tools") or []
                                _log(f"qwen session.updated tools={len(tools)}")
                finally:
                    await turns.end_agent(why="outbound_exit")
                    end.set()

            await omni.send(json.dumps(nudge_greeting()))
            _log(f"qwen nudge_greeting → {bp['start']}")

            tasks = [
                asyncio.create_task(inbound()),
                asyncio.create_task(outbound()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            end.set()
            await turns.close()
            stats.maybe_summary(force=True)
            _log(
                f"CALL END chops={stats.chops} barge={stats.barge_ins} "
                f"echo_ign={stats.echo_ignores} "
                f"in={stats.in_bytes}B out={stats.out_bytes}B "
                f"muted={stats.out_muted_bytes}B"
            )
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
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)


async def _handler(ws, industry: str, model: str) -> None:
    expected = _auth()
    if expected and ws.request.headers.get("Authorization") != expected:
        await ws.close(1008, "unauthorized")
        return
    try:
        await _bridge(ws, industry, model)
    except Exception as e:
        if type(e).__name__.startswith("ConnectionClosed"):
            return
        _log(f"chirp bridge error: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await ws.close(1011, "bridge error")


def main(model: str | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8769")))
    p.add_argument(
        "--model", default=model or os.environ.get("QWEN_OMNI_MODEL", MODEL)
    )
    a = p.parse_args()
    industry_path(a.industry)
    print(
        f"ws↔Qwen Omni {a.model} × {a.industry} chirp=:{a.port} "
        f"upstream={ws_url(a.model)} auth={bool(_auth())} "
        f"log={_LOG_LEVEL} echo_suppress={ECHO_SUPPRESS_S}s",
        flush=True,
    )

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.industry, a.model), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
