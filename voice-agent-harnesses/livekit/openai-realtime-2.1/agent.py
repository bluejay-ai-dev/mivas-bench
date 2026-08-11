"""MIVAS LiveKit runtime — OpenAI Realtime `gpt-realtime-2.1` (speech-to-speech).

    python openai-realtime-2.1/agent.py dev
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from livekit.agents import AgentSession  # noqa: E402
from livekit.plugins import openai  # noqa: E402

import harness  # noqa: E402

AGENT_NAME = "mivas-livekit-openai-realtime"
MODEL = "gpt-realtime-2.1"


def build_session(_bp):
    return AgentSession(
        llm=openai.realtime.RealtimeModel(model=MODEL, voice="marin"),
        max_tool_steps=8,
    )


if __name__ == "__main__":
    harness.serve(
        AGENT_NAME,
        build_session=build_session,
        build_agent=lambda bp, hangup: harness.Receptionist(bp, hangup),
        model=MODEL,
    )
