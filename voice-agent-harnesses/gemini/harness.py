"""Blueprint → Gemini Live session helpers (google-genai SDK).

Industry tools map to the industry state API. Handoff is soft (Live config is
fixed at connect). Session tools (end_call) end the live session.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from google import genai
from google.genai import types

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    return (REPO_ROOT / "industries" / name).resolve()


def load_blueprint(industry_dir: str | Path) -> dict[str, Any]:
    industry_dir = industry_path(industry_dir)
    blueprint = json.loads((industry_dir / "agent_blueprint.json").read_text())
    catalog = {t["name"]: t for t in json.loads((industry_dir / "tools.json").read_text())["tools"]}
    agents = {}
    for entry in blueprint["agents"]:
        agents[entry["name"]] = {
            "name": entry["name"],
            "instructions": (industry_dir / entry["system_prompt"]).read_text(),
            "tools": entry["tools"],
        }
    return {
        "industry_dir": industry_dir,
        "start": blueprint["agents"][0]["name"],
        "agents": agents,
        "catalog": catalog,
    }


def _decl(spec: dict) -> types.FunctionDeclaration:
    # Gemini Live rejects JSON-Schema keys like additionalProperties.
    raw = dict(spec.get("inputSchema") or {})
    raw.pop("additionalProperties", None)
    props = {}
    for key, prop in (raw.get("properties") or {}).items():
        p = {k: v for k, v in dict(prop).items() if k in {"type", "description", "enum"}}
        props[key] = p
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if raw.get("required"):
        schema["required"] = list(raw["required"])
    return types.FunctionDeclaration(
        name=spec["name"],
        description=spec.get("description", spec["name"]),
        parameters=schema,
    )


def live_config(bp: dict[str, Any], *, voice: str = "Puck") -> types.LiveConnectConfig:
    """All blueprint tools are declared up front (Live config is fixed at connect)."""
    decls = []
    seen: set[str] = set()
    for agent in bp["agents"].values():
        for t in agent["tools"]:
            name = t["name"]
            if name in seen or name not in bp["catalog"]:
                continue
            seen.add(name)
            decls.append(_decl(bp["catalog"][name]))

    start = bp["agents"][bp["start"]]
    # note soft-handoff: scheduler tools are visible; prompt still starts as receptionist
    instruction = (
        start["instructions"]
        + "\n\n# Multi-agent note\n"
        "Start as the receptionist. Only call schedule_appointment after "
        "handoff_to_scheduler has succeeded and you have adopted the scheduler role."
    )
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=instruction)]),
        tools=[types.Tool(function_declarations=decls)] if decls else None,
    )


async def _post_appointment(date: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TOOL_SERVER_URL}/appointments", json={"date": date})
        resp.raise_for_status()
        body = resp.json()
    return {"success": True, "date": body["date"]}


async def run_tool(
    name: str,
    args: dict[str, Any],
    bp: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Execute a tool. Returns (result, should_end_call)."""
    if name == "handoff_to_scheduler":
        target = None
        for t in bp["agents"][state["agent"]]["tools"]:
            if t["name"] == name and t.get("handoff"):
                target = t.get("handoff_to")
                break
        if not target or target not in bp["agents"]:
            return {"success": False, "error": "unknown handoff target"}, False
        state["agent"] = target
        sched = bp["agents"][target]
        return {
            "success": True,
            "role": target,
            "instructions": sched["instructions"],
            "note": "You are now the scheduler. Follow the instructions field exactly.",
        }, False

    if name == "schedule_appointment":
        return await _post_appointment(args["date"]), False

    if name == "end_call":
        return {"success": True}, True

    return {"success": False, "error": f"unknown tool {name}"}, False


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


async def run_live(industry_dir: str | Path, model: str) -> None:
    """open a Live session; stdin text turns in, event types on stdout."""
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("need GOOGLE_API_KEY")
    bp = load_blueprint(industry_dir)
    state = {"agent": bp["start"]}
    client = genai.Client(api_key=key)
    async with client.aio.live.connect(model=model, config=live_config(bp)) as session:
        prompt = "Hello"
        while True:
            await session.send_realtime_input(text=prompt)
            async for response in session.receive():
                if response.data:
                    print(f"audio {len(response.data)}", flush=True)
                if response.tool_call:
                    replies = []
                    should_end = False
                    for fc in response.tool_call.function_calls or []:
                        result, stop = await run_tool(fc.name, dict(fc.args or {}), bp, state)
                        should_end = should_end or stop
                        print(f"tool {fc.name}", flush=True)
                        replies.append(
                            types.FunctionResponse(id=fc.id, name=fc.name, response=result)
                        )
                    if replies:
                        await session.send_tool_response(function_responses=replies)
                    if should_end:
                        return
                sc = response.server_content
                if sc is not None and getattr(sc, "turn_complete", False):
                    print("turn_complete", flush=True)
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            prompt = line.strip() or "Hello"
