"""Gemini 3.8 Live — LiveKit SIP worker.

Same wiring as flash-live-3.1. The `extended` variant (variants.json) runs
gemini-3.8-live-extended-thinking, which differs on the wire in three ways,
all measured against the API directly (not inferred from docs):

1. It refuses to connect without a thinking_level (1007).
2. It closes the socket on any FunctionResponse.scheduling (1007 "Function
   response scheduling is not supported for this model"), BLOCKING or
   NON_BLOCKING alike. It still answers a tool result on its own.
3. It answers one caller turn with several generations (filler, tool call,
   answer), each closed by turn_complete with interaction_status=IN_PROGRESS
   until the final IDLE.
"""

from __future__ import annotations

import asyncio
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
from livekit.plugins.google.realtime import realtime_api as _lk_rt  # noqa: E402

import harness  # noqa: E402

MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.8-live")
EXTENDED = "extended" in MODEL
AGENT_NAME = "mivas-gemini-3-8-live"

_orig_dispatch = harness._dispatch


async def _shielded_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    # agents-core cancels in-flight tool tasks on barge-in: a call the model
    # already committed then never reaches the tool server and records no
    # actual. Finish the POST regardless.
    t = asyncio.ensure_future(_orig_dispatch(name, args))
    try:
        return await asyncio.shield(t)
    except asyncio.CancelledError:
        return await t


harness._dispatch = _shielded_dispatch


if EXTENDED:
    # The plugin treats every server-initiated generation as a barge-in and
    # cuts the playout of the previous one (_start_new_generation emits
    # input_speech_started). With extended's multi-generation turns the caller
    # hears the first words of each generation and nothing else. Queue them
    # instead; a real barge-in still arrives as server_content.interrupted.
    _orig_start = _lk_rt.RealtimeSession._start_new_generation

    def _start_without_barge_in(self: Any) -> None:
        self._handle_input_speech_started = lambda: None
        try:
            _orig_start(self)
        finally:
            del self._handle_input_speech_started

    _lk_rt.RealtimeSession._start_new_generation = _start_without_barge_in

    # Without scheduling a tool result is consumed WHEN_IDLE, and extended
    # sometimes declares itself idle (turn_complete + interaction_status=IDLE)
    # after a filler without ever speaking the result: measured 55-70 s of
    # silence until the caller asked "are you still there?". An empty
    # completed turn makes it act on the result, but sending one while it is
    # still IN_PROGRESS reads as silence to it ("No speech."). So after a tool
    # response, wait for the model's own IDLE with nothing generating (or,
    # if it never says IDLE, for 20 s with no generation at all), then
    # complete one empty turn.
    _orig_send = _lk_rt.RealtimeSession._send_client_event
    _orig_server_content = _lk_rt.RealtimeSession._handle_server_content

    def _track_status(self: Any, server_content: Any) -> None:
        self._mivas_active_at = asyncio.get_event_loop().time()
        if server_content.turn_complete:
            status = getattr(server_content, "interaction_status", None)
            self._mivas_status = str(getattr(status, "value", status) or "").upper()
            self._mivas_status_at = asyncio.get_event_loop().time()
        _orig_server_content(self, server_content)

    _lk_rt.RealtimeSession._handle_server_content = _track_status

    async def _force_turn_when_idle(self: Any, sent_at: float) -> None:
        deadline = sent_at + float(os.environ.get("GEMINI_FORCE_TURN_MAX_S", "45"))
        while asyncio.get_event_loop().time() < deadline and not self._msg_ch.closed:
            await asyncio.sleep(1.0)
            gen = self._current_generation
            if gen is not None and not gen._done:
                continue
            now = asyncio.get_event_loop().time()
            idle_since = getattr(self, "_mivas_status_at", 0.0)
            quiet = now - max(sent_at, getattr(self, "_mivas_active_at", 0.0))
            said_idle = getattr(self, "_mivas_status", "") == "IDLE" and idle_since > sent_at
            if (said_idle and now - idle_since >= 3.0) or quiet >= 20.0:
                harness.logger.info(
                    "forcing a turn after a tool result: status=%s quiet=%.0fs",
                    getattr(self, "_mivas_status", ""), quiet,
                )
                _orig_send(self, genai_types.LiveClientContent(turns=[], turn_complete=True))
                return

    def _send_and_watch(self: Any, event: Any) -> None:
        if isinstance(event, genai_types.LiveClientToolResponse) and event.function_responses:
            # the plugin declares 3.8 tools NON_BLOCKING and then marks a result
            # that needs no reply SILENT; extended closes the socket on any
            # scheduling value (1007), which ended 9 of 720 k=5 calls mid-call
            for fr in event.function_responses:
                fr.scheduling = None
            _orig_send(self, event)
            asyncio.ensure_future(_force_turn_when_idle(self, asyncio.get_event_loop().time()))
            return
        _orig_send(self, event)

    _lk_rt.RealtimeSession._send_client_event = _send_and_watch


def _llm(instructions: str) -> Any:
    if EXTENDED:
        model_kw: dict[str, Any] = {
            "thinking_config": genai_types.ThinkingConfig(
                thinking_level=genai_types.ThinkingLevel(
                    os.environ.get("GEMINI_THINKING_LEVEL", "LOW")
                )
            )
        }
    else:
        # default WHEN_IDLE holds the tool response until the input stream
        # idles, which a SIP line never does
        model_kw = {
            "tool_response_scheduling": genai_types.FunctionResponseScheduling.INTERRUPT
        }
    return lk_google.realtime.RealtimeModel(
        model=MODEL,
        voice="Puck",
        language="en-US",
        instructions=instructions,
        **model_kw,
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
    return AgentSession(max_tool_steps=16)


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
