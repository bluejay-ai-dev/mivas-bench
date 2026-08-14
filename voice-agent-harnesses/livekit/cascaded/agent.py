"""MIVAS LiveKit runtime — cascaded: Deepgram Flux + GPT-4.1 + ElevenLabs Flash v2.5.

Same STT/LLM/TTS as the Vapi/Cartesia cascaded harnesses, so the stack is the
control variable and the framework is what changes.

Flux owns turn boundaries (`turn_detection="stt"`). Eager EOT and preemptive
generation are off: 0.4 eager + preemptive LLM was starting TTS while the
caller was still talking, then VAD barge-in cut the line mid-sentence
(Alice 728130: "Your current", "on the"). Interruptions need a few real
words, not a backchannel. The extra session endpointing delay is a short
buffer after Flux EndOfTurn, not the old 0.5s pad.

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
        turn_handling=TurnHandlingOptions(
            turn_detection="stt",
            # In STT mode min_delay is *added* after the provider EOT. A tiny
            # buffer absorbs Flux jitter without the old 0.5s latency pad.
            endpointing={"mode": "fixed", "min_delay": 0.25, "max_delay": 3.0},
            preemptive_generation={"enabled": False},
            interruption={
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.8,
                "min_words": 3,
                "resume_false_interruption": True,
            },
        ),
        stt=deepgram.STTv2(
            model="flux-general-en",
            eot_threshold=0.8,
        ),
        llm=openai.LLM(model=MODEL),
        tts=elevenlabs.TTS(model="eleven_flash_v2_5", voice_id="21m00Tcm4TlvDq8ikWAM"),
        vad=silero.VAD.load(min_speech_duration=0.3),
        # healthcare reception can chain classify + locations + transfer in one
        # turn; 8 was the old cap and sat on the default-3 LiveKit limit.
        max_tool_steps=16,
    )


if __name__ == "__main__":
    harness.serve(
        AGENT_NAME,
        build_session=build_session,
        build_agent=lambda bp, hangup: harness.BlueprintAgent(bp, bp["start"], hangup),
        model=MODEL,
        greet="say",
    )
