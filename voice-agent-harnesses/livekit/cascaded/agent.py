"""MIVAS LiveKit runtime — cascaded: Deepgram Flux + GPT-4.1 + ElevenLabs Flash v2.5.

Same STT/LLM/TTS as the Vapi/Cartesia cascaded harnesses, so the stack is the
control variable and the framework is what changes.

    python cascaded/agent.py dev      # local worker, picks up Bluejay dispatch
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from livekit.agents import AgentSession, TurnHandlingOptions  # noqa: E402
from livekit.plugins import deepgram, elevenlabs, openai, silero  # noqa: E402

import harness  # noqa: E402

AGENT_NAME = "mivas-livekit-cascaded"
MODEL = "gpt-4.1"


def build_session(_bp):
    return AgentSession(
        turn_handling=TurnHandlingOptions(turn_detection="stt"),
        stt=deepgram.STTv2(model="flux-general-en"),
        llm=openai.LLM(model=MODEL),
        tts=elevenlabs.TTS(model="eleven_flash_v2_5", voice_id="21m00Tcm4TlvDq8ikWAM"),
        vad=silero.VAD.load(),
        max_tool_steps=8,
    )


if __name__ == "__main__":
    harness.serve(
        AGENT_NAME,
        build_session=build_session,
        build_agent=lambda bp, hangup: harness.BlueprintAgent(bp, bp["start"], hangup),
        model=MODEL,
    )
