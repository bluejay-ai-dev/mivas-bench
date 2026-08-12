"""Blueprint → NVIDIA Nemotron cascaded services (ASR → LLM → TTS) + Flows nodes.

Stack mirrors NVIDIA-AI-Blueprints/nemotron-voice-agent (cloud NIM profile):
  NvidiaSTTService (Nemotron ASR Streaming)
  NvidiaLLMService (nemotron-3-nano-30b-a3b)
  NvidiaTTSService (Magpie Multilingual)

Multi-agent: Pipecat Flows — one NodeConfig per blueprint agent, each with its
own prompt and tool set. Handoff tools (`handoff: true`) return
`(result, next_node)` and FlowManager swaps context + advertised tools.
Industry tools map onto the industry state API; session tools (`end_call`) hang up.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1] if len(HARNESS_DIR.parents) > 1 else HARNESS_DIR

RUNTIME = "nemotron"
MODEL = os.environ.get(
    "NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-nano-30b-a3b"
)
LLM_BASE_URL = os.environ.get(
    "NEMOTRON_LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"
)
ASR_SERVER = os.environ.get("NEMOTRON_ASR_SERVER", "grpc.nvcf.nvidia.com:443")
ASR_FUNCTION_ID = os.environ.get(
    "NEMOTRON_ASR_FUNCTION_ID", "bb0837de-8c7b-481f-9ec8-ef5663e9c1fa"
)
ASR_MODEL = os.environ.get("NEMOTRON_ASR_MODEL", "nemotron-asr-streaming")
TTS_SERVER = os.environ.get("NEMOTRON_TTS_SERVER", "grpc.nvcf.nvidia.com:443")
TTS_VOICE = os.environ.get(
    "NEMOTRON_TTS_VOICE", "Magpie-Multilingual.EN-US.Aria"
)
SAMPLE_RATE = int(os.environ.get("NEMOTRON_SAMPLE_RATE", "16000"))

# Verbatim from control-industry receptionist prompt; Flows kicks greeting via LLMRun.
GREETING = "Welcome to Bluejay's Repair Services!"


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    for base in (HARNESS_DIR / "industries", REPO_ROOT / "industries"):
        if (base / name).is_dir():
            return (base / name).resolve()
    return (REPO_ROOT / "industries" / name).resolve()


def load_blueprint(industry_dir: str | Path) -> dict[str, Any]:
    industry_dir = industry_path(industry_dir)
    blueprint = json.loads((industry_dir / "agent_blueprint.json").read_text())
    catalog = {
        t["name"]: t
        for t in json.loads((industry_dir / "tools.json").read_text())["tools"]
    }
    agents = {
        entry["name"]: {
            "name": entry["name"],
            "instructions": (industry_dir / entry["system_prompt"]).read_text(),
            "tools": entry["tools"],
        }
        for entry in blueprint["agents"]
    }
    return {
        "industry_dir": industry_dir,
        "start": blueprint["agents"][0]["name"],
        "agents": agents,
        "catalog": catalog,
    }


def agent_order(bp: dict[str, Any]) -> list[str]:
    return [bp["start"]] + [n for n in bp["agents"] if n != bp["start"]]


def instructions(bp: dict[str, Any], agent: str) -> str:
    return bp["agents"][agent]["instructions"]


def tool_names(bp: dict[str, Any], agent: str) -> list[str]:
    return [t["name"] for t in bp["agents"][agent]["tools"] if t["name"] in bp["catalog"]]


def handoff_target(bp: dict[str, Any], agent: str, tool: str) -> str | None:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == tool and t.get("handoff"):
            target = t.get("handoff_to")
            return target if target in bp["agents"] else None
    return None


def is_session_tool(bp: dict[str, Any], agent: str, tool: str) -> bool:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == tool:
            return bool(t.get("session"))
    return False


def tool_server_url() -> str:
    return os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


def nvidia_api_key() -> str:
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not key:
        raise SystemExit("need NVIDIA_API_KEY")
    return key


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Run a blueprint tool. Returns (result, should_end_call)."""
    target = handoff_target(bp, state["agent"], name)
    if target:
        state["agent"] = target
        return {"success": True, "role": target}, False

    if name == "schedule_appointment":
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{tool_server_url()}/appointments", json={"date": args["date"]}
            )
            resp.raise_for_status()
            return {"success": True, "date": resp.json()["date"]}, False

    if name == "end_call" or is_session_tool(bp, state["agent"], name):
        return {"success": True}, True

    return {"success": False, "error": f"unknown tool {name}"}, False


async def run_tool(
    name: str,
    args: dict[str, Any],
    bp: dict[str, Any],
    state: dict[str, Any],
    *,
    call_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    from report import finish_tool_span, tool_span

    parent = state.get("_otel_root")
    with tool_span(name, args, call_id=call_id, parent=parent) as span:
        try:
            result, stop = await _execute_tool(name, args, bp, state)
            ok = bool(result.get("success"))
        except Exception as e:  # noqa: BLE001 — dead tool must not kill the call
            result, stop, ok = (
                {"success": False, "error": f"{type(e).__name__}: {e}"},
                False,
                False,
            )
        finish_tool_span(span, result, ok=ok)
        return result, stop


def _spec(bp: dict[str, Any], name: str) -> tuple[str, dict, list]:
    spec = bp["catalog"][name]
    raw = spec.get("inputSchema") or {}
    return (
        spec.get("description", name),
        dict(raw.get("properties") or {}),
        list(raw.get("required") or []),
    )


def flows_node(bp: dict[str, Any], agent: str, handler):
    """One agent as a Pipecat Flows node: its own prompt, its own functions."""
    import functools

    from pipecat.flows import FlowsFunctionSchema, NodeConfig
    from pipecat.flows.types import ContextStrategy, ContextStrategyConfig

    return NodeConfig(
        name=agent,
        task_messages=[{"role": "system", "content": instructions(bp, agent)}],
        functions=[
            FlowsFunctionSchema(
                name=name,
                description=d,
                properties=p,
                required=r,
                handler=functools.partial(handler, name),
            )
            for name in tool_names(bp, agent)
            for d, p, r in [_spec(bp, name)]
        ],
        context_strategy=ContextStrategyConfig(strategy=ContextStrategy.RESET),
    )


def build_stt():
    from pipecat.services.nvidia.stt import NvidiaSTTService

    return NvidiaSTTService(
        api_key=nvidia_api_key(),
        server=ASR_SERVER,
        use_ssl=True,
        sample_rate=SAMPLE_RATE,
        model_function_map={
            "function_id": ASR_FUNCTION_ID,
            "model_name": ASR_MODEL,
        },
        stop_history=400,
    )


def build_llm():
    """Text LLM for Flows. Prompt/tools are per-node, not on the service."""
    from pipecat.services.nvidia.llm import NvidiaLLMService, NvidiaLLMSettings

    # Match nemotron-voice-agent cloud catalog: thinking off for lowest latency.
    settings = NvidiaLLMSettings(model=MODEL)
    settings.extra = {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
            "repetition_penalty": 1.05,
        }
    }
    return NvidiaLLMService(
        api_key=nvidia_api_key(),
        base_url=LLM_BASE_URL,
        settings=settings,
    )


def build_tts():
    from pipecat.services.nvidia.tts import NvidiaTTSService

    return NvidiaTTSService(
        api_key=nvidia_api_key(),
        server=TTS_SERVER,
        use_ssl=True,
        voice_id=TTS_VOICE,
        sample_rate=SAMPLE_RATE,
    )


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


def demo() -> None:
    """Self-check: blueprint, per-agent tool split and handoff, no network."""
    import asyncio

    bp = load_blueprint("control-industry")
    assert bp["start"] == "receptionist", bp["start"]
    assert agent_order(bp) == ["receptionist", "scheduler"], agent_order(bp)
    assert tool_names(bp, "receptionist") == ["handoff_to_scheduler", "end_call"]
    assert tool_names(bp, "scheduler") == ["schedule_appointment", "end_call"]
    assert "schedule_appointment" not in tool_names(bp, "receptionist")
    assert handoff_target(bp, "receptionist", "handoff_to_scheduler") == "scheduler"
    assert f'say: "{GREETING}"' in instructions(bp, "receptionist"), GREETING

    state = {"agent": bp["start"]}
    res, stop = asyncio.run(run_tool("handoff_to_scheduler", {}, bp, state))
    assert res == {"success": True, "role": "scheduler"} and not stop, res
    assert state["agent"] == "scheduler"

    res, stop = asyncio.run(run_tool("end_call", {"reason": "done"}, bp, state))
    assert res == {"success": True} and stop

    os.environ["TOOL_SERVER_URL"] = "http://127.0.0.1:1"
    res, stop = asyncio.run(
        run_tool("schedule_appointment", {"date": "08/18/2026"}, bp, state)
    )
    assert res["success"] is False and "error" in res, res

    print("harness self-check ok")


if __name__ == "__main__":
    demo()
