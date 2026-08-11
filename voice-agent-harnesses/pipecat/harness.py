"""Blueprint → Pipecat services, tools and nodes, for three runtimes.

Pipecat runs our code, so the industry tools are plain Python coroutines wrapped
in `report.tool_span` — all three (`handoff_to_scheduler`, `schedule_appointment`,
`end_call`) produce real `execute_tool` spans, no untimeable gaps.

The handoff is a real agent switch in every runtime, not a prompt injection: each
blueprint agent gets its own prompt and its own tool set, and `handoff_to_scheduler`
moves the call from one to the other. What "the other agent" is made of differs by
runtime, because Pipecat's own machinery differs:

  cascaded              Pipecat Flows (`pipecat.flows`). One text LLM, one node per
                        blueprint agent, each node carrying its own `task_messages`
                        and its own `functions`. The consolidated handler returns
                        `(result, next_node)` and FlowManager swaps the context and
                        the advertised tool set (`LLMSetToolsFrame`).
  openai-realtime-2.1   Two `OpenAIRealtimeLLMService` instances behind an
  gemini-flash-live-3.1 `LLMSwitcher`, one per blueprint agent, each with its own
                        websocket session, its own `instructions` and its own
                        `tools`. `ManuallySwitchServiceFrame` moves the call.
                        Flows explicitly does not support S2S services ("Gemini
                        Live, OpenAI Realtime, Ultravox, AWS Nova Sonic"), because
                        it transitions by mutating one live session — which is the
                        exact thing two sessions make unnecessary.

Either way the receptionist's model never sees `schedule_appointment`.

Runtimes:
  cascaded              Deepgram Flux flux-general-en → gpt-4.1 → ElevenLabs eleven_flash_v2_5
  openai-realtime-2.1   gpt-realtime-2.1
  gemini-flash-live-3.1 gemini-3.1-flash-live-preview
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

HARNESS_DIR = Path(__file__).resolve().parent
# In the repo this is mivas-bench/; in the deployed image the harness IS the root.
REPO_ROOT = HARNESS_DIR.parents[1] if len(HARNESS_DIR.parents) > 1 else HARNESS_DIR

RUNTIMES = {
    "cascaded": "gpt-4.1",
    "openai-realtime-2.1": "gpt-realtime-2.1",
    "gemini-flash-live-3.1": "gemini-3.1-flash-live-preview",
}
RUNTIME_SECRET_KEYS = {
    "cascaded": frozenset({"DEEPGRAM_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY"}),
    "openai-realtime-2.1": frozenset({"OPENAI_API_KEY"}),
    "gemini-flash-live-3.1": frozenset({"GOOGLE_API_KEY", "ELEVENLABS_API_KEY"}),
}
DEFAULT_RUNTIME = "cascaded"
# The runtimes whose "agent" is a speech-to-speech session rather than a text LLM.
S2S_RUNTIMES = frozenset({"openai-realtime-2.1", "gemini-flash-live-3.1"})

STT_MODEL = os.environ.get("PIPECAT_STT_MODEL", "flux-general-en")
TTS_MODEL = os.environ.get("PIPECAT_TTS_MODEL", "eleven_flash_v2_5")
TTS_VOICE_ID = os.environ.get("PIPECAT_TTS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Gemini 3.1 Live will not speak until the caller does, so it opens with dead
# air until the digital human gives up and prompts — ~64 s of a 180 s call. A TTS
# is attached for that runtime purely so the scripted opener can be spoken; the
# model still says everything else itself. Same workaround as the LiveKit harness
# (`session.say(GREETING)`), for the same plugin limitation.
GREETING = "Welcome to Bluejay's Repair Services!"
GREETING_TTS_RUNTIMES = frozenset({"gemini-flash-live-3.1"})


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
    """Blueprint agents, starting agent first."""
    return [bp["start"]] + [n for n in bp["agents"] if n != bp["start"]]


def instructions(bp: dict[str, Any], agent: str) -> str:
    return bp["agents"][agent]["instructions"]


def tool_names(bp: dict[str, Any], agent: str) -> list[str]:
    """The tools *this* agent may call. Nobody else's."""
    return [t["name"] for t in bp["agents"][agent]["tools"] if t["name"] in bp["catalog"]]


def handoff_target(bp: dict[str, Any], agent: str, tool: str) -> str | None:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == tool and t.get("handoff"):
            target = t.get("handoff_to")
            return target if target in bp["agents"] else None
    return None


# ── tools ────────────────────────────────────────────────────────────────────


def tool_server_url() -> str:
    return os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Run a blueprint tool. Returns (result, should_end_call).

    A handoff only resolves and records the target here; moving the call is the
    caller's job, because *how* you move it is a runtime property (a Flows node
    transition, or an `LLMSwitcher` swap to the target agent's own S2S session).
    """
    if name == "handoff_to_scheduler":
        target = handoff_target(bp, state["agent"], name)
        if not target:
            return {"success": False, "error": "unknown handoff target"}, False
        state["agent"] = target
        return {"success": True, "role": target}, False

    if name == "schedule_appointment":
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{tool_server_url()}/appointments", json={"date": args["date"]}
            )
            resp.raise_for_status()
            return {"success": True, "date": resp.json()["date"]}, False

    if name == "end_call":
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
    """Execute a tool under an `execute_tool` span. Errors soft-fail so the call
    survives and the trace still flushes."""
    from report import finish_tool_span, tool_span
    with tool_span(name, args, call_id=call_id) as span:
        try:
            result, stop = await _execute_tool(name, args, bp, state)
            ok = bool(result.get("success"))
        except Exception as e:  # noqa: BLE001 — a dead tool must not kill the call
            result, stop, ok = {"success": False, "error": f"{type(e).__name__}: {e}"}, False, False
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


def agent_tools_schema(bp: dict[str, Any], agent: str, handler):
    """One agent's tools as a Pipecat `ToolsSchema`, bound to `handler`.

    Handed to the S2S service at construction, so the session advertises this
    agent's tools and nothing else. Pipecat registers the handlers off the
    service's own tools when the context advertises none
    (`LLMService._sync_registered_tool_handlers`).
    """
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema

    return ToolsSchema(
        standard_tools=[
            FunctionSchema(name=name, description=d, properties=p, required=r, handler=handler)
            for name in tool_names(bp, agent)
            for d, p, r in [_spec(bp, name)]
        ]
    )


def flows_node(bp: dict[str, Any], agent: str, handler):
    """One agent as a Pipecat Flows node: its own prompt, its own functions.

    `handler` is called as `handler(name, args, flow_manager)` and must return
    Flows' consolidated `(result, next_node)`.

    RESET, not APPEND: a handoff hands the caller to a different agent, so the
    scheduler starts on its own prompt rather than inheriting the receptionist's.
    That matches what the S2S runtimes get for free from a second session.
    """
    import functools

    from pipecat.flows import FlowsFunctionSchema, NodeConfig
    from pipecat.flows.types import ContextStrategy, ContextStrategyConfig

    return NodeConfig(
        name=agent,
        task_messages=[{"role": "system", "content": instructions(bp, agent)}],
        functions=[
            FlowsFunctionSchema(
                name=name, description=d, properties=p, required=r,
                handler=functools.partial(handler, name),
            )
            for name in tool_names(bp, agent)
            for d, p, r in [_spec(bp, name)]
        ],
        context_strategy=ContextStrategyConfig(strategy=ContextStrategy.RESET),
    )


# ── services ─────────────────────────────────────────────────────────────────


def build_llm(runtime: str, instructions: str, tools):
    """One agent's LLM. For the S2S runtimes this *is* the agent: its own model
    session, opened with its own instructions and its own tool set."""
    model = RUNTIMES[runtime]

    if runtime == "openai-realtime-2.1":
        from pipecat.services.openai.realtime.events import SessionProperties
        from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

        return OpenAIRealtimeLLMService(
            api_key=os.environ["OPENAI_API_KEY"],
            model=model,
            session_properties=SessionProperties(instructions=instructions, tools=tools),
        )

    if runtime == "gemini-flash-live-3.1":
        from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

        # the plugin quirk carried over from the LiveKit prior art: the system
        # prompt must go in on the constructor, not as a context message.
        return GeminiLiveLLMService(
            api_key=os.environ["GOOGLE_API_KEY"],
            model=model,
            system_instruction=instructions,
            tools=tools,
        )

    # cascaded: prompt and tools are per-node, set by Flows.
    from pipecat.services.openai.llm import OpenAILLMService

    return OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"], model=model)


def build_agent_llms(runtime: str, bp: dict[str, Any], handler) -> dict[str, Any]:
    """One S2S service per blueprint agent, receptionist first."""
    return {
        agent: build_llm(runtime, instructions(bp, agent), agent_tools_schema(bp, agent, handler))
        for agent in agent_order(bp)
    }


def build_tts():
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

    return ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        model=TTS_MODEL,
        voice_id=TTS_VOICE_ID,
    )


def build_stt_tts(runtime: str):
    """(stt, tts) for the cascaded runtime; (None, None) for the S2S ones."""
    if runtime != "cascaded":
        return None, None

    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

    stt = DeepgramFluxSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"], model=STT_MODEL
    )
    return stt, build_tts()


def demo() -> None:
    """Self-check: blueprint, per-agent tool split and the handoff, no network."""
    import asyncio

    bp = load_blueprint("control-industry")
    assert bp["start"] == "receptionist", bp["start"]
    assert agent_order(bp) == ["receptionist", "scheduler"], agent_order(bp)
    assert set(RUNTIMES) == {
        "cascaded",
        "openai-realtime-2.1",
        "gemini-flash-live-3.1",
    }
    assert S2S_RUNTIMES < set(RUNTIMES)

    # The whole point: the receptionist cannot book. Its agent never carries
    # schedule_appointment, so no model session it drives is ever told about it.
    assert tool_names(bp, "receptionist") == ["handoff_to_scheduler", "end_call"]
    assert tool_names(bp, "scheduler") == ["schedule_appointment", "end_call"]
    assert "schedule_appointment" not in tool_names(bp, "receptionist")
    assert "handoff_to_scheduler" not in tool_names(bp, "scheduler")

    assert handoff_target(bp, "receptionist", "handoff_to_scheduler") == "scheduler"
    assert handoff_target(bp, "scheduler", "handoff_to_scheduler") is None

    # the scripted opener must stay verbatim from the industry prompt, and only
    # the runtimes that cannot speak first may be given a greeting TTS
    assert f'say: "{GREETING}"' in instructions(bp, "receptionist"), GREETING
    assert GREETING_TTS_RUNTIMES <= S2S_RUNTIMES

    # (the tool-set split as the services actually see it needs pipecat — see check.py)

    state = {"agent": bp["start"]}
    res, stop = asyncio.run(run_tool("handoff_to_scheduler", {}, bp, state))
    assert res == {"success": True, "role": "scheduler"} and not stop, res
    assert state["agent"] == "scheduler"
    # no instructions blob in the tool result — the switch is the mechanism now
    assert "instructions" not in res

    res, stop = asyncio.run(run_tool("end_call", {"reason": "done"}, bp, state))
    assert res == {"success": True} and stop

    # unreachable tool server must soft-fail, not raise
    os.environ["TOOL_SERVER_URL"] = "http://127.0.0.1:1"
    res, stop = asyncio.run(
        run_tool("schedule_appointment", {"date": "08/18/2026"}, bp, state)
    )
    assert res["success"] is False and "error" in res, res

    print("harness self-check ok")


if __name__ == "__main__":
    demo()
