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
_REPAIR_GREETING = "Welcome to Bluejay's Repair Services!"
_REPAIR_HANDOFF = "Hey, when do you want to schedule your repair appointment?"
GREETING = os.getenv("MIVAS_GREETING") or BLUEPRINT.get("greeting") or _REPAIR_GREETING


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


def _build(
    agent_name: str,
    call_id: str | None,
    stack: frozenset[str] = frozenset(),
    cache: dict[str, LlmAgent] | None = None,
) -> LlmAgent:
    """build one Line agent; skip handoff edges that would recurse (healthcare is cyclic)."""
    cache = cache if cache is not None else {}
    if agent_name in cache:
        return cache[agent_name]
    entry = BLUEPRINT["agents"][agent_name]
    tools = []
    next_stack = stack | {agent_name}
    for t in entry["tools"]:
        if t.get("handoff"):
            target_name = t["handoff_to"]
            if target_name in next_stack:
                continue
            target = _build(target_name, call_id, next_stack, cache)
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
    intro = _introduction(agent_name)
    config = (
        LlmConfig(system_prompt=entry["instructions"], introduction=intro)
        if intro
        else LlmConfig(system_prompt=entry["instructions"])
    )
    agent = LlmAgent(
        model=MODEL,
        api_key=os.environ["OPENAI_API_KEY"],
        tools=tools,
        config=config,
    )
    cache[agent_name] = agent
    return agent


def _introduction(agent_name: str) -> str | None:
    """start node speaks the pack greeting; handoff targets must not re-greet.

    control-industry has no pack greeting and the scheduler only talks on
    CallStarted if it has an introduction. Other packs continue mid-stride.
    """
    if agent_name == BLUEPRINT["start"]:
        return GREETING
    override = os.getenv("MIVAS_HANDOFF_INTRO")
    if override is not None:
        return override or None
    if BLUEPRINT.get("greeting"):
        return None
    return _REPAIR_HANDOFF


async def get_agent(env: AgentEnv, call_request: CallRequest) -> LlmAgent:
    return _build(BLUEPRINT["start"], _call_id(call_request))


app = VoiceAgentApp(get_agent=get_agent)

if __name__ == "__main__":
    app.run()
