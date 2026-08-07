"""Build an OpenAI Realtime voice agent from a MIVAS industry agent_blueprint.json."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from agents import FunctionTool
from agents.realtime import RealtimeAgent, RealtimeRunner, realtime_handoff

MODEL = "gpt-realtime-2.1"
REPO_ROOT = Path(__file__).resolve().parents[2]

# ponytail: process-local store; swap when Bluejay evals need durable DB state
DB: dict[str, Any] = {}


def _tool_catalog(industry_dir: Path) -> dict[str, dict]:
    data = json.loads((industry_dir / "tools.json").read_text())
    return {t["name"]: t for t in data["tools"]}


def _fn_tool(spec: dict) -> FunctionTool:
    name = spec["name"]
    schema = {**spec["inputSchema"], "additionalProperties": False}
    schema.setdefault("properties", {})

    async def on_invoke(_ctx: Any, raw: str) -> str:
        args = json.loads(raw or "{}")
        if name == "schedule_appointment":
            date = args["date"]
            DB["appointment"] = {"date": date}
            return json.dumps({"success": True, "date": date})
        return json.dumps({"success": True})

    return FunctionTool(
        name=name,
        description=spec.get("description", name),
        params_json_schema=schema,
        on_invoke_tool=on_invoke,
    )


def build_agents(industry_dir: str | Path) -> tuple[RealtimeAgent, dict[str, RealtimeAgent]]:
    """Parse blueprint → (starting_agent, all_agents)."""
    industry_dir = Path(industry_dir).resolve()
    blueprint = json.loads((industry_dir / "agent_blueprint.json").read_text())
    catalog = _tool_catalog(industry_dir)

    agents: dict[str, RealtimeAgent] = {}
    for entry in blueprint["agents"]:
        agents[entry["name"]] = RealtimeAgent(
            name=entry["name"],
            instructions=(industry_dir / entry["system_prompt"]).read_text(),
            tools=[
                _fn_tool(catalog[t["name"]])
                for t in entry["tools"]
                if not t.get("handoff")
            ],
        )

    for entry in blueprint["agents"]:
        handoffs = []
        for t in entry["tools"]:
            if not t.get("handoff"):
                continue
            desc = catalog.get(t["name"], {}).get(
                "description", f"Hand off to {t['handoff_to']}"
            )
            handoffs.append(
                realtime_handoff(
                    agents[t["handoff_to"]],
                    tool_name_override=t["name"],
                    tool_description_override=desc,
                )
            )
        if handoffs:
            agents[entry["name"]].handoffs = handoffs

    start = agents[blueprint["agents"][0]["name"]]
    return start, agents


def build_from_blueprint(industry_dir: str | Path) -> RealtimeRunner:
    """Load agent_blueprint.json + tools.json + prompts → ready RealtimeRunner."""
    start, _ = build_agents(industry_dir)
    return RealtimeRunner(
        starting_agent=start,
        config={
            "model_settings": {
                "model_name": MODEL,
                "audio": {
                    "input": {
                        "format": "pcm16",
                        "turn_detection": {
                            "type": "semantic_vad",
                            "interrupt_response": True,
                        },
                    },
                    "output": {"format": "pcm16", "voice": "ash"},
                },
                "tool_choice": "auto",
            }
        },
    )


async def run(industry: str = "control-industry") -> None:
    runner = build_from_blueprint(REPO_ROOT / "industries" / industry)
    async with await runner.run() as session:
        async for event in session:
            print(event.type)


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = REPO_ROOT / "industries" / industry
    if "--check" in sys.argv:
        start, agents = build_agents(industry_dir)
        assert start.handoffs, "starting agent needs a handoff"
        assert any(t.name == "schedule_appointment" for t in agents["scheduler"].tools)
        build_from_blueprint(industry_dir)
        print(f"ok {industry} → {MODEL} ({', '.join(agents)})")
    else:
        asyncio.run(run(industry))
