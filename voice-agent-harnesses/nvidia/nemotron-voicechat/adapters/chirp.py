"""CHIRP (16 kHz pcm) ↔ one Nemotron VoiceChat Realtime session (24 kHz).

Root multi-agent model (not dual cold sockets):
  One VoiceChat WS for the call. Handoff is `session.update` to the target
  agent's pack + tools on the SAME socket so conversation history stays.
  A cold second session + `response.create` is what caused mid-call
  "Hello, how can I help?" — we never do that.

Speak-first (call open only): short speech-shaped kick + trail silence.
VoiceChat is full-duplex — keep feeding silence while we expect agent audio.
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
from harness import industry_path, load_blueprint, set_call_id  # noqa: E402
from report import end_speech_span, start_speech_span, traced_run  # noqa: E402
from voicechat import (  # noqa: E402
    MODEL,
    SAMPLE_RATE,
    connect_voicechat,
    handle_function_call,
    handoff_nudge_event,
    handoff_role,
    infer_tool_calls,
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

AGENT_RMS_ON = int(os.environ.get("VOICECHAT_AGENT_RMS_ON", "500"))
AGENT_SILENCE_S = float(os.environ.get("VOICECHAT_AGENT_SILENCE_S", "0.55"))
AGENT_MIN_LOUD_MS = int(os.environ.get("VOICECHAT_AGENT_MIN_LOUD_MS", "120"))
AGENT_LOUD_GAP_S = float(os.environ.get("VOICECHAT_AGENT_LOUD_GAP_S", "0.25"))
USER_RMS_ON = int(os.environ.get("VOICECHAT_USER_RMS_ON", "350"))
USER_LIVE_S = float(os.environ.get("VOICECHAT_USER_LIVE_S", "0.25"))
CUSTOMER_HANG_S = float(os.environ.get("VOICECHAT_CUSTOMER_HANG_S", "2.0"))
AGENT_HANG_S = float(os.environ.get("VOICECHAT_AGENT_HANG_S", "2.2"))
KICK_TRAIL_SILENCE_S = float(os.environ.get("VOICECHAT_KICK_TRAIL_SILENCE_S", "3.5"))
ECHO_SUPPRESS_S = float(os.environ.get("VOICECHAT_ECHO_SUPPRESS_S", "1.5"))
_KICK_WAV = Path(__file__).resolve().parents[1] / "assets" / "speak_first_kick.wav"

_LOG_LEVEL = (os.environ.get("VOICECHAT_LOG") or "full").strip().lower()
_LOG_ON = _LOG_LEVEL not in {"0", "off", "false", "no"}
_LOG_AUDIO = _LOG_LEVEL in {"audio", "pcm", "all"}
_call_t0: float | None = None


def _ms() -> int:
    if _call_t0 is None:
        return 0
    return int((time.monotonic() - _call_t0) * 1000)


def _log(msg: str, *, every_ms: int | None = None, bucket: dict | None = None) -> None:
    if not _LOG_ON:
        return
    if every_ms is not None and bucket is not None:
        now = time.monotonic()
        if now - float(bucket.get("t") or 0.0) < every_ms / 1000.0:
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
    """Exclusive agent.speech / customer.speech under voice.call."""

    def __init__(self, ws, root) -> None:
        self.ws = ws
        self.root = root
        self.agent_utt: str | None = None
        self.agent_span = None
        self.agent_text: list[str] = []
        self.customer_utt: str | None = None
        self.customer_span = None
        self._customer_hang: asyncio.Task | None = None
        self._agent_hang: asyncio.Task | None = None

    def _cancel_customer_hang(self) -> None:
        if self._customer_hang is not None:
            self._customer_hang.cancel()
            self._customer_hang = None

    def _cancel_agent_hang(self) -> None:
        if self._agent_hang is not None:
            self._agent_hang.cancel()
            self._agent_hang = None

    def agent_has_text(self) -> bool:
        return bool("".join(self.agent_text).strip())

    async def start_agent(self, *, why: str = "") -> None:
        self._cancel_agent_hang()
        if self.agent_utt is not None:
            return
        await self.end_customer(why=f"agent_start:{why}")
        self.agent_utt = f"u_{uuid.uuid4().hex[:12]}"
        prior = "".join(self.agent_text).strip()
        self.agent_span = start_speech_span(
            self.agent_utt, speaker="agent", parent=self.root
        )
        await self.ws.send(_event("speech.started", {"utterance_id": self.agent_utt}))
        _log(f"agent.speech START utt={self.agent_utt} why={why} prior={_clip(prior, 60)!r}")

    async def note_agent_quiet(self, *, why: str = "") -> None:
        if self.agent_utt is None:
            return
        self._cancel_agent_hang()

        async def _hang() -> None:
            try:
                await asyncio.sleep(AGENT_HANG_S)
                await self.end_agent(why=f"hang_expired:{why}")
            except asyncio.CancelledError:
                return

        self._agent_hang = asyncio.create_task(_hang())
        _log(f"agent.hang arm {AGENT_HANG_S}s why={why}")

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
            await self.ws.send(_event("speech.completed", {"utterance_id": utt}))
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
        self._cancel_customer_hang()
        if self.customer_utt is not None:
            _log(f"customer.speech COALESCE keep={self.customer_utt} new={uid}")
            return
        self.customer_utt = uid
        self.customer_span = start_speech_span(uid, speaker="customer", parent=self.root)
        _log(f"customer.speech START utt={uid} why={why}")

    async def note_customer_completed(self, *, why: str = "") -> None:
        if self.customer_utt is None:
            return
        self._cancel_customer_hang()

        async def _hang() -> None:
            try:
                await asyncio.sleep(CUSTOMER_HANG_S)
                await self.end_customer(why=f"hang_expired:{why}")
            except asyncio.CancelledError:
                return

        self._customer_hang = asyncio.create_task(_hang())

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
    _log(f"voicechat[{agent}] {json.loads(raw).get('type')}")
    await vc_ws.send(json.dumps(session_update_for_agent(bp, agent)))
    while True:
        raw = await asyncio.wait_for(vc_ws.recv(), timeout=60)
        ev = json.loads(raw)
        et = ev.get("type")
        _log(f"voicechat[{agent}] {et}")
        if et == "session.updated":
            n = len((ev.get("session") or {}).get("tools") or [])
            _log(f"voicechat[{agent}] tools_registered={n}")
            return
        if et == "error":
            raise RuntimeError(f"session.update failed for {agent}: {ev}")


async def _bridge(ws, industry: str) -> None:
    global _call_t0
    _call_t0 = time.monotonic()

    bp = load_blueprint(industry)
    state = {"agent": bp["start"]}
    end = asyncio.Event()
    industry_dir = industry_path(industry)
    workflow = f"mivas-{Path(industry_dir).name}-{MODEL}"
    sim_id = _simulation_result_id(ws)
    set_call_id(sim_id)
    speak_first = speaks_first()
    kick_pcm = _load_kick_pcm() if speak_first else b""

    _log(
        f"CALL START sim={sim_id} mode=single-session agents={list(bp['agents'])} "
        f"start={bp['start']} speaks_first={speak_first}"
    )

    cm = connect_voicechat()
    vc = None
    try:
        async with traced_run(
            workflow, simulation_result_id=sim_id, model=MODEL
        ) as otel_root:
            state["_otel_root"] = otel_root
            vc = await cm.__aenter__()
            await _configure_session(vc, bp["start"], bp)
            turns = _Turns(ws, otel_root)

            ctl = {
                "last_user_loud": 0.0,
                "last_agent_loud": 0.0,
                "agent_heard": False,
                "awaiting_agent": speak_first,
                "customer_speaking": False,
                "kick_generation": 0,
                "call_open_kick": False,
                "last_spoken": "",
                "reconfiguring": False,
            }
            rate: dict[str, dict] = {}
            deferred_tools: list[dict[str, Any]] = []
            pending_user_pcm: list[bytes] = []
            agent_tools = {
                a: session_update_for_agent(bp, a)["session"]["tools"] for a in bp["agents"]
            }
            handled_tools: set[str] = set()

            def _user_live(now: float | None = None) -> bool:
                now = now if now is not None else time.monotonic()
                if ctl["customer_speaking"]:
                    return True
                return bool(ctl["last_user_loud"]) and (
                    now - ctl["last_user_loud"] < USER_LIVE_S
                )

            def _agent_echo_risk(now: float | None = None) -> bool:
                now = now if now is not None else time.monotonic()
                if turns.agent_utt is not None:
                    return True
                return bool(ctl["agent_heard"]) and (
                    now - float(ctl["last_agent_loud"] or 0.0) < ECHO_SUPPRESS_S
                )

            def _real_barge_in(now: float | None = None) -> bool:
                """User is actually talking — not Bluejay VAD hearing our TTS."""
                now = now if now is not None else time.monotonic()
                if _agent_echo_risk(now):
                    return False
                return _user_live(now)

            async def _send_pcm24(pcm24: bytes) -> None:
                if not pcm24 or end.is_set():
                    return
                if ctl["reconfiguring"]:
                    # Keep caller audio across session.update — dropping it loses barge-in.
                    pending_user_pcm.append(pcm24)
                    return
                await vc.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "event_id": _eid(),
                            "audio": base64.b64encode(pcm24).decode("ascii"),
                        }
                    )
                )

            async def _flush_pending_user_pcm() -> None:
                if not pending_user_pcm or end.is_set() or ctl["reconfiguring"]:
                    return
                chunks = list(pending_user_pcm)
                pending_user_pcm.clear()
                for pcm24 in chunks:
                    if end.is_set() or ctl["reconfiguring"]:
                        pending_user_pcm.append(pcm24)
                        break
                    await vc.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "event_id": _eid(),
                                "audio": base64.b64encode(pcm24).decode("ascii"),
                            }
                        )
                    )

            async def _send_silence_chunk() -> None:
                if end.is_set() or _user_live() or ctl["reconfiguring"]:
                    return
                await vc.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "event_id": _eid(),
                            "audio": SILENCE_B64,
                        }
                    )
                )

            async def speak_first_kick() -> None:
                gen = ctl["kick_generation"]
                try:
                    _log(f"speak_first_kick BEGIN bytes={len(kick_pcm)}")
                    ctl["awaiting_agent"] = True
                    ctl["call_open_kick"] = True
                    ctl["agent_heard"] = False
                    step = SILENCE_BYTES
                    for i in range(0, len(kick_pcm), step):
                        if end.is_set() or ctl["kick_generation"] != gen or _user_live():
                            return
                        chunk = kick_pcm[i : i + step]
                        if len(chunk) < step:
                            chunk = chunk + b"\x00" * (step - len(chunk))
                        await _send_pcm24(chunk)
                        await asyncio.sleep(SILENCE_CHUNK_MS / 1000.0)
                    t0 = time.monotonic()
                    while not end.is_set() and ctl["kick_generation"] == gen:
                        if _user_live():
                            return
                        now = time.monotonic()
                        if ctl["agent_heard"] and (
                            now - ctl["last_agent_loud"] > AGENT_SILENCE_S
                        ):
                            ctl["awaiting_agent"] = False
                            _log("speak_first_kick DONE")
                            return
                        if now - t0 > KICK_TRAIL_SILENCE_S:
                            ctl["awaiting_agent"] = False
                            _log(f"speak_first_kick TIMEOUT heard={ctl['agent_heard']}")
                            return
                        await _send_silence_chunk()
                        await asyncio.sleep(SILENCE_CHUNK_MS / 1000.0)
                except Exception as e:
                    _log(f"speak_first_kick ERROR {type(e).__name__}: {e}")
                finally:
                    ctl["call_open_kick"] = False

            async def duplex_pump() -> None:
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
                        if agent_speaking or ctl["awaiting_agent"] or turns.agent_utt:
                            try:
                                await _send_silence_chunk()
                            except Exception as e:
                                if end.is_set():
                                    return
                                _log(f"duplex_pump ERROR {type(e).__name__}: {e}")
                                return
                except Exception as e:
                    _log(f"duplex_pump FATAL {type(e).__name__}: {e}")

            async def _handoff_same_session(target: str) -> None:
                """Root handoff: reconfigure THIS socket — keep call history."""
                gen = ctl["kick_generation"]
                try:
                    _log(f"handoff session.update → {target}")
                    ctl["reconfiguring"] = True
                    ctl["awaiting_agent"] = True
                    ctl["agent_heard"] = False
                    await vc.send(json.dumps(session_update_for_agent(bp, target)))
                    # Wait for session.updated (also handled in outbound).
                    deadline = time.monotonic() + 8.0
                    while time.monotonic() < deadline and not end.is_set():
                        if not ctl["reconfiguring"]:
                            break
                        await asyncio.sleep(0.05)
                    if ctl["reconfiguring"]:
                        ctl["reconfiguring"] = False
                        await _flush_pending_user_pcm()
                        _log(f"handoff FAILED no session.updated → {target}")
                        return
                    await _flush_pending_user_pcm()
                    # Nudge only after DH is quiet so we don't talk over them.
                    wait_t0 = time.monotonic()
                    while not end.is_set() and ctl["kick_generation"] == gen:
                        if not ctl["customer_speaking"] and not _user_live():
                            break
                        if time.monotonic() - wait_t0 > 8.0:
                            break
                        await asyncio.sleep(0.05)
                    if end.is_set() or ctl["kick_generation"] != gen:
                        return
                    _log(f"handoff response.create → {target}")
                    await vc.send(json.dumps(handoff_nudge_event()))
                    t0 = time.monotonic()
                    while not end.is_set() and ctl["kick_generation"] == gen:
                        if ctl["customer_speaking"]:
                            return
                        now = time.monotonic()
                        if ctl["agent_heard"] and (
                            now - ctl["last_agent_loud"] > AGENT_SILENCE_S
                        ):
                            ctl["awaiting_agent"] = False
                            _log("handoff continue DONE")
                            return
                        if now - t0 > 3.0:
                            ctl["awaiting_agent"] = False
                            _log("handoff continue TIMEOUT")
                            return
                        await _send_silence_chunk()
                        await asyncio.sleep(SILENCE_CHUNK_MS / 1000.0)
                except Exception as e:
                    ctl["reconfiguring"] = False
                    with contextlib.suppress(Exception):
                        await _flush_pending_user_pcm()
                    _log(f"handoff ERROR {type(e).__name__}: {e}")

            async def _close_call() -> None:
                await asyncio.sleep(END_CALL_CLOSE_DELAY_S)
                end.set()
                with contextlib.suppress(Exception):
                    await vc.send(
                        json.dumps({"type": "session.close", "event_id": _eid()})
                    )
                with contextlib.suppress(Exception):
                    await vc.close()
                with contextlib.suppress(Exception):
                    await ws.close(1000)

            async def _dispatch_tool(
                *,
                name: str,
                arguments: str | dict,
                call_id: str,
                source: str = "",
            ) -> None:
                if not name:
                    return
                args_key = (
                    json.dumps(arguments, sort_keys=True, separators=(",", ":"))
                    if isinstance(arguments, dict)
                    else str(arguments)
                )
                # Dedup by tool name only — agent flips on handoff before a native
                # retry can arrive, so `{agent}:{name}` would miss the duplicate.
                key = name
                if key in handled_tools:
                    _log(f"tool DEDUP skip {name} source={source}")
                    return
                if ctl["customer_speaking"] and name == "schedule_appointment":
                    _log(f"tool DEFER {name} source={source} args={_clip(args_key, 80)}")
                    deferred_tools.append(
                        {
                            "name": name,
                            "arguments": arguments,
                            "call_id": call_id,
                            "source": f"flush:{source}",
                        }
                    )
                    return
                outgoing = state["agent"]
                _log(f"tool DISPATCH {name} source={source} args={_clip(args_key)}")
                await turns.end_agent(why=f"tool:{name}")
                result, stop, reply = await handle_function_call(
                    name, arguments, call_id, bp, state
                )
                if result.get("success"):
                    handled_tools.add(key)
                _log(
                    f"tool RESULT {name} success={result.get('success')} "
                    f"active={state['agent']} stop={stop}"
                )
                with contextlib.suppress(Exception):
                    await vc.send(json.dumps(reply))
                role = handoff_role(result, bp)
                if role and role != outgoing:
                    _log(f"HANDOFF {outgoing} → {role} (same session)")
                    ctl["kick_generation"] += 1
                    asyncio.create_task(_handoff_same_session(role))
                if stop:
                    asyncio.create_task(_close_call())

            async def _flush_deferred() -> None:
                if not deferred_tools or ctl["customer_speaking"]:
                    return
                pending = list(deferred_tools)
                deferred_tools.clear()
                _log(f"tool FLUSH n={len(pending)}")
                for item in pending:
                    await _dispatch_tool(**item)

            async def _maybe_tools(blob: str, *, prefix: str) -> None:
                agent = state["agent"]
                calls = infer_tool_calls(agent_tools[agent], blob)
                if not calls:
                    return
                _log(f"infer HIT prefix={prefix} calls={calls} blob={_clip(blob)!r}")
                for call in calls:
                    await _dispatch_tool(
                        name=call["name"],
                        arguments=call["arguments"],
                        call_id=f"{prefix}_{agent}_{call['name']}",
                        source=f"infer:{prefix}",
                    )

            async def _maybe_hard_tools(blob: str, *, prefix: str) -> None:
                calls = parse_toolcalls(blob)
                if not calls:
                    return
                for call in calls:
                    await _dispatch_tool(
                        name=call["name"],
                        arguments=call["arguments"],
                        call_id=f"{prefix}_{state['agent']}_{call['name']}",
                        source=f"hard:{prefix}",
                    )

            async def inbound() -> None:
                up = None
                try:
                    async for msg in ws:
                        if end.is_set():
                            break
                        if isinstance(msg, bytes) and msg:
                            rms = audioop.rms(msg, W)
                            loud = rms >= USER_RMS_ON
                            echo = _agent_echo_risk()
                            if loud and not echo:
                                ctl["last_user_loud"] = time.monotonic()
                            elif loud and echo and rms >= USER_RMS_ON * 2:
                                # Real barge-in over TTS: much louder than typical echo.
                                ctl["last_user_loud"] = time.monotonic()
                                echo = False
                            if echo or not (ctl["customer_speaking"] or loud):
                                continue
                            pcm, up = audioop.ratecv(msg, W, 1, R_CHIRP, R_VC, up)
                            if pcm:
                                ctl["awaiting_agent"] = True
                                await _send_pcm24(pcm)
                                if _LOG_AUDIO:
                                    _log(
                                        f"user→nvcf rms={rms} vad={ctl['customer_speaking']}",
                                        every_ms=500,
                                        bucket=rate.setdefault("user", {}),
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
                            if not _real_barge_in():
                                _log(
                                    f"CHIRP speech.started IGNORED echo "
                                    f"uid={data.get('utterance_id')} "
                                    f"user_live={_user_live()} echo={_agent_echo_risk()}"
                                )
                                continue
                            ctl["customer_speaking"] = True
                            uid = data.get("utterance_id") or f"c_{uuid.uuid4().hex[:12]}"
                            _log(f"CHIRP speech.started uid={uid}")
                            ctl["awaiting_agent"] = True
                            if turns.agent_utt is not None:
                                await turns.end_agent(why="barge_in:user")
                            await turns.start_customer(uid, why="chirp_speech.started")
                        elif etype == "speech.completed":
                            if not ctl["customer_speaking"] and turns.customer_utt is None:
                                continue
                            ctl["customer_speaking"] = False
                            _log(f"CHIRP speech.completed uid={data.get('utterance_id')}")
                            await turns.note_customer_completed(why="chirp_speech.completed")
                            ctl["awaiting_agent"] = True
                            await _flush_deferred()
                finally:
                    ctl["customer_speaking"] = False
                    await turns.end_customer(why="inbound_exit")
                    end.set()

            async def outbound() -> None:
                down = None
                last_loud = 0.0
                loud_ms = 0.0
                text_buf = ""
                spoken = ""
                turn_spoken = ""
                saw_audio_transcript = False

                async def _commit(*, why: str) -> None:
                    if turns.agent_utt is not None or ctl["customer_speaking"]:
                        return
                    await turns.start_agent(why=why)

                def _note(delta: str) -> None:
                    nonlocal spoken, turn_spoken
                    spoken += delta
                    turn_spoken += delta
                    if spoken.strip():
                        ctl["last_spoken"] = spoken.strip()
                    turns.note_agent_text(delta)

                try:
                    async for raw in vc:
                        if end.is_set():
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        now = time.monotonic()

                        if etype == "session.updated":
                            tools = (event.get("session") or {}).get("tools") or []
                            # After handoff reconfigure, mark ready.
                            ctl["reconfiguring"] = False
                            await _flush_pending_user_pcm()
                            _log(
                                f"session.updated agent={state['agent']} "
                                f"tools={len(tools)}"
                            )
                            continue

                        if etype == "response.output_audio.delta":
                            b64 = event.get("delta") or ""
                            if not b64:
                                continue
                            pcm24 = base64.b64decode(b64)
                            pcm16, down = audioop.ratecv(
                                pcm24, W, 1, R_VC, R_CHIRP, down
                            )
                            if not pcm16:
                                continue
                            rms = audioop.rms(pcm16, W)
                            # Never drop agent PCM because Bluejay VAD heard our
                            # own TTS (run 230628: barge_in:chirp chopped every turn).
                            if _real_barge_in(now):
                                loud_ms = 0.0
                                if turns.agent_utt is not None:
                                    await turns.end_agent(why="barge_in:user")
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
                                if (
                                    turns.agent_utt is not None
                                    and last_loud > 0
                                    and now - last_loud > max(AGENT_HANG_S, 3.0)
                                ):
                                    await turns.note_agent_quiet(why="stall_quiet")
                                    ctl["awaiting_agent"] = False

                            await ws.send(pcm16)
                            if turns.agent_utt is None:
                                if turns.agent_has_text() or loud_ms >= AGENT_MIN_LOUD_MS:
                                    await _commit(
                                        why=(
                                            "transcript"
                                            if turns.agent_has_text()
                                            else f"loud_ms={loud_ms:.0f}"
                                        )
                                    )
                            elif loud:
                                turns._cancel_agent_hang()

                        elif etype in {"response.output_audio.done", "response.done"}:
                            _log(
                                f"{etype} turn={_clip(turn_spoken)!r} "
                                f"agent={state['agent']}"
                            )
                            loud_ms = 0.0
                            if etype == "response.done":
                                await turns.note_agent_quiet(why="response.done")
                                ctl["awaiting_agent"] = False
                            blob = turn_spoken.strip()
                            if blob:
                                await _maybe_tools(blob, prefix="done")
                            await _flush_deferred()
                            if etype == "response.done":
                                text_buf = ""
                                turn_spoken = ""
                                saw_audio_transcript = False

                        elif etype == "response.output_audio_transcript.delta":
                            delta = event.get("delta") or ""
                            if not delta:
                                continue
                            saw_audio_transcript = True
                            text_buf += delta
                            _note(delta)
                            _log(f"audio_tx Δ={delta!r} turn={_clip(turn_spoken)!r}")
                            await _commit(why="audio_transcript.delta")
                            await _maybe_hard_tools(text_buf, prefix="tc")
                            if (
                                "<TOOLCALL>" in text_buf.upper()
                                and "</TOOLCALL>" in text_buf.upper()
                            ):
                                text_buf = ""

                        elif etype == "response.output_text.delta":
                            delta = event.get("delta") or ""
                            if not delta:
                                continue
                            # text_buf is the TOOL-CALL channel. The model emits
                            # <TOOLCALL> as text and never speaks it, so it MUST
                            # accumulate even while the audio transcript carries the
                            # spoken words — `if saw_audio_transcript: continue` skipped
                            # this line, leaving text_buf empty for the whole call. Only
                            # the SPOKEN accumulation is suppressed, which is all
                            # "prefer the audio transcript" was ever for.
                            text_buf += delta
                            if not saw_audio_transcript:
                                _note(delta)
                                await _commit(why="text.delta")
                            await _maybe_hard_tools(text_buf, prefix="inf")

                        elif etype == "response.output_audio_transcript.done":
                            tr = (event.get("transcript") or "").strip()
                            _log(f"transcript.done={_clip(tr)!r}")
                            if tr and tr not in spoken:
                                pad = " " + tr
                                spoken = (spoken + pad).strip()
                                turn_spoken = (turn_spoken + pad).strip()
                                turns.note_agent_text(pad)
                                ctl["last_spoken"] = spoken
                            await _maybe_tools(
                                text_buf or turn_spoken or tr, prefix="tr"
                            )
                            text_buf = ""

                        elif etype == "response.function_call_arguments.done":
                            await _dispatch_tool(
                                name=event.get("name", ""),
                                arguments=event.get("arguments") or "{}",
                                call_id=event.get("call_id") or _eid(),
                                source="nvcf_fc",
                            )

                        elif etype == "input_audio_buffer.speech_started":
                            _log(f"event={etype}")
                            if not _agent_echo_risk():
                                ctl["customer_speaking"] = True
                                ctl["last_user_loud"] = time.monotonic()
                                if turns.agent_utt is not None:
                                    await turns.end_agent(why="barge_in:nvcf")
                        elif etype == "error":
                            _log(f"ERROR {event}")
                        elif etype == "session.end":
                            _log(f"session.end {event.get('stats')}")
                            end.set()
                            break
                        elif etype in {
                            "response.created",
                            "conversation.item.created",
                            "input_audio_buffer.speech_stopped",
                        }:
                            _log(f"event={etype}")
                finally:
                    await turns.end_agent(why="outbound_exit")
                    end.set()

            pump = asyncio.create_task(duplex_pump())
            tasks = [
                asyncio.create_task(inbound()),
                asyncio.create_task(outbound()),
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
                f"CALL END sim={sim_id} agent={state['agent']} "
                f"tools={sorted(handled_tools)} "
                f"last_spoken={_clip(str(ctl.get('last_spoken') or ''))!r}"
            )
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
        f"upstream={ws_url()} speaks_first={speaks_first()} "
        f"mode=single-session auth={bool(_auth())} VOICECHAT_LOG={_LOG_LEVEL}",
        flush=True,
    )

    async def run() -> None:
        async with serve(lambda ws: _handler(ws, a.industry), a.host, a.port):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
