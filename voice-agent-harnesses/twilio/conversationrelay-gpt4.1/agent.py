"""Twilio ConversationRelay harness — GPT-4.1 × industry agent_blueprint.json.

family/runtime = twilio/conversationrelay-gpt4.1
Guide: https://www.twilio.com/en-us/blog/developers/tutorials/product/ai-agent-conversationrelay-voice-mistral
(same ConversationRelay shape; LLM is OpenAI GPT-4.1 instead of Mistral/Hugging Face)

Soft multi-agent: one chat session; handoff swaps system + tools.
Speak-first: ConversationRelay welcomeGreeting.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (  # noqa: E402
    MODEL,
    RUNTIME,
    TOOL_SERVER_URL,
    advertised_tools,
    build_agents,
    demo,
    industry_path,
    load_blueprint,
    tool_names,
    tool_server_url,
    welcome_greeting,
)


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    if "--check" in sys.argv:
        demo()
        start, agents = build_agents(industry_dir)
        bp = load_blueprint(industry_dir)
        tools = advertised_tools(industry_dir)
        per = {a: tool_names(bp, a) for a in bp["agents"]}
        print(
            f"ok {industry_dir.name} × {RUNTIME} model={MODEL} start={start} "
            f"agents={agents} start_tools={tools} per_agent={per} "
            f"welcome={welcome_greeting()!r} tool_server={tool_server_url() or TOOL_SERVER_URL}"
        )
    else:
        # Live traffic is served by adapters/chirp.py (TwiML + ConversationRelay WS).
        print(
            "Run the ConversationRelay server:\n"
            "  uv run python voice-agent-harnesses/twilio/conversationrelay-gpt4.1/adapters/chirp.py\n"
            "Or: uv run python voice-agent-harnesses/twilio/adapters/chirp.py",
            file=sys.stderr,
        )
        sys.exit(2)
