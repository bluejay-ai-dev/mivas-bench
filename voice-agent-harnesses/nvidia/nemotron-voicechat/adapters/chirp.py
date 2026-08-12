"""CHIRP (16 kHz pcm) ↔ N VoiceChat Realtime sessions (24 kHz), one per blueprint agent.

Hard multi-agent: all sessions stay open; only the active agent receives input audio
and has its output forwarded. Handoff rewires the active agent (Pipecat S2S switcher)
and nudges the target with response.create — never a speech-shaped kick (that makes a
cold session open-greet mid-call).

VoiceChat is full-duplex: it generates while input is flowing. Call-open speak-first
uses a short speech-shaped kick + trail silence. Agent.speech opens on transcript or
sustained RMS (not kick noise blips); customer.speech coalesces CHIRP VAD fragments.
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
import wave
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness import industry_path, load_blueprint  # noqa: E402
from report import end_speech_span, start_speech_span, traced_run  # noqa: E402
from voicechat import (  # noqa: E402
    MODEL,
    SAMPLE_RATE,
    connect_voicechat,
    handle_function_call,
    handoff_continue_events,
    handoff_nudge_event,
    handoff_role,
    infer_tool_calls,
    looks_like_open_greeting,
    parse_toolcalls,
    session_update_for_agent,
    speaks_first,
    ws_url,
)

W, R_VC, R_CHIRP = 2, SAMPLE_RATE, 16_000
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "2.5"))
SILENCE_CHUNK_MS = 60
SILENCE_BYTES = int(R_VC * (SILENCE_CHUNK_MS / 1000.0)) * W
SILENCE_PCM = b"\x00" * SILENCE_BYTES
SILENCE_B64 = base64.b64encode(SILENCE_PCM).decode("ascii")
# VoiceChat is full-duplex and barges in on any non-zero-ish input. CHIRP streams
# idle PCM (line noise); forwarding it makes the agent "interrupt itself". Bluejay
# speech.* + RMS decide when user audio is real. Agent audio is muted the instant
# the user is live so the DH hears a gap and can register the interruption.
AGENT_RMS_ON = int(os.environ.get("VOICECHAT_AGENT_RMS_ON", "500"))
AGENT_SILENCE_S = float(os.environ.get("VOICECHAT_AGENT_SILENCE_S", "0.55"))
# Cumulative loud ms within a gap-tolerant window before opening agent.speech.
AGENT_MIN_LOUD_MS = int(os.environ.get("VOICECHAT_AGENT_MIN_LOUD_MS", "120"))
# Short intra-word quiet must not zero the loud accumulator (was dropping PCM).
AGENT_LOUD_GAP_S = float(os.environ.get("VOICECHAT_AGENT_LOUD_GAP_S", "0.25"))
USER_RMS_ON = int(os.environ.get("VOICECHAT_USER_RMS_ON", "350"))
USER_LIVE_S = float(os.environ.get("VOICECHAT_USER_LIVE_S", "0.25"))
# Hang CHIRP VAD blips into one customer.speech until agent speaks or hang expires.
CUSTOMER_HANG_S = float(os.environ.get("VOICECHAT_CUSTOMER_HANG_S", "2.0"))
# Close agent.speech only after response.done + hang (not per-frame quiet).
AGENT_HANG_S = float(os.environ.get("VOICECHAT_AGENT_HANG_S", "2.2"))
KICK_TRAIL_SILENCE_S = float(os.environ.get("VOICECHAT_KICK_TRAIL_SILENCE_S", "3.5"))
# Ignore CHIRP speech.started while agent TTS was just loud (speaker echo → fake DH VAD).
ECHO_SUPPRESS_S = float(os.environ.get("VOICECHAT_ECHO_SUPPRESS_S", "0.75"))
_KICK_WAV = Path(__file__).resolve().parents[1] / "assets" / "speak_first_kick.wav"
# Full call logging on by default — set VOICECHAT_LOG=0 to quiet, =audio for PCM spam.
_LOG_LEVEL = (os.environ.get("VOICECHAT_LOG") or "full").strip().lower()
_LOG_ON = _LOG_LEVEL not in {"0", "off", "false", "no"}
_LOG_AUDIO = _LOG_LEVEL in {"audio", "pcm", "all"}

_call_t0: float | None = None
_audio_fwd_bytes = 0
_audio_drop_bytes = 0
_audio_last_summary = 0.0


def _ms() -> int:
    if _call_t0 is None:
        return 0
    return int((time.monotonic() - _call_t0) * 1000)


def _log(msg: str, *, every_ms: int | None = None, bucket: dict | None = None) -> None:
    """Call-relative log. Optional rate-limit via every_ms + mutable bucket['t']."""
    if not _LOG_ON:
        return
    if every_ms is not None and bucket is not None:
        now = time.monotonic()
        last = float(bucket.get("t") or 0.0)
        if now - last < every_ms / 1000.0:
            return
        bucket["t"] = now
    print(f"vc t={_ms():06d} {msg}", flush=True)


def _clip(s: str, n: int = 160) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


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


class _Turns:
    """At most one open agent.speech and one customer.speech, both under voice.call.

    Customer spans coalesce across CHIRP VAD blips (speech.completed → hang timer;
    a new speech.started before hang expiry keeps the same span). Agent start or
    hang expiry ends the customer turn. Barge-in closes the other speaker.
    """

    def __init__(
        self,
        ws,
        root,
        *,
        customer_hang_s: float = CUSTOMER_HANG_S,
        agent_hang_s: float = AGENT_HANG_S,
    ) -> None:
        self.ws = ws
        self.root = root
        self.customer_hang_s = customer_hang_s
        self.agent_hang_s = agent_hang_s
        self.agent_utt: str | None = None
        self.agent_span = None
        self.agent_text: list[str] = []
        self.customer_utt: str | None = None
        self.customer_span = None
        self._customer_hang: asyncio.Task | None = None
        self._agent_hang: asyncio.Task | None = None

    def _cancel_customer_hang(self, *, why: str = "") -> None:
        if self._customer_hang is not None:
            self._customer_hang.cancel()
            self._customer_hang = None
            if why:
                _log(f"customer.hang cancel ({why}) utt={self.customer_utt}")

    def _cancel_agent_hang(self, *, why: str = "") -> None:
        if self._agent_hang is not None:
            self._agent_hang.cancel()
            self._agent_hang = None
            if why:
                _log(f"agent.hang cancel ({why}) utt={self.agent_utt}")

    def agent_has_text(self) -> bool:
        return bool("".join(self.agent_text).strip())

    async def start_agent(self, *, why: str = "") -> None:
        self._cancel_agent_hang(why="start_agent")
        if self.agent_utt is not None:
            _log(f"agent.speech start SKIP already open utt={self.agent_utt} why={why}")
            return
        await self.end_customer(why=f"agent_start:{why}")
        self.agent_utt = f"u_{uuid.uuid4().hex[:12]}"
        # Keep prior text if we buffered transcript before opening the span.
        prior = "".join(self.agent_text).strip()
        self.agent_span = start_speech_span(
            self.agent_utt, speaker="agent", parent=self.root
        )
        await self.ws.send(_event("speech.started", {"utterance_id": self.agent_utt}))
        _log(
            f"agent.speech START utt={self.agent_utt} why={why} "
            f"prior_text={_clip(prior, 80)!r}"
        )

    async def note_agent_quiet(self, *, why: str = "") -> None:
        """RMS / response end — hold the span open so one turn ≠ many spans."""
        if self.agent_utt is None:
            return
        self._cancel_agent_hang(why="rearm")

        async def _hang() -> None:
            try:
                await asyncio.sleep(self.agent_hang_s)
                await self.end_agent(why=f"hang_expired:{why}")
            except asyncio.CancelledError:
                return

        self._agent_hang = asyncio.create_task(_hang())
        _log(
            f"agent.hang arm {self.agent_hang_s}s utt={self.agent_utt} why={why} "
            f"text={_clip(''.join(self.agent_text), 80)!r}"
        )

    async def end_agent(self, *, why: str = "") -> None:
        self._cancel_agent_hang()
        if self.agent_utt is None:
            self.agent_text = []
            return
        text = "".join(self.agent_text).strip()
        utt = self.agent_utt
        if text and self.agent_span is not None:
            with contextlib.suppress(Exception):
                self.agent_span.set_attribute("mivas.transcript", text[:500])
        with contextlib.suppress(Exception):
            await self.ws.send(
                _event("speech.completed", {"utterance_id": self.agent_utt})
            )
        end_speech_span(self.agent_span)
        self.agent_utt = None
        self.agent_span = None
        self.agent_text = []
        _log(f"agent.speech END utt={utt} why={why} text={_clip(text)!r}")

    def note_agent_text(self, delta: str) -> None:
        if delta:
            self.agent_text.append(delta)

    async def start_customer(self, uid: str, *, why: str = "") -> None:
        if self.agent_utt is not None:
            await self.end_agent(why=f"customer_barge:{why}")
        self._cancel_customer_hang(why="start_customer")
        if self.customer_utt is not None:
            # Same customer turn — Bluejay VAD re-segmented; keep one OTel span.
            if self.customer_span is not None and uid:
                with contextlib.suppress(Exception):
                    self.customer_span.set_attribute("mivas.utterance_id_last", str(uid))
            _log(
                f"customer.speech COALESCE keep={self.customer_utt} new={uid} why={why}"
            )
            return
        self.customer_utt = uid
        self.customer_span = start_speech_span(
            uid, speaker="customer", parent=self.root
        )
        _log(f"customer.speech START utt={uid} why={why}")

    async def note_customer_completed(self, *, why: str = "") -> None:
        """CHIRP speech.completed — hold the span open briefly for VAD blips."""
        if self.customer_utt is None:
            return
        self._cancel_customer_hang(why="rearm")

        async def _hang() -> None:
            try:
                await asyncio.sleep(self.customer_hang_s)
                await self.end_customer(why=f"hang_expired:{why}")
            except asyncio.CancelledError:
                return

        self._customer_hang = asyncio.create_task(_hang())
        _log(
            f"customer.hang arm {self.customer_hang_s}s utt={self.customer_utt} why={why}"
        )

    async def end_customer(self, *, why: str = "") -> None:
        self._cancel_customer_hang()
        if self.customer_utt is None:
            return
        utt = self.customer_utt
        end_speech_span(self.customer_span)
        self.customer_utt = None
        self.customer_span = None
        _log(f"customer.speech END utt={utt} why={why}")

    async def close(self) -> None:
        await self.end_agent(why="close")
        await self.end_customer(why="close")


def _simulation_result_id(ws) -> str | None:
    headers = getattr(getattr(ws, "request", None), "headers", None)
    if headers is None:
        return None
    val = headers.get("X-Simulation-Result-Id") or headers.get("x-simulation-result-id")
    return str(val).strip() if val else None


def _load_kick_pcm() -> bytes:
    """Short speech-shaped PCM @ 24 kHz to open a speak-first turn (not a greeting string)."""
    override = (os.environ.get("VOICECHAT_SPEAK_FIRST_KICK_WAV") or "").strip()
    path = Path(override) if override else _KICK_WAV
    if not path.is_file():
        raise FileNotFoundError(f"speak-first kick wav missing: {path}")
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"kick wav must be mono pcm16: {path}")
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    if rate != R_VC:
        pcm, _ = audioop.ratecv(pcm, W, 1, rate, R_VC, None)
    return pcm


async def _configure_session(vc_ws, agent: str, bp: dict[str, Any]) -> None:
    raw = await asyncio.wait_for(vc_ws.recv(), timeout=60)
    print(f"voicechat[{agent}] {json.loads(raw).get('type')}", flush=True)
    await vc_ws.send(json.dumps(session_update_for_agent(bp, agent)))
    while True:
        raw = await asyncio.wait_for(vc_ws.recv(), timeout=60)
        ev = json.loads(raw)
        et = ev.get("type")
        print(f"voicechat[{agent}] {et}", flush=True)
        if et == "session.updated":
            n = len((ev.get("session") or {}).get("tools") or [])
            print(f"voicechat[{agent}] tools_registered={n}", flush=True)
            return
        if et == "error":
            raise RuntimeError(f"session.update failed for {agent}: {ev}")


async def _open_sessions(bp: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    """Connect + configure one VoiceChat WS per blueprint agent (parallel)."""

    async def one(agent: str) -> tuple[str, Any, Any]:
        cm = connect_voicechat()
        vc = await cm.__aenter__()
        try:
            await _configure_session(vc, agent, bp)
        except Exception:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            raise
        return agent, vc, cm

    results = await asyncio.gather(*(one(a) for a in bp["agents"]))
    sessions = {name: vc for name, vc, _ in results}
    cms = [cm for _, _, cm in results]
    return sessions, cms


async def _close_all(sessions: dict[str, Any], chirp_ws, end: asyncio.Event) -> None:
    await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
    end.set()
    for name, vc in list(sessions.items()):
        with contextlib.suppress(Exception):
            await vc.send(json.dumps({"type": "session.close", "event_id": _eid()}))
        with contextlib.suppress(Exception):
            await vc.close()
    with contextlib.suppress(Exception):
        await chirp_ws.close(1000)


async def _bridge(ws, industry: str) -> None:
    global _call_t0, _audio_fwd_bytes, _audio_drop_bytes, _audio_last_summary
    _call_t0 = time.monotonic()
    _audio_fwd_bytes = 0
    _audio_drop_bytes = 0
    _audio_last_summary = 0.0

    bp = load_blueprint(industry)
    state = {"agent": bp["start"]}
    end = asyncio.Event()
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{MODEL}"
    sim_id = _simulation_result_id(ws)
    _log(
        f"CALL START sim={sim_id} industry={industry} log={_LOG_LEVEL} "
        f"agents={list(bp['agents'])} start={bp['start']}"
    )

    speak_first = speaks_first()
    kick_pcm = _load_kick_pcm() if speak_first else b""
    _log(
        f"config ws={ws_url()} speaks_first={speak_first} kick_bytes={len(kick_pcm)} "
        f"rms_on={AGENT_RMS_ON} min_loud_ms={AGENT_MIN_LOUD_MS} "
        f"agent_hang={AGENT_HANG_S}s customer_hang={CUSTOMER_HANG_S}s "
        f"user_live={USER_LIVE_S}s"
    )

    sessions: dict[str, Any] = {}
    session_cms: list[Any] = []
    try:
        async with traced_run(
            workflow, simulation_result_id=sim_id, model=MODEL
        ) as otel_root:
            state["_otel_root"] = otel_root
            sessions, session_cms = await _open_sessions(bp)
            turns = _Turns(ws, otel_root)
            _log(f"sessions open: {list(sessions)}")

            # No shared send-lock: the silence pump holding a lock added ~80ms to
            # every user frame and made barge-in lose the race.
            ctl = {
                "last_user_loud": 0.0,
                "last_agent_loud": 0.0,
                "agent_heard": False,
                "awaiting_agent": speak_first,
                "customer_speaking": False,
                "kick_generation": 0,
                # Call-open kick only — never set for handoff (avoids mid-call hello).
                "call_open_kick": False,
                # Last agent transcript blob — primed into the handoff target.
                "last_spoken": "",
                # After handoff, drop cold-open greeting audio until a real turn.
                "drop_greet": False,
                "greet_renudge_done": False,
            }
            rate = {"user_fwd": {}, "agent_fwd": {}, "agent_mute": {}, "nvcf_misc": {}}
            deferred_tools: list[dict[str, Any]] = []

            def active_ws():
                return sessions[state["agent"]]

            def _user_live(now: float | None = None) -> bool:
                now = now if now is not None else time.monotonic()
                if ctl["customer_speaking"]:
                    return True
                return bool(ctl["last_user_loud"]) and (
                    now - ctl["last_user_loud"] < USER_LIVE_S
                )

            def _agent_echo_risk(now: float | None = None) -> bool:
                """Agent TTS was just loud — CHIRP often VAD-fires on speaker echo."""
                now = now if now is not None else time.monotonic()
                return bool(ctl["agent_heard"]) and (
                    now - float(ctl["last_agent_loud"] or 0.0) < ECHO_SUPPRESS_S
                )

            def _ctl_snap() -> str:
                return (
                    f"active={state['agent']} cust_vad={ctl['customer_speaking']} "
                    f"awaiting={ctl['awaiting_agent']} heard={ctl['agent_heard']} "
                    f"kick={ctl['call_open_kick']} gen={ctl['kick_generation']} "
                    f"echo={_agent_echo_risk()} "
                    f"agent_utt={turns.agent_utt} cust_utt={turns.customer_utt}"
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

            async def _send_silence_chunk() -> None:
                if end.is_set() or _user_live():
                    return
                await active_ws().send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "event_id": _eid(),
                            "audio": SILENCE_B64,
                        }
                    )
                )

            async def _on_user_interrupt(*, why: str = "vad") -> None:
                """DH started talking: yield the agent on CHIRP immediately."""
                if _agent_echo_risk():
                    _log(f"BARGE-IN IGNORED echo_suppress why={why} {_ctl_snap()}")
                    return
                ctl["awaiting_agent"] = True
                if turns.agent_utt is not None:
                    _log(f"BARGE-IN why={why} {_ctl_snap()}")
                    await turns.end_agent(why=f"barge_in:{why}")

            async def speak_first_kick() -> None:
                """Call-open only: speech-shaped kick + trail silence."""
                gen = ctl["kick_generation"]
                try:
                    _log(f"speak_first_kick BEGIN → {state['agent']} bytes={len(kick_pcm)}")
                    ctl["awaiting_agent"] = True
                    ctl["call_open_kick"] = True
                    ctl["agent_heard"] = False
                    step = SILENCE_BYTES
                    for i in range(0, len(kick_pcm), step):
                        if end.is_set() or ctl["kick_generation"] != gen or _user_live():
                            _log(
                                f"speak_first_kick ABORT mid-kick "
                                f"user_live={_user_live()} gen_ok={ctl['kick_generation']==gen}"
                            )
                            return
                        chunk = kick_pcm[i : i + step]
                        if len(chunk) < step:
                            chunk = chunk + b"\x00" * (step - len(chunk))
                        await _send_pcm24_to_active(chunk)
                        await asyncio.sleep(SILENCE_CHUNK_MS / 1000.0)
                    _log("speak_first_kick trail silence…")
                    t0 = time.monotonic()
                    while not end.is_set() and ctl["kick_generation"] == gen:
                        if _user_live():
                            _log("speak_first_kick ABORT trail user_live")
                            return
                        now = time.monotonic()
                        if ctl["agent_heard"] and (
                            now - ctl["last_agent_loud"] > AGENT_SILENCE_S
                        ):
                            ctl["awaiting_agent"] = False
                            _log("speak_first_kick DONE (agent heard + quiet)")
                            return
                        if now - t0 > KICK_TRAIL_SILENCE_S:
                            ctl["awaiting_agent"] = False
                            _log(
                                f"speak_first_kick TIMEOUT heard={ctl['agent_heard']}"
                            )
                            return
                        await _send_silence_chunk()
                        await asyncio.sleep(SILENCE_CHUNK_MS / 1000.0)
                except Exception as e:
                    _log(f"speak_first_kick ERROR {type(e).__name__}: {e}")
                finally:
                    ctl["call_open_kick"] = False

            async def handoff_continue(target: str, *, prior_agent_said: str = "") -> None:
                """Seed history immediately; nudge only after the DH is quiet."""
                gen = ctl["kick_generation"]
                try:
                    events = handoff_continue_events(prior_agent_said=prior_agent_said)
                    notice = ""
                    with contextlib.suppress(Exception):
                        notice = events[0]["item"]["content"][0]["text"]
                    _log(
                        f"handoff_continue BEGIN → {target} prior={_clip(prior_agent_said)!r} "
                        f"notice={_clip(notice)!r}"
                    )
                    ctl["awaiting_agent"] = True
                    ctl["agent_heard"] = False
                    ctl["call_open_kick"] = False
                    vc = sessions[target]
                    for ev in events:
                        if end.is_set() or ctl["kick_generation"] != gen:
                            _log("handoff_continue ABORT before seed send")
                            return
                        _log(
                            f"handoff_continue send type={ev.get('type')} "
                            f"role={(ev.get('item') or {}).get('role')}"
                        )
                        await vc.send(json.dumps(ev))
                    # Wait out the DH so response.create does not open-greet over them.
                    wait_t0 = time.monotonic()
                    while not end.is_set() and ctl["kick_generation"] == gen:
                        if not ctl["customer_speaking"] and not _user_live():
                            break
                        if time.monotonic() - wait_t0 > 8.0:
                            _log("handoff_continue wait-quiet TIMEOUT — nudging anyway")
                            break
                        await asyncio.sleep(0.05)
                    if end.is_set() or ctl["kick_generation"] != gen:
                        return
                    nudge = handoff_nudge_event()
                    _log(f"handoff_continue send type={nudge['type']} (post-quiet)")
                    await vc.send(json.dumps(nudge))
                    t0 = time.monotonic()
                    while not end.is_set() and ctl["kick_generation"] == gen:
                        if ctl["customer_speaking"]:
                            _log("handoff_continue trail stop customer_speaking")
                            return
                        now = time.monotonic()
                        if ctl["agent_heard"] and (
                            now - ctl["last_agent_loud"] > AGENT_SILENCE_S
                        ):
                            ctl["awaiting_agent"] = False
                            _log("handoff_continue DONE (agent heard + quiet)")
                            return
                        if now - t0 > min(KICK_TRAIL_SILENCE_S, 3.0):
                            ctl["awaiting_agent"] = False
                            _log(
                                f"handoff_continue TIMEOUT heard={ctl['agent_heard']} "
                                f"{_ctl_snap()}"
                            )
                            return
                        await _send_silence_chunk()
                        await asyncio.sleep(SILENCE_CHUNK_MS / 1000.0)
                except Exception as e:
                    _log(f"handoff_continue ERROR {type(e).__name__}: {e}")

            async def duplex_pump() -> None:
                """Keep feeding silence while the agent should still be talking.

                VoiceChat is full-duplex: if input audio stops, generation stalls.
                After 'one moment please' the model goes quiet → without silence it
                freezes until the DH nudges (60s+ dead air on 716558).
                """
                try:
                    if speak_first:
                        await speak_first_kick()
                    while not end.is_set():
                        gen = ctl["kick_generation"]
                        await asyncio.sleep(SILENCE_CHUNK_MS / 1000.0)
                        if end.is_set() or ctl["kick_generation"] != gen:
                            continue
                        if _user_live() or ctl["customer_speaking"]:
                            ctl["awaiting_agent"] = True
                            continue
                        now = time.monotonic()
                        agent_speaking = ctl["agent_heard"] and (
                            now - ctl["last_agent_loud"] <= AGENT_SILENCE_S
                        )
                        # Keep duplex alive while a span is open or we expect more.
                        # Do NOT keep feeding silence for many seconds after quiet —
                        # that retriggers "one moment please" loops (716583 stuck 200s+).
                        need_silence = (
                            agent_speaking
                            or ctl["awaiting_agent"]
                            or turns.agent_utt is not None
                        )
                        if need_silence:
                            try:
                                await _send_silence_chunk()
                            except Exception as e:
                                if end.is_set():
                                    return
                                _log(f"duplex_pump ERROR {type(e).__name__}: {e}")
                                return
                except Exception as e:
                    _log(f"duplex_pump FATAL {type(e).__name__}: {e}")

            async def inbound() -> None:
                up = None
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            rms = audioop.rms(msg, W)
                            loud = rms >= USER_RMS_ON
                            if not (ctl["customer_speaking"] or loud):
                                continue
                            if ctl["customer_speaking"] and loud:
                                ctl["last_user_loud"] = time.monotonic()
                            pcm, up = audioop.ratecv(msg, W, 1, R_CHIRP, R_VC, up)
                            if pcm:
                                ctl["awaiting_agent"] = True
                                await _send_pcm24_to_active(pcm)
                                if _LOG_AUDIO:
                                    _log(
                                        f"user→nvcf bytes={len(pcm)} rms={rms} "
                                        f"vad={ctl['customer_speaking']} "
                                        f"active={state['agent']}",
                                        every_ms=250,
                                        bucket=rate["user_fwd"],
                                    )
                                else:
                                    _log(
                                        f"user→nvcf rms={rms} vad={ctl['customer_speaking']} "
                                        f"active={state['agent']}",
                                        every_ms=1000,
                                        bucket=rate["user_fwd"],
                                    )
                            continue
                        if not isinstance(msg, str):
                            continue
                        try:
                            event = json.loads(msg)
                        except json.JSONDecodeError:
                            _log(f"chirp JSONDecodeError: {msg[:80]!r}")
                            continue
                        etype = event.get("type")
                        data = event.get("data") or {}
                        if etype == "speech.started":
                            if _agent_echo_risk():
                                _log(
                                    f"CHIRP speech.started IGNORED echo "
                                    f"uid={data.get('utterance_id')} {_ctl_snap()}"
                                )
                                continue
                            ctl["customer_speaking"] = True
                            ctl["last_user_loud"] = time.monotonic()
                            uid = data.get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                            _log(f"CHIRP speech.started uid={uid} {_ctl_snap()}")
                            await _on_user_interrupt(why="chirp_speech.started")
                            await turns.start_customer(uid, why="chirp_speech.started")
                        elif etype == "speech.completed":
                            if not ctl["customer_speaking"] and turns.customer_utt is None:
                                _log(
                                    f"CHIRP speech.completed IGNORED (no open customer) "
                                    f"uid={data.get('utterance_id')}"
                                )
                                continue
                            ctl["customer_speaking"] = False
                            _log(
                                f"CHIRP speech.completed uid={data.get('utterance_id')} "
                                f"{_ctl_snap()}"
                            )
                            await turns.note_customer_completed(why="chirp_speech.completed")
                            ctl["awaiting_agent"] = True
                            await _flush_deferred_tools()
                        else:
                            _log(f"CHIRP event type={etype} data={_clip(json.dumps(data), 120)}")
                finally:
                    ctl["customer_speaking"] = False
                    await turns.end_customer(why="inbound_exit")
                    end.set()
                    _log("inbound EXIT end.set()")

            agent_tools = {
                a: session_update_for_agent(bp, a)["session"]["tools"] for a in sessions
            }
            handled_tools: set[str] = set()

            async def _dispatch_tool(
                *,
                name: str,
                arguments: str | dict,
                call_id: str,
                outgoing: str,
                outgoing_ws,
                source: str = "",
            ) -> None:
                if not name:
                    return
                args_key = (
                    json.dumps(arguments, sort_keys=True, separators=(",", ":"))
                    if isinstance(arguments, dict)
                    else str(arguments)
                )
                key = f"{outgoing}:{name}"
                # One schedule_appointment per call — model often re-confirms with a
                # different hallucinated date (03/10 then 03/02 on 716596).
                if key in handled_tools:
                    _log(
                        f"tool DEDUP skip {name} key={key} source={source} "
                        f"args={_clip(args_key, 80)}"
                    )
                    return
                # Never fire booking tools over the DH — queue and flush when quiet.
                if ctl["customer_speaking"] and name == "schedule_appointment":
                    _log(
                        f"tool DEFER {name} (customer_speaking) source={source} "
                        f"args={_clip(args_key, 80)}"
                    )
                    deferred_tools.append(
                        {
                            "name": name,
                            "arguments": arguments,
                            "call_id": call_id,
                            "outgoing": outgoing,
                            "outgoing_ws": outgoing_ws,
                            "source": f"flush:{source}",
                        }
                    )
                    return
                handled_tools.add(key)
                if state["agent"] != outgoing:
                    _log(
                        f"tool IGNORE inactive agent={outgoing} active={state['agent']} "
                        f"name={name} source={source}"
                    )
                    return
                _log(
                    f"tool DISPATCH {name} source={source} call_id={call_id} "
                    f"args={_clip(args_key)} {_ctl_snap()}"
                )
                await turns.end_agent(why=f"tool:{name}")
                result, stop, reply = await handle_function_call(
                    name, arguments, call_id, bp, state
                )
                _log(
                    f"tool RESULT {name} success={result.get('success')} "
                    f"result={_clip(json.dumps(result), 200)} stop={stop} "
                    f"active_now={state['agent']}"
                )
                with contextlib.suppress(Exception):
                    await outgoing_ws.send(json.dumps(reply))
                role = handoff_role(result, bp)
                if role and role != outgoing:
                    _log(
                        f"HANDOFF {outgoing} → {role} "
                        f"prior={_clip(str(ctl.get('last_spoken') or ''))!r}"
                    )
                    ctl["kick_generation"] += 1
                    ctl["drop_greet"] = True
                    ctl["greet_renudge_done"] = False
                    asyncio.create_task(
                        handoff_continue(
                            role, prior_agent_said=str(ctl.get("last_spoken") or "")
                        )
                    )
                if stop:
                    _log("tool requested END_CALL → close")
                    asyncio.create_task(_close_all(sessions, ws, end))

            async def _flush_deferred_tools() -> None:
                if not deferred_tools or ctl["customer_speaking"]:
                    return
                pending = list(deferred_tools)
                deferred_tools.clear()
                _log(f"tool FLUSH deferred n={len(pending)}")
                for item in pending:
                    await _dispatch_tool(**item)

            async def _maybe_tools(agent: str, vc, blob: str, *, prefix: str) -> None:
                calls = infer_tool_calls(agent_tools[agent], blob)
                if not calls:
                    _log(
                        f"infer_tools[{agent}] miss prefix={prefix} "
                        f"blob={_clip(blob)!r}",
                        every_ms=800,
                        bucket=rate.setdefault(f"infer_miss_{agent}", {}),
                    )
                    return
                _log(
                    f"infer_tools[{agent}] HIT prefix={prefix} "
                    f"calls={calls} blob={_clip(blob)!r}"
                )
                for call in calls:
                    await _dispatch_tool(
                        name=call["name"],
                        arguments=call["arguments"],
                        call_id=f"{prefix}_{agent}_{call['name']}",
                        outgoing=agent,
                        outgoing_ws=vc,
                        source=f"infer:{prefix}",
                    )

            async def _maybe_hard_tools(agent: str, vc, blob: str, *, prefix: str) -> None:
                """Streaming deltas: only native <TOOLCALL>, never soft ack/date infer."""
                calls = parse_toolcalls(blob)
                if not calls:
                    return
                _log(
                    f"hard_tools[{agent}] HIT prefix={prefix} "
                    f"calls={calls} blob={_clip(blob)!r}"
                )
                for call in calls:
                    await _dispatch_tool(
                        name=call["name"],
                        arguments=call["arguments"],
                        call_id=f"{prefix}_{agent}_{call['name']}",
                        outgoing=agent,
                        outgoing_ws=vc,
                        source=f"hard:{prefix}",
                    )

            async def outbound_agent(agent: str) -> None:
                vc = sessions[agent]
                down = None
                last_loud = 0.0
                loud_ms = 0.0
                text_buf = ""
                spoken = ""  # cumulative (handoff context)
                turn_spoken = ""  # this response only (tool inference)
                saw_audio_transcript = False
                n_audio = 0
                n_fwd = 0
                n_mute = 0

                async def _commit_agent(*, why: str) -> None:
                    if turns.agent_utt is not None:
                        return
                    if ctl["customer_speaking"]:
                        _log(f"agent.speech DEFER (customer_speaking) why={why}")
                        return
                    if ctl.get("drop_greet") and looks_like_open_greeting(
                        turn_spoken or "".join(turns.agent_text)
                    ):
                        _log(f"agent.speech SKIP greet span why={why}")
                        return
                    await turns.start_agent(why=why)

                def _remember_spoken() -> None:
                    if spoken.strip():
                        ctl["last_spoken"] = spoken.strip()

                def _note_text(delta: str) -> None:
                    nonlocal spoken, turn_spoken
                    spoken += delta
                    turn_spoken += delta
                    _remember_spoken()
                    turns.note_agent_text(delta)

                try:
                    async for raw in vc:
                        if end.is_set():
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            _log(f"nvcf[{agent}] JSONDecodeError: {str(raw)[:80]!r}")
                            continue
                        etype = event.get("type")
                        is_active = state["agent"] == agent
                        now = time.monotonic()

                        if etype == "response.output_audio.delta":
                            if not is_active:
                                _log(
                                    f"nvcf[{agent}] audio DROPPED inactive "
                                    f"active={state['agent']}",
                                    every_ms=2000,
                                    bucket=rate.setdefault(f"inactive_{agent}", {}),
                                )
                                continue
                            b64 = event.get("delta") or ""
                            if not b64:
                                continue
                            pcm24 = base64.b64decode(b64)
                            pcm16, down = audioop.ratecv(
                                pcm24, W, 1, R_VC, R_CHIRP, down
                            )
                            if not pcm16:
                                continue
                            n_audio += 1
                            rms = audioop.rms(pcm16, W)
                            # Mute over real customer speech. Do NOT end the span on
                            # USER_LIVE_S grace — that split "Sure" / "thing." into two.
                            if ctl["customer_speaking"]:
                                n_mute += 1
                                loud_ms = 0.0
                                if turns.agent_utt is not None:
                                    _log(
                                        f"nvcf[{agent}] MUTE+END customer_speaking "
                                        f"rms={rms} {_ctl_snap()}",
                                        every_ms=300,
                                        bucket=rate["agent_mute"],
                                    )
                                    await turns.end_agent(why="customer_speaking")
                                continue
                            if _user_live(now):
                                n_mute += 1
                                _log(
                                    f"nvcf[{agent}] MUTE grace user_live rms={rms}",
                                    every_ms=500,
                                    bucket=rate["agent_mute"],
                                )
                                continue
                            loud = rms >= AGENT_RMS_ON
                            frame_ms = 1000.0 * (len(pcm16) / W) / R_CHIRP
                            if loud:
                                last_loud = now
                                ctl["last_agent_loud"] = now
                                ctl["agent_heard"] = True
                                loud_ms += frame_ms
                            elif now - last_loud > AGENT_LOUD_GAP_S:
                                loud_ms = 0.0
                                # NVCF sometimes never sends response.done — close the
                                # span on prolonged quiet so duplex silence can stop.
                                if (
                                    turns.agent_utt is not None
                                    and last_loud > 0
                                    and now - last_loud > max(AGENT_HANG_S, 3.0)
                                ):
                                    await turns.note_agent_quiet(why="stall_quiet")
                                    ctl["awaiting_agent"] = False

                            # Post-handoff: eat cold-open greeting audio so the DH
                            # never hears a second "Hello, how can I help?".
                            if ctl.get("drop_greet"):
                                if turn_spoken and not looks_like_open_greeting(
                                    turn_spoken
                                ):
                                    ctl["drop_greet"] = False
                                    _log(
                                        f"nvcf[{agent}] drop_greet cleared "
                                        f"(non-greet turn={_clip(turn_spoken)!r})"
                                    )
                                else:
                                    _log(
                                        f"nvcf[{agent}] DROP greet audio "
                                        f"rms={rms} turn={_clip(turn_spoken)!r}",
                                        every_ms=400,
                                        bucket=rate.setdefault("drop_greet", {}),
                                    )
                                    continue

                            await ws.send(pcm16)
                            n_fwd += 1
                            if _LOG_AUDIO:
                                _log(
                                    f"nvcf[{agent}]→chirp bytes={len(pcm16)} rms={rms} "
                                    f"loud_ms={loud_ms:.0f} utt={turns.agent_utt}",
                                    every_ms=200,
                                    bucket=rate["agent_fwd"],
                                )

                            if turns.agent_utt is None:
                                # Prefer transcript; loud-only needs sustained audio and
                                # no active customer (avoids empty echo spans).
                                if turns.agent_has_text() or (
                                    loud_ms >= AGENT_MIN_LOUD_MS
                                    and not ctl["customer_speaking"]
                                ):
                                    why = (
                                        "transcript"
                                        if turns.agent_has_text()
                                        else f"loud_ms={loud_ms:.0f}"
                                    )
                                    await _commit_agent(why=why)
                            elif loud:
                                turns._cancel_agent_hang(why="loud_audio")

                        elif etype in {
                            "response.output_audio.done",
                            "response.done",
                        }:
                            _log(
                                f"nvcf[{agent}] {etype} active={is_active} "
                                f"turn={_clip(turn_spoken)!r} "
                                f"spoken={_clip(spoken)!r} audio_frames={n_audio} "
                                f"fwd={n_fwd} mute={n_mute} {_ctl_snap()}"
                            )
                            if is_active:
                                loud_ms = 0.0
                                if (
                                    ctl.get("drop_greet")
                                    and looks_like_open_greeting(turn_spoken)
                                ):
                                    _log(
                                        f"nvcf[{agent}] SUPPRESSED mid-call greeting "
                                        f"turn={_clip(turn_spoken)!r}"
                                    )
                                    turn_spoken = ""
                                    text_buf = ""
                                    if etype == "response.done":
                                        ctl["drop_greet"] = False
                                        ctl["awaiting_agent"] = True
                                        if not ctl.get("greet_renudge_done"):
                                            ctl["greet_renudge_done"] = True
                                            with contextlib.suppress(Exception):
                                                await vc.send(
                                                    json.dumps(handoff_nudge_event())
                                                )
                                            _log(
                                                f"nvcf[{agent}] re-nudge after "
                                                "suppressed greeting"
                                            )
                                    continue
                                if etype == "response.done":
                                    await turns.note_agent_quiet(why="response.done")
                                    ctl["awaiting_agent"] = False
                                blob = turn_spoken.strip() or spoken.strip()
                                if blob:
                                    _remember_spoken()
                                    await _maybe_tools(agent, vc, blob, prefix="done")
                                await _flush_deferred_tools()
                            if etype == "response.done":
                                text_buf = ""
                                turn_spoken = ""
                                saw_audio_transcript = False
                                n_audio = n_fwd = n_mute = 0

                        elif etype == "response.output_audio_transcript.delta":
                            if not is_active:
                                continue
                            delta = event.get("delta") or ""
                            if not delta:
                                continue
                            saw_audio_transcript = True
                            text_buf += delta
                            _note_text(delta)
                            _log(
                                f"nvcf[{agent}] audio_tx Δ={delta!r} "
                                f"turn={_clip(turn_spoken)!r}"
                            )
                            await _commit_agent(why="audio_transcript.delta")
                            await _maybe_hard_tools(agent, vc, text_buf, prefix="tc")
                            if (
                                "<TOOLCALL>" in text_buf.upper()
                                and "</TOOLCALL>" in text_buf.upper()
                            ):
                                text_buf = ""

                        elif etype == "response.output_text.delta":
                            if not is_active or saw_audio_transcript:
                                continue
                            delta = event.get("delta") or ""
                            if not delta:
                                continue
                            text_buf += delta
                            _note_text(delta)
                            _log(
                                f"nvcf[{agent}] text_tx Δ={delta!r} "
                                f"(no audio_tx yet) turn={_clip(turn_spoken)!r}"
                            )
                            await _commit_agent(why="text.delta")
                            await _maybe_hard_tools(agent, vc, turn_spoken, prefix="inf")

                        elif etype == "response.output_audio_transcript.done":
                            if is_active:
                                tr = (event.get("transcript") or "").strip()
                                _log(
                                    f"nvcf[{agent}] transcript.done={_clip(tr)!r} "
                                    f"turn={_clip(turn_spoken)!r}"
                                )
                                if tr:
                                    if tr not in spoken:
                                        pad = " " + tr
                                        spoken = (spoken + pad).strip()
                                        turn_spoken = (turn_spoken + pad).strip()
                                        turns.note_agent_text(pad)
                                    _remember_spoken()
                                await _maybe_tools(
                                    agent,
                                    vc,
                                    text_buf or turn_spoken or tr or spoken,
                                    prefix="tr",
                                )
                                text_buf = ""

                        elif etype == "response.function_call_arguments.done":
                            _log(
                                f"nvcf[{agent}] FC.done name={event.get('name')} "
                                f"args={_clip(str(event.get('arguments') or ''), 120)}"
                            )
                            await _dispatch_tool(
                                name=event.get("name", ""),
                                arguments=event.get("arguments") or "{}",
                                call_id=event.get("call_id") or _eid(),
                                outgoing=agent,
                                outgoing_ws=vc,
                                source="nvcf_fc",
                            )

                        elif etype == "error":
                            _log(f"nvcf[{agent}] ERROR {event}")
                        elif etype == "session.end":
                            _log(
                                f"nvcf[{agent}] session.end stats={event.get('stats')}"
                            )
                            if is_active:
                                end.set()
                                break
                        elif etype in {
                            "response.created",
                            "conversation.item.created",
                            "input_audio_buffer.speech_started",
                            "input_audio_buffer.speech_stopped",
                            "input_audio_buffer.committed",
                            "response.output_item.added",
                            "response.output_item.done",
                        }:
                            _log(
                                f"nvcf[{agent}] {etype} active={is_active} "
                                f"{_clip(json.dumps(event), 180)}"
                            )
                        else:
                            _log(
                                f"nvcf[{agent}] event={etype} active={is_active}",
                                every_ms=500,
                                bucket=rate.setdefault(f"ev_{etype}", {}),
                            )
                finally:
                    _log(
                        f"nvcf[{agent}] outbound EXIT active={state['agent']==agent} "
                        f"spoken={_clip(spoken)!r}"
                    )
                    if state["agent"] == agent:
                        await turns.end_agent(why="outbound_exit")
                        end.set()

            pump = asyncio.create_task(duplex_pump())
            tasks = [asyncio.create_task(inbound())] + [
                asyncio.create_task(outbound_agent(a)) for a in sessions
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            end.set()
            await turns.close()
            pump.cancel()
            for t in pending:
                t.cancel()
            await asyncio.gather(pump, *pending, return_exceptions=True)
            _log(
                f"CALL END sim={sim_id} active={state['agent']} "
                f"last_spoken={_clip(str(ctl.get('last_spoken') or ''))!r} "
                f"tools_seen={sorted(handled_tools)}"
            )
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if isinstance(exc, BaseException) and not type(exc).__name__.startswith(
                    "ConnectionClosed"
                ):
                    _log(f"CALL task error: {type(exc).__name__}: {exc}")
                    raise exc
    finally:
        for cm in reversed(session_cms):
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)


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


def main(model: str | None = None) -> None:
    _ = model
    p = argparse.ArgumentParser()
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8765")))
    p.add_argument("--model", default=MODEL)
    a = p.parse_args()
    industry_path(a.industry)
    print(
        f"ws↔VoiceChat {a.model} × {a.industry} chirp=:{a.port} "
        f"upstream={ws_url()} speaks_first={speaks_first()} auth={bool(_auth())} "
        f"VOICECHAT_LOG={_LOG_LEVEL}",
        flush=True,
    )

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
