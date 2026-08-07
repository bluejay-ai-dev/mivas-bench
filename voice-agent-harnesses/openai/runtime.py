"""Shared blueprint → RealtimeRunner builder for OpenAI Realtime harnesses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents import FunctionTool
from agents.realtime import RealtimeAgent, RealtimeRunner, realtime_handoff

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
        # ponytail: stub OK until industry tool servers are wired
        return json.dumps({"ok": True, "success": True, **args})

    return FunctionTool(
        name=name,
        description=spec.get("description", name),
        params_json_schema=schema,
        on_invoke_tool=on_invoke,
    )


def build_agents(industry_dir: str | Path) -> tuple[RealtimeAgent, dict[str, RealtimeAgent]]:
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
                if not t.get("handoff") and t["name"] in catalog
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

    return agents[blueprint["agents"][0]["name"]], agents


def build_from_blueprint(industry_dir: str | Path, model: str) -> RealtimeRunner:
    start, _ = build_agents(industry_dir)
    return RealtimeRunner(
        starting_agent=start,
        config={
            "model_settings": {
                "model_name": model,
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


def industry_path(name: str) -> Path:
    return REPO_ROOT / "industries" / name
