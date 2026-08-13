"""Line agent deployed to Cartesia (`cartesia deploy`) — the provider-side agent.

Line is code-first: this file *is* the agent config, so the industry blueprint
has to travel with it. `harness.export_blueprint()` writes `blueprint.json` into
this directory right before every deploy; the deployed runtime has no repo.

Multi-agent is native: the receptionist's `handoff_to_scheduler` is a Line
handoff tool (`agent_as_handoff`), so the scheduler takes over inside Line with
no bridge-side routing. `end_call` is Line's built-in.

`schedule_appointment` is an `http_server_tool` pointed at the harness webhook
(`$TOOL_BASE_URL/tool/schedule_appointment?call_id=<ac_*>`) rather than a local function: that
webhook is what emits the `execute_tool` span and forwards to the industry tool
server. `get_agent` runs per call and bakes Cartesia's call id onto the URL and
`X-Call-Id` headers so a random CHIRP replica can resolve the Bluejay sim id via
the tools bind store. TOOL_BASE_URL is the ephemeral cloudflared URL, re-pushed with
`cartesia env set` on every chirp boot, and read per call so a new tunnel needs no redeploy.
"""

import json
import os
from pathlib import Path
from urllib.parse import quote

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


def _call_id(call_request: CallRequest | None) -> str | None:
    cid = getattr(call_request, "call_id", None) if call_request is not None else None
    if cid is None:
        return None
    text = str(cid).strip()
    return text or None


def _tool_url(name: str, call_id: str | None = None) -> str:
    base = os.environ.get("TOOL_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("TOOL_BASE_URL unset — run `cartesia env set --agent-id … TOOL_BASE_URL=…`")
    url = f"{base}/tool/{name}"
    if call_id:
        return f"{url}?call_id={quote(str(call_id), safe='')}"
    return url


def _http_tool(name: str, call_id: str | None) -> object:
    spec = BLUEPRINT["catalog"][name]
    schema = dict(spec.get("inputSchema") or {"type": "object"})
    schema.pop("additionalProperties", None)
    extra: dict = {}
    if call_id:
        extra["headers"] = {
            "X-Call-Id": str(call_id),
            "X-Cartesia-Call-Id": str(call_id),
        }
    return http_server_tool(
        name=name,
        description=spec.get("description", name),
        url=_tool_url(name, call_id),
        method="POST",
        request_body_schema=schema,
        **extra,
    )


def _build(agent_name: str, call_id: str | None) -> LlmAgent:
    entry = BLUEPRINT["agents"][agent_name]
    tools = []
    for t in entry["tools"]:
        if t.get("handoff"):
            target = _build(t["handoff_to"], call_id)
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
            tools.append(_http_tool(t["name"], call_id))
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
    return _build(BLUEPRINT["start"], _call_id(call_request))


app = VoiceAgentApp(get_agent=get_agent)

if __name__ == "__main__":
    app.run()
