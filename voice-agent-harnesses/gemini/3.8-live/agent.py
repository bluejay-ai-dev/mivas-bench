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
    if "extended" in MODEL:
        # extended rejects FunctionResponse.scheduling (1007), so tool results default to
        # WHEN_IDLE — and a continuous SIP stream never idles: the agent sits silent after
        # a tool until the caller gives up. Nudge speech after each non-handoff tool.
        # Handoffs already kick via Stage.on_enter; end_call must stay silent.
        @session.on("function_tools_executed")
        def _kick_after_tools(ev: Any) -> None:
            names = [c.name for c in getattr(ev, "function_calls", [])]
            if not names or any(n.startswith("transfer_to") or n == "end_call" for n in names):
                return
            harness.kick(session, "Now tell the caller the result.")
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
