"""Gemini 2.5 Flash Native Audio — LiveKit SIP worker.

2.5 can generate_reply. Tools are still fixed per Live socket, so each
blueprint agent still gets its own model instance.
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

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
AGENT_NAME = "mivas-gemini-2-5-flash-native-audio"


def _llm(instructions: str) -> Any:
    return lk_google.realtime.RealtimeModel(
        model=MODEL,
        voice="Puck",
        language="en-US",
        instructions=instructions,
        # default WHEN_IDLE stalls on a continuous SIP stream (see 3.1 agent)
        tool_response_scheduling=genai_types.FunctionResponseScheduling.INTERRUPT,
        # default end-of-turn VAD misses short confirmations on telephone
        # audio: model sits silent until the caller speaks again (30-60s).
        # the 1007 session kills initially blamed on this occur without it
        # too and are handled by the text-only chat sync patch in harness.py
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
            inst = harness.with_clock(bp["agents"][name]["instructions"])
            if name == bp["start"]:
                inst = harness.speak_first(inst, greet_text)
            return _llm(inst)

        harness.serve(
            AGENT_NAME,
            build_session=build_session,
            make_llm=make_llm,
            model=MODEL,
            greet="generate_reply",
            scripted=False,
        )
