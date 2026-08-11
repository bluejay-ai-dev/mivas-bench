"""Line agent deployed to Cartesia (`cartesia deploy`) — the provider-side agent.

Line is code-first: this file *is* the agent config, so the industry blueprint
has to travel with it. `harness.export_blueprint()` writes `blueprint.json` into
this directory right before every deploy; the deployed runtime has no repo.

Multi-agent is native: the receptionist's `handoff_to_scheduler` is a Line
handoff tool (`agent_as_handoff`), so the scheduler takes over inside Line with
no bridge-side routing. `end_call` is Line's built-in.

`schedule_appointment` is an `http_server_tool` pointed at the harness webhook
(`$TOOL_BASE_URL/tool/schedule_appointment`) rather than a local function: that
webhook is what emits the `execute_tool` span and forwards to the industry tool
server. TOOL_BASE_URL is the ephemeral cloudflared URL, re-pushed with
`cartesia env set` on every chirp boot, and read per call (get_agent runs per
call) so a new tunnel needs no redeploy.
"""

import json
import os
from pathlib import Path

from line.llm_agent import (
    LlmAgent,
    LlmConfig,
    agent_as_handoff,
    end_call,
    http_server_tool,
)
from line.voice_agent_app import AgentEnv, CallRequest, VoiceAgentApp

BLUEPRINT = json.loads((Path(__file__).parent / "blueprint.json").read_text())
MODEL = os.getenv("MIVAS_MODEL", "gpt-4.1")
GREETING = os.getenv("MIVAS_GREETING", "Welcome to Bluejay's Repair Services!")
# A handoff target is started with CallStarted, so it only speaks if it has an
# introduction — this is the scheduler prompt's own step 1, verbatim.
HANDOFF_INTRO = "Hey, when do you want to schedule your repair appointment?"


def _tool_url(name: str) -> str:
    base = os.environ.get("TOOL_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("TOOL_BASE_URL unset — run `cartesia env set --agent-id … TOOL_BASE_URL=…`")
    return f"{base}/tool/{name}"


def _http_tool(name: str) -> object:
    spec = BLUEPRINT["catalog"][name]
    schema = dict(spec.get("inputSchema") or {"type": "object"})
    schema.pop("additionalProperties", None)
    return http_server_tool(
        name=name,
        description=spec.get("description", name),
        url=_tool_url(name),
        method="POST",
        request_body_schema=schema,
    )


def _build(agent_name: str) -> LlmAgent:
    entry = BLUEPRINT["agents"][agent_name]
    tools = []
    for t in entry["tools"]:
        if t.get("handoff"):
            target = _build(t["handoff_to"])
            tools.append(
                agent_as_handoff(
                    target,
                    name=t["name"],
                    description=BLUEPRINT["catalog"][t["name"]]["description"],
                )
            )
        elif t.get("session") or t["name"] == "end_call":
            tools.append(end_call)
        else:
            tools.append(_http_tool(t["name"]))
    return LlmAgent(
        model=MODEL,
        api_key=os.environ["OPENAI_API_KEY"],
        tools=tools,
        config=LlmConfig(
            system_prompt=entry["instructions"],
            introduction=GREETING if agent_name == BLUEPRINT["start"] else HANDOFF_INTRO,
        ),
    )


async def get_agent(env: AgentEnv, call_request: CallRequest) -> LlmAgent:
    return _build(BLUEPRINT["start"])


app = VoiceAgentApp(get_agent=get_agent)

if __name__ == "__main__":
    app.run()
