"""MIVAS LiveKit runtime — Gemini `gemini-3.1-flash-live-preview` (speech-to-speech).

`livekit/plugins/google/realtime/realtime_api.py` sets `mutable = "3.1" not in model`,
so `mutable_instructions` / `mutable_chat_context` are False (and `mutable_tools` is
False for every Gemini Live model). Those flags only forbid *mutating a live session*,
not running two of them, so this runtime does a real agent handoff by giving each
`Agent` its own `RealtimeModel`:

* `AgentActivity._detach_reusable_resources` reuses the realtime session only when
  `self.llm is new_activity.llm`. Two instances => no reuse => `_start_session` calls
  `llm.session()` and logs "created new realtime session for activity".
* the fresh session has no active socket yet, so `update_instructions`/`update_tools`
  land on `_opts`/`_tools` and `_build_connect_config` sends them as the connect-time
  `system_instruction` and `tools` — the target agent's own prompt and *only* that
  agent's tools reach the model.
* `generate_reply()` is still rejected, so the ElevenLabs TTS delivers the two
  scripted lines: the call greeting, and (on handoff) a first line derived from the
  target agent's own prompt (`harness._derive_opener`); every other turn is Gemini
  audio.

    python gemini-flash-live-3.1/agent.py dev
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

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


def _model(instructions: str) -> lk_google.realtime.RealtimeModel:
    return lk_google.realtime.RealtimeModel(
        model=MODEL, voice="Puck", language="en-US", instructions=instructions
    )


def build_session(_bp: dict[str, Any]) -> AgentSession:
    # no session-level llm on purpose: each agent brings its own, which is what makes
    # the handoff open a second Gemini Live session instead of reusing the first.
    return AgentSession(
        tts=elevenlabs.TTS(model="eleven_flash_v2_5", voice_id="21m00Tcm4TlvDq8ikWAM"),
        max_tool_steps=8,
    )


def build_agent(bp: dict[str, Any], hangup: asyncio.Event) -> harness.BlueprintAgent:
    # llm_factory gives every agent its own RealtimeModel, which is what makes a
    # handoff open a second Gemini Live session instead of reusing the first.
    # `scripted_opener=True` because 3.1 rejects generate_reply(): each handoff
    # target derives its own scripted first line from its own prompt (see
    # `harness._derive_opener`) rather than reusing one literal for every target.
    return harness.BlueprintAgent(
        bp,
        bp["start"],
        hangup,
        llm_factory=lambda name: _model(bp["agents"][name]["instructions"]),
        scripted_opener=True,
    )


if __name__ == "__main__":
    if "--check" in sys.argv:
        harness.load_blueprint()
        print("ok")
    else:
        harness.serve(
            AGENT_NAME,
            build_session=build_session,
            build_agent=build_agent,
            model=MODEL,
            greet="say",
        )
