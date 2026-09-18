"""Gemini 3.8 Live — LiveKit SIP worker.

Copied from flash-live-3.1; only the model id and agent name differ. 3.8
docs say client_content generates again, but the realtime-text kick still
works and is the proven speak-first path, so it stays.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from google.genai import types as genai_types  # noqa: E402
from livekit.agents import AgentSession  # noqa: E402
from livekit.plugins import google as lk_google  # noqa: E402

import harness  # noqa: E402

MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.8-live")
AGENT_NAME = "mivas-gemini-3-8-live"

# Seconds to wait for extended to answer a caller turn before forcing one.
# 0 disables. Tuned above normal thinking latency so it only catches stalls.
_ANSWER_WATCHDOG_S = float(os.environ.get("GEMINI_ANSWER_WATCHDOG_S", "6"))
# Consecutive forced turns before the watchdog stops; a call that has really
# ended should be allowed to end.
_WATCHDOG_MAX_FORCES = int(os.environ.get("GEMINI_WATCHDOG_MAX_FORCES", "3"))

_orig_dispatch = harness._dispatch


async def _shielded_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    # agents-core cancels in-flight tool tasks on barge-in (cancel_and_wait on the
    # exe task): a call the model already committed then never reaches the tool
    # server and records no actual. Shield the dispatch and, if cancelled, finish
    # it anyway — the result is committed via the finished-despite-interruption path.
    import asyncio

    t = asyncio.ensure_future(_orig_dispatch(name, args))
    try:
        return await asyncio.shield(t)
    except asyncio.CancelledError:
        return await t


harness._dispatch = _shielded_dispatch


if "extended" in MODEL and os.environ.get("GEMINI_KEEP_TURN_OPEN", "1").strip() != "0":
    # Extended thinking sends generation_complete and turn_complete while it is
    # still producing, flagging that with interaction_status=IN_PROGRESS ("more
    # output may follow"). The plugin has no notion of interaction_status:
    # generation_complete closes the audio stream and turn_complete ends the
    # generation, so everything after the first word is discarded — the caller
    # hears "I" and then nothing, and hangs up at the 120 s dead-air limit.
    # Suppress both while IN_PROGRESS; the IDLE that follows closes the turn.
    from livekit.plugins.google.realtime import realtime_api as _lk_rt  # noqa: E402

    _orig_server_content = _lk_rt.RealtimeSession._handle_server_content

    def _server_content_keep_turn_open(self: Any, server_content: Any) -> Any:
        status = str(
            getattr(getattr(server_content, "interaction_status", None), "value",
                    getattr(server_content, "interaction_status", None)) or ""
        ).upper()
        if status == "IN_PROGRESS":
            # generation_complete closes the audio stream mid-sentence, so the
            # caller hears "I" and then nothing. Suppress only that. turn_complete
            # must still be honoured: suppressing it strands the generation open,
            # and while one is open the caller's next utterance never starts a
            # turn — 11 of 13 residual stalls were exactly that.
            server_content.generation_complete = None
        return _orig_server_content(self, server_content)

    _lk_rt.RealtimeSession._handle_server_content = _server_content_keep_turn_open

    _orig_send_event = _lk_rt.RealtimeSession._send_client_event

    def _send_and_force_turn(self: Any, event: Any) -> Any:
        # Extended rejects FunctionResponseScheduling (1007), so every tool response
        # falls back to WHEN_IDLE and Gemini sits on it until the input stream goes
        # quiet — which a SIP call never does. The response is delivered; only the
        # turn is missing. Send an empty completed turn straight after it so the
        # model acts on the result it already has instead of waiting for silence.
        out = _orig_send_event(self, event)
        if isinstance(event, genai_types.LiveClientToolResponse) and event.function_responses:
            _orig_send_event(self, genai_types.LiveClientContent(turns=[], turn_complete=True))
        return out

    _lk_rt.RealtimeSession._send_client_event = _send_and_force_turn


def _llm(instructions: str) -> Any:
    # extended-thinking rejects FunctionResponse.scheduling outright: the first tool
    # response closes the socket with 1007 "Function response scheduling is not
    # supported for this model", killing every tool after the first. It also refuses
    # to connect without a thinking_level (1007 "Thinking level must be specified").
    if "extended" in MODEL:
        scheduling_kw: dict[str, Any] = {
            "thinking_config": genai_types.ThinkingConfig(
                thinking_level=genai_types.ThinkingLevel(
                    os.environ.get("GEMINI_THINKING_LEVEL", "LOW")
                )
            )
        }
    else:
        # default WHEN_IDLE stalls on a continuous SIP stream: 3.1 holds the
        # tool response until barge-in "idles" it
        scheduling_kw = {
            "tool_response_scheduling": genai_types.FunctionResponseScheduling.INTERRUPT
        }
    return lk_google.realtime.RealtimeModel(
        model=MODEL,
        voice="Puck",
        language="en-US",
        instructions=instructions,
        **scheduling_kw,
        # default end-of-turn VAD misses short confirmations on telephone
        # audio: model sits silent until the caller speaks again (30-60s)
        realtime_input_config=genai_types.RealtimeInputConfig(
            automatic_activity_detection=genai_types.AutomaticActivityDetection(
                # quiet telephone-band onsets miss START detection entirely:
                # the turn never opens, the model never replies, and only a
                # louder re-ask ("are you still there?") revives it
                start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=500,
            )
        ),
    )


def build_session(_bp: dict[str, Any]) -> AgentSession:
    session = AgentSession(max_tool_steps=16)
    # Required for extended, not optional: without it the agent goes silent after
    # every tool call and the caller waits ~120 s before hanging up (measured).
    if "extended" in MODEL and os.environ.get("GEMINI_KICK_AFTER_TOOLS", "1").strip() != "0":
        # extended rejects FunctionResponse.scheduling (1007), so tool results default to
        # WHEN_IDLE — and a continuous SIP stream never idles: the agent sits silent after
        # a tool until the caller gives up. Nudge speech after each non-handoff tool.
        # Handoffs already kick via Stage.on_enter; end_call must stay silent.
        @session.on("function_tools_executed")
        def _kick_after_tools(ev: Any) -> None:
            # Transport-level forcing (see _send_and_force_turn) handles the normal
            # case. This stays as a backstop for batches whose response never
            # triggered a turn, and is skipped for handoffs (the new stage speaks)
            # and end_call (which must stay silent).
            names = [c.name for c in getattr(ev, "function_calls", []) or []]
            if not names or any(n.startswith("transfer_to") or n == "end_call" for n in names):
                return
            if os.environ.get("GEMINI_KICK_AFTER_TOOLS", "1").strip() == "0":
                return
            harness.kick(session, "Tell the caller that result now, in English.")

    if "extended" in MODEL and _ANSWER_WATCHDOG_S > 0:
        # Residual stalls are all "caller spoke, agent never answered": extended
        # sometimes takes no turn at all after an utterance, and the caller sits in
        # silence until it gives up. If no speech starts within the window, force
        # one. Cancelled the moment the agent does start speaking, so a normally
        # answered turn never sees it.
        import asyncio
        import time as _time

        state: dict[str, Any] = {"last": _time.monotonic(), "forced": 0, "task": None}

        def _touch(*_: Any) -> None:
            state["last"] = _time.monotonic()
            state["forced"] = 0

        async def _watch() -> None:
            # A silence watchdog rather than a per-utterance timer: extended also
            # stalls after its own turn and after a handoff, where no caller
            # transcript arrives to arm a one-shot. Poll, and force a turn each
            # time the line has been quiet too long, a few times before giving up
            # so a genuinely finished call is not kept alive forever.
            while True:
                await asyncio.sleep(1.0)
                quiet = _time.monotonic() - state["last"]
                if quiet < _ANSWER_WATCHDOG_S or state["forced"] >= _WATCHDOG_MAX_FORCES:
                    continue
                state["last"] = _time.monotonic()
                state["forced"] += 1
                try:
                    session.generate_reply(
                        instructions="Continue the call now, in English: answer the"
                        " caller's last message or report the result you just looked up."
                    )
                except Exception:
                    pass

        for _ev in ("user_input_transcribed", "speech_created", "conversation_item_added"):
            session.on(_ev, _touch)

        @session.on("agent_state_changed")
        def _on_state(ev: Any) -> None:
            _touch()
            if state["task"] is None:
                state["task"] = asyncio.create_task(_watch())
    return session


if __name__ == "__main__":
    if "--check" in sys.argv:
        industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
        start, agents = harness.build_agents(os.environ.get("INDUSTRY_DIR") or industry)
        print(f"ok {MODEL} start={start} agents={agents}")
    else:
        bp = harness.load_blueprint()
        greet_text = harness.greeting(bp)

        def make_llm(name: str) -> Any:
            inst = harness.with_clock(bp["agents"][name]["instructions"], bp.get("industry_dir"))
            if name == bp["start"]:
                inst = harness.speak_first(inst, greet_text)
            return _llm(inst)

        harness.serve(
            AGENT_NAME,
            build_session=build_session,
            make_llm=make_llm,
            model=MODEL,
            greet="kick",
            scripted=True,
        )
