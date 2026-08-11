"""MIVAS LiveKit runtime — Gemini `gemini-3.1-flash-live-preview` (speech-to-speech).

Two constraints the plugin imposes on any "3.1" Live model
(`livekit/plugins/google/realtime/realtime_api.py`: `mutable = "3.1" not in model`):

* `mutable_instructions` / `mutable_chat_context` / `mutable_tools` are False, so
  the system prompt MUST go to the `RealtimeModel` constructor and the agent may
  not be swapped mid-session — this runtime uses `harness.Combined`
  (one agent, both blueprint prompts, soft handoff) instead of a real Agent handoff.
* `generate_reply()` is rejected, so the agent cannot open the call from the model.
  A TTS is attached purely so `session.say()` can deliver the scripted greeting;
  every later turn is native Gemini audio.

    python gemini-flash-live-3.1/agent.py dev
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from livekit.agents import AgentSession  # noqa: E402
from livekit.plugins import elevenlabs  # noqa: E402
from livekit.plugins import google as lk_google  # noqa: E402

import harness  # noqa: E402

AGENT_NAME = "mivas-livekit-gemini-live"
MODEL = "gemini-3.1-flash-live-preview"


def build_session(bp):
    return AgentSession(
        llm=lk_google.realtime.RealtimeModel(
            model=MODEL,
            voice="Puck",
            language="en-US",
            instructions=harness.combined_instructions(bp),
        ),
        tts=elevenlabs.TTS(model="eleven_flash_v2_5", voice_id="21m00Tcm4TlvDq8ikWAM"),
        max_tool_steps=8,
    )


if __name__ == "__main__":
    harness.serve(
        AGENT_NAME,
        build_session=build_session,
        build_agent=lambda bp, hangup: harness.Combined(bp, hangup),
        model=MODEL,
        greet="say",
    )
