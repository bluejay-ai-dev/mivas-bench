"""CHIRP (16 kHz pcm_s16le) ↔ xAI Grok Voice (24 kHz PCM).

Hard multi-agent: one Grok Realtime WS per blueprint agent; only the active
agent receives input audio and has its output forwarded. Handoff rewires
CHIRP audio to the target session and nudges it with bare `response.create`.

Speak-first: after session.updated, send `{"type":"response.create"}`.

Audio barge-in (critical):
  Bluejay CHIRP `speech.started` often fires on *agent echo* in the mixed
  recording path. Muting/cancelling on that signal hard-chops agent PCM
  ("Hey," then silence) and starves Grok of the DH's full utterance.
  Echo of our own TTS is also loud on the inbound RMS meter — `user_loud`
  must not override echo risk (nemotron policy: echo wins).
  Correct policy:
    - While the agent utterance is open (or just ended), treat inbound as
      echo unless RMS is ~2× the user threshold (true barge-over-TTS).
    - Do not forward echo PCM to Grok — server_vad will cancel the greeting.
    - Forward agent PCM to Bluejay unless a *real* barge-in is confirmed.

Observability: GROK_CHIRP_LOG=full|audio|off (default full). Periodic
byte/RMS summaries + every mute/chop/speech event with reason.
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
    MODEL,
    SAMPLE_RATE,
    call_session,
    configure_session,
    connect_grok,
    handle_function_call,
    handoff_role,
    handoff_seed_events,
    industry_path,
    infer_schedule_appointment,
    load_blueprint,
    nudge_greeting,
    run_tool,
    set_call_id,
    tool_names,
    ws_url,
)
from pcm import PcmPacer  # noqa: E402
from report import end_speech_span, start_speech_span, traced_run  # noqa: E402

W, R_GROK, R_CHIRP = 2, SAMPLE_RATE, 16_000

# Ignore CHIRP speech.started while agent TTS was just loud (speaker echo → fake DH VAD).
ECHO_SUPPRESS_S = float(os.environ.get("GROK_ECHO_SUPPRESS_S", "1.25"))
# Inbound PCM must clear this RMS to count as a real barge-in (with CHIRP VAD).
USER_RMS_ON = int(os.environ.get("GROK_USER_RMS_ON", "350"))
USER_LIVE_S = float(os.environ.get("GROK_USER_LIVE_S", "0.35"))
# Agent utterance ended under this duration after a mute → logged as CHOP.
CHOP_WARN_MS = int(os.environ.get("GROK_CHOP_WARN_MS", "400"))

_LOG_LEVEL = (os.environ.get("GROK_CHIRP_LOG") or "full").strip().lower()
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
    """Per-call counters — printed on a timer and at call end."""

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
        self.grok_speech_starts = 0
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
            f"chirp_vad={self.chirp_speech_starts} grok_vad={self.grok_speech_starts}"
        )


class _Turns:
    """At most one open agent.speech and one customer.speech, both under voice.call."""

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
        # Tracing only — do NOT end agent audio here. Ending agent.speech for
        # OTel must not imply we drop PCM (that was the hard-chop bug).
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


async def _open_sessions(bp: dict[str, Any], model: str) -> tuple[dict[str, Any], list[Any]]:
    async def one(agent: str) -> tuple[str, Any, Any]:
        cm = connect_grok(model)
        grok = await cm.__aenter__()
        try:
            raw = await asyncio.wait_for(grok.recv(), timeout=60)
            first = json.loads(raw) if isinstance(raw, str) else {}
            _log(f"grok[{agent}] {first.get('type')}")
            updated = await configure_session(grok, agent, bp)
            n = len((updated.get("session") or {}).get("tools") or [])
            _log(f"grok[{agent}] session.updated tools_registered={n}")
        except Exception:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            raise
        return agent, grok, cm

    results = await asyncio.gather(*(one(a) for a in bp["agents"]))
    sessions = {name: grok for name, grok, _ in results}
    cms = [cm for _, _, cm in results]
    return sessions, cms


async def _close_all(sessions: dict[str, Any], chirp_ws, end: asyncio.Event) -> None:
    await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
    end.set()
    for _name, grok in list(sessions.items()):
        with contextlib.suppress(Exception):
            await grok.close()
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
    set_call_id(sim_id)

    _log(
        f"grok ws={ws_url(model)} agents={list(bp['agents'])} start={bp['start']} "
        f"echo_suppress={ECHO_SUPPRESS_S}s user_rms_on={USER_RMS_ON}"
    )

    sessions: dict[str, Any] = {}
    session_cms: list[Any] = []
    try:
        # call_session freezes this call's DB to S3 on exit.
        async with traced_run(
            workflow, simulation_result_id=sim_id, model=model
        ) as otel_root, call_session(sim_id):
            state["_otel_root"] = otel_root
            sessions, session_cms = await _open_sessions(bp, model)
            turns = _Turns(ws, otel_root, stats)
            ctl = {
                "customer_speaking": False,  # CHIRP VAD (may be echo)
                "grok_user_speaking": False,  # Grok server_vad — ground truth
                "forward_agent": True,
                "pending_fn": 0,
                "need_continue": False,
                "audio_done": True,
                "response_active": False,
                "last_agent_loud": 0.0,
                "last_user_loud": 0.0,
                "mute_why": "",
                # Handoff context — last USER_ASR + agent transcript before switch.
                "last_user_asr": "",
                "last_spoken": "",
            }
            spoken: dict[str, list[str]] = {a: [] for a in sessions}

            async def _paced_send(frame: bytes) -> None:
                # stamp loudness when PCM actually hits CHIRP so the echo
                # window covers playback, not Grok's burst-push.
                if frame:
                    rms = audioop.rms(frame, W) if len(frame) >= W else 0
                    if rms >= 200:
                        ctl["last_agent_loud"] = time.monotonic()
                await ws.send(frame)

            pacer = PcmPacer(_paced_send)
            pacer_task = asyncio.create_task(pacer.run())

            def active_ws():
                return sessions[state["agent"]]

            def _agent_echo_risk(now: float | None = None) -> bool:
                now = now if now is not None else time.monotonic()
                # open agent speech, or mix delay after the last paced frame.
                if turns.agent_utt is not None:
                    return True
                return bool(ctl["last_agent_loud"]) and (
                    now - float(ctl["last_agent_loud"]) < ECHO_SUPPRESS_S
                )

            def _user_loud_recent(now: float | None = None) -> bool:
                now = now if now is not None else time.monotonic()
                return bool(ctl["last_user_loud"]) and (
                    now - float(ctl["last_user_loud"]) < USER_LIVE_S
                )

            def _real_barge_in(now: float | None = None) -> bool:
                """user is talking — not Bluejay VAD hearing our TTS."""
                if _agent_echo_risk(now):
                    return False
                return _user_loud_recent(now)

            def _ctl_snap() -> str:
                return (
                    f"active={state['agent']} chirp_vad={ctl['customer_speaking']} "
                    f"grok_vad={ctl['grok_user_speaking']} fwd={ctl['forward_agent']} "
                    f"resp={ctl['response_active']} echo={_agent_echo_risk()} "
                    f"user_loud={_user_loud_recent()} mute_why={ctl['mute_why']!r} "
                    f"agent_utt={turns.agent_utt}"
                )

            async def _send_pcm24_to_active(pcm24: bytes) -> None:
                if not pcm24 or end.is_set():
                    return
                await active_ws().send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "event_id": _eid(),
                            "audio": base64.b64encode(pcm24).decode("ascii"),
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
                    await active_ws().send(
                        json.dumps({"type": "response.cancel", "event_id": _eid()})
                    )

            async def _on_real_barge_in(*, why: str) -> None:
                """Confirmed user speech overlapping agent — mute + cancel."""
                if turns.agent_utt is None and not ctl["response_active"]:
                    # idle: user started talking. muting here sticks FORWARD MUTE
                    # until grok_vad_stopped and the next greeting never plays.
                    _log(f"barge SKIP (agent idle) why={why} {_ctl_snap()}")
                    return
                stats.barge_ins += 1
                await _set_forward(False, why=why)
                await turns.end_agent(why=f"barge:{why}")
                await _cancel_active(why=why)

            async def inbound() -> None:
                up = None
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            rms = audioop.rms(msg, W) if len(msg) >= W else 0
                            stats.note_in(len(msg), rms)
                            echo = _agent_echo_risk()
                            loud = rms >= USER_RMS_ON
                            if loud and not echo:
                                ctl["last_user_loud"] = time.monotonic()
                            elif loud and echo and rms >= USER_RMS_ON * 2:
                                # real barge-in over TTS: much louder than echo.
                                ctl["last_user_loud"] = time.monotonic()
                                echo = False
                            if echo:
                                # TTS looped into the mix. feeding it to Grok
                                # makes server_vad cancel the greeting.
                                stats.maybe_summary()
                                continue
                            pcm, up = audioop.ratecv(msg, W, 1, R_CHIRP, R_GROK, up)
                            if pcm:
                                await _send_pcm24_to_active(pcm)
                                if _LOG_AUDIO:
                                    _log(
                                        f"user→grok bytes={len(pcm)} rms={rms} "
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
                            # echo of our own TTS → Bluejay VAD. echo wins over
                            # inbound RMS (echo is loud on the mixed path).
                            if not _real_barge_in():
                                stats.echo_ignores += 1
                                _log(
                                    f"CHIRP speech.started IGNORED echo uid={uid} "
                                    f"{_ctl_snap()}"
                                )
                                await turns.start_customer(uid, why="chirp_echo_ignored")
                                continue
                            ctl["customer_speaking"] = True
                            await turns.start_customer(uid, why="chirp_speech.started")
                            if turns.agent_utt is not None or ctl["response_active"]:
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
                            # If we muted on CHIRP VAD and Grok never confirmed user
                            # speech, resume forwarding so we don't stick muted.
                            if (
                                not ctl["grok_user_speaking"]
                                and not ctl["forward_agent"]
                                and ctl["mute_why"].startswith("chirp_")
                            ):
                                await _set_forward(True, why="chirp_speech.completed")
                finally:
                    ctl["customer_speaking"] = False
                    await turns.end_customer(why="inbound_exit")
                    end.set()

            handled_tools: set[str] = set()

            async def _dispatch_tool(
                *,
                name: str,
                arguments: str | dict,
                call_id: str,
                outgoing: str,
                outgoing_ws,
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
                    f"{outgoing}:{name}:{args_key}"
                    if name == "schedule_appointment"
                    else f"{outgoing}:{name}:{args_key}:{call_id}"
                )
                if key in handled_tools:
                    return
                if name == "schedule_appointment" and any(
                    k.startswith(f"{outgoing}:schedule_appointment:")
                    for k in handled_tools
                ):
                    return
                handled_tools.add(key)
                if state["agent"] != outgoing:
                    _log(f"grok[{outgoing}] ignore tool on inactive {name}")
                    return
                # Do not end_agent here in a way that implies audio chop — tool
                # spans are siblings; close the speech span cleanly first.
                if turns.agent_utt is not None:
                    await turns.end_agent(why=f"tool:{name}")
                ctl["pending_fn"] += 1
                if notify_model:
                    result, stop, reply = await handle_function_call(
                        name, arguments, call_id, bp, state
                    )
                    with contextlib.suppress(Exception):
                        await outgoing_ws.send(json.dumps(reply))
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
                    f"grok[{outgoing}] tool {name} -> {result.get('success')} "
                    f"source={source}"
                )
                role = handoff_role(result, bp)
                if role and role != outgoing:
                    user_ctx = str(ctl.get("last_user_asr") or "")
                    prior = str(ctl.get("last_spoken") or "")
                    _log(
                        f"grok handoff → {role} "
                        f"user={user_ctx[:80]!r} prior={prior[:80]!r}"
                    )
                    ctl["need_continue"] = False
                    await _set_forward(True, why="handoff")
                    # Seed cold dual-session target with prior turns (OpenAI soft
                    # handoff equivalent), then nudge — not a blank greeting.
                    target = sessions[role]
                    for ev in handoff_seed_events(
                        user_said=user_ctx, prior_agent_said=prior
                    ):
                        with contextlib.suppress(Exception):
                            await target.send(json.dumps(ev))
                            _log(f"handoff seed → {role} role={ev['item']['role']}")
                    with contextlib.suppress(Exception):
                        await target.send(json.dumps(nudge_greeting()))
                        _log(f"handoff nudge → {role}")
                elif stop:
                    await pacer.wait_until_idle()
                    asyncio.create_task(_close_all(sessions, ws, end))
                else:
                    ctl["need_continue"] = True
                    if ctl["pending_fn"] == 0 and ctl["audio_done"]:
                        ctl["need_continue"] = False
                        with contextlib.suppress(Exception):
                            await outgoing_ws.send(json.dumps(nudge_greeting()))

            async def _maybe_infer_schedule(agent: str, transcript: str) -> None:
                if agent != state["agent"]:
                    return
                if "schedule_appointment" not in tool_names(bp, agent):
                    return
                if any(
                    k.startswith(f"{agent}:schedule_appointment:") for k in handled_tools
                ):
                    return
                blob = " ".join(spoken.get(agent, [])[-6:] + [transcript]).strip()
                args = infer_schedule_appointment(blob)
                if not args:
                    return
                _log(
                    f"grok[{agent}] infer schedule_appointment {args} "
                    f"from={transcript[:80]!r}"
                )
                await _dispatch_tool(
                    name="schedule_appointment",
                    arguments=args,
                    call_id=f"infer_{agent}_{_eid()[:8]}",
                    outgoing=agent,
                    outgoing_ws=sessions[agent],
                    notify_model=False,
                    source="infer",
                )

            async def _forward_agent_pcm(agent: str, pcm24: bytes, down_state):
                """Resample 24k→16k and send to CHIRP unless muted."""
                pcm16, down_state = audioop.ratecv(
                    pcm24, W, 1, R_GROK, R_CHIRP, down_state
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
                ctl["response_active"] = True
                ctl["audio_done"] = False
                if turns.agent_utt is None:
                    await turns.start_agent()
                turns.note_agent_bytes(len(pcm16))
                pacer.push(pcm16)
                if _LOG_AUDIO:
                    _log(f"agent→chirp bytes={len(pcm16)} rms={rms}")
                stats.maybe_summary()
                return down_state

            async def outbound_agent(agent: str) -> None:
                grok = sessions[agent]
                down = None
                try:
                    async for raw in grok:
                        if end.is_set():
                            break
                        if isinstance(raw, bytes):
                            if state["agent"] != agent:
                                continue
                            down = await _forward_agent_pcm(agent, raw, down)
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        is_active = state["agent"] == agent

                        # Grok server_vad — authoritative barge-in signal (xAI docs).
                        if etype == "input_audio_buffer.speech_started":
                            if is_active:
                                stats.grok_speech_starts += 1
                                if _agent_echo_risk():
                                    stats.echo_ignores += 1
                                    _log(
                                        f"grok VAD speech_started IGNORED echo "
                                        f"{_ctl_snap()}"
                                    )
                                    continue
                                ctl["grok_user_speaking"] = True
                                _log(f"grok VAD speech_started {_ctl_snap()}")
                                await _on_real_barge_in(why="grok_vad")
                            continue
                        if etype == "input_audio_buffer.speech_stopped":
                            if is_active:
                                ctl["grok_user_speaking"] = False
                                _log(f"grok VAD speech_stopped {_ctl_snap()}")
                                await _set_forward(True, why="grok_vad_stopped")
                            continue

                        if etype in {
                            "response.output_audio.delta",
                            "response.audio.delta",
                        }:
                            if not is_active:
                                continue
                            # New agent audio after barge-in → resume unless Grok
                            # still hears the user.
                            if not ctl["forward_agent"] and not ctl["grok_user_speaking"]:
                                await _set_forward(True, why="agent_audio_resume")
                            b64 = event.get("delta") or event.get("audio") or ""
                            if not b64:
                                continue
                            pcm24 = base64.b64decode(b64)
                            down = await _forward_agent_pcm(agent, pcm24, down)

                        elif etype in {
                            "response.output_audio.done",
                            "response.audio.done",
                            "response.done",
                        }:
                            if is_active:
                                await pacer.wait_until_idle()
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
                                        await grok.send(json.dumps(nudge_greeting()))

                        elif etype in {
                            "response.output_audio_transcript.delta",
                            "response.output_text.delta",
                        }:
                            if is_active:
                                turns.note_agent_text(event.get("delta") or "")

                        elif etype == "response.output_audio_transcript.done":
                            if is_active:
                                tr = (event.get("transcript") or "").strip()
                                if tr:
                                    _log(f"grok[{agent}] transcript={tr[:160]}")
                                    spoken.setdefault(agent, []).append(tr)
                                    ctl["last_spoken"] = tr
                                    if tr not in "".join(turns.agent_text):
                                        turns.note_agent_text(tr)
                                    await _maybe_infer_schedule(agent, tr)

                        elif etype in {
                            "conversation.item.input_audio_transcription.completed",
                            "conversation.item.input_audio_transcription.updated",
                        }:
                            # What Grok actually heard from the DH — gold signal for
                            # "DH audio cut off before the model".
                            if is_active:
                                tr = (event.get("transcript") or "").strip()
                                if tr and etype.endswith("completed"):
                                    ctl["last_user_asr"] = tr
                                tag = (
                                    "USER_ASR"
                                    if etype.endswith("completed")
                                    else "USER_ASR_partial"
                                )
                                _log(
                                    f"grok[{agent}] {tag}={tr[:200]!r} {_ctl_snap()}"
                                )

                        elif etype == "response.function_call_arguments.done":
                            await _dispatch_tool(
                                name=event.get("name", ""),
                                arguments=event.get("arguments") or "{}",
                                call_id=event.get("call_id") or _eid(),
                                outgoing=agent,
                                outgoing_ws=grok,
                                source="fc",
                            )

                        elif etype == "error":
                            err = (event.get("error") or {}).get("message") or event
                            _log(f"grok[{agent}] error: {err}")
                        elif etype == "session.end":
                            _log(f"grok[{agent}] session.end")
                            if is_active:
                                end.set()
                                break
                        elif etype in {
                            "response.created",
                            "response.cancelled",
                            "rate_limits.updated",
                            "session.updated",
                        }:
                            if etype == "response.cancelled" and is_active:
                                ctl["response_active"] = False
                                _log(f"grok[{agent}] response.cancelled {_ctl_snap()}")
                            elif etype == "response.created" and is_active:
                                _log(f"grok[{agent}] response.created")
                finally:
                    if state["agent"] == agent:
                        await turns.end_agent(why="outbound_exit")
                        end.set()

            await sessions[bp["start"]].send(json.dumps(nudge_greeting()))
            _log(f"grok nudge_greeting → {bp['start']}")

            async def _greeting_watchdog() -> None:
                # first TTS used to be inaudible ("You with."). re-nudge
                # only if no agent PCM actually hit CHIRP.
                await asyncio.sleep(3.0)
                if end.is_set():
                    return
                if ctl.get("last_user_asr"):
                    _log("greeting watchdog skip (user asr)")
                    return
                if _user_loud_recent() or turns.agent_utt is not None:
                    _log("greeting watchdog skip (live speech)")
                    return
                if ctl.get("last_agent_loud"):
                    _log("greeting watchdog skip (already greeted)")
                    return
                _log("greeting watchdog re-nudge")
                with contextlib.suppress(Exception):
                    await sessions[state["agent"]].send(json.dumps(nudge_greeting()))

            watchdog = asyncio.create_task(_greeting_watchdog())
            tasks = [asyncio.create_task(inbound())] + [
                asyncio.create_task(outbound_agent(a)) for a in sessions
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            end.set()
            watchdog.cancel()
            await pacer.wait_until_idle()
            pacer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pacer_task, timeout=5)
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
        for cm in reversed(session_cms):
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
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8768")))
    p.add_argument("--model", default=model or os.environ.get("GROK_VOICE_MODEL", MODEL))
    a = p.parse_args()
    industry_path(a.industry)
    print(
        f"ws↔Grok {a.model} × {a.industry} chirp=:{a.port} "
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
