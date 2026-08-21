"""Gemini 3.1 Flash Live — LiveKit SIP worker.

3.1 ignores LiveKit generate_reply and treats client_content as history
only. Greeting is in the connect-time instructions; a realtime text input
kick makes Gemini speak first.
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

MODEL = "gemini-3.1-flash-live-preview"
AGENT_NAME = "mivas-gemini-flash-live"


def _llm(instructions: str) -> Any:
    return lk_google.realtime.RealtimeModel(
        model=MODEL,
        voice="Puck",
        language="en-US",
        instructions=instructions,
        # default WHEN_IDLE stalls on a continuous SIP stream: 3.1 holds the
        # tool response until barge-in "idles" it
        tool_response_scheduling=genai_types.FunctionResponseScheduling.INTERRUPT,
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
            greet="kick",
            scripted=True,
        )
