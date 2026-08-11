"""Blueprint → Pipecat services, tools and pipeline, for three runtimes.

Pipecat runs our code, so the industry tools are plain Python coroutines wrapped
in `report.tool_span` — all three (`handoff_to_scheduler`, `schedule_appointment`,
`end_call`) produce real `execute_tool` spans, no untimeable gaps.

Handoff is soft (prompt + tool result), not Pipecat Flows: Flows explicitly does
not support realtime S2S services, and two of the three runtimes are S2S. Doing
it the same way in all three keeps the runtimes comparable.

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
DEFAULT_RUNTIME = "cascaded"

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

HANDOFF_NOTE = (
    "\n\n# Multi-agent note\n"
    "Start as the receptionist. Only call schedule_appointment after "
    "handoff_to_scheduler has succeeded and you have adopted the scheduler role."
)


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


def system_prompt(bp: dict[str, Any]) -> str:
    return bp["agents"][bp["start"]]["instructions"] + HANDOFF_NOTE


def tool_names(bp: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for agent in bp["agents"].values():
        for t in agent["tools"]:
            if t["name"] not in seen and t["name"] in bp["catalog"]:
                seen.append(t["name"])
    return seen


# ── tools ────────────────────────────────────────────────────────────────────


def tool_server_url() -> str:
    return os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Run a blueprint tool. Returns (result, should_end_call)."""
    if name == "handoff_to_scheduler":
        target = next(
            (
                t.get("handoff_to")
                for t in bp["agents"][state["agent"]]["tools"]
                if t["name"] == name and t.get("handoff")
            ),
            None,
        )
        if not target or target not in bp["agents"]:
            return {"success": False, "error": "unknown handoff target"}, False
        state["agent"] = target
        return {
            "success": True,
            "role": target,
            "instructions": bp["agents"][target]["instructions"],
            "note": "You are now the scheduler. Follow the instructions field exactly.",
        }, False

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


def function_schemas(bp: dict[str, Any], handler) -> list:
    """tools.json → Pipecat FunctionSchema list, all bound to one handler."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    schemas = []
    for name in tool_names(bp):
        spec = bp["catalog"][name]
        raw = spec.get("inputSchema") or {}
        schemas.append(
            FunctionSchema(
                name=name,
                description=spec.get("description", name),
                properties=dict(raw.get("properties") or {}),
                required=list(raw.get("required") or []),
                handler=handler,
            )
        )
    return schemas


# ── services ─────────────────────────────────────────────────────────────────


def build_llm(runtime: str, instructions: str, tools: list):
    """The runtime's LLM (or S2S) service. `tools`/`instructions` are only passed
    here for the S2S services that need them at construction time."""
    model = RUNTIMES[runtime]

    if runtime == "openai-realtime-2.1":
        from pipecat.services.openai.realtime.events import SessionProperties
        from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

        return OpenAIRealtimeLLMService(
            api_key=os.environ["OPENAI_API_KEY"],
            model=model,
            session_properties=SessionProperties(instructions=instructions),
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

    from pipecat.services.openai.llm import OpenAILLMService

    return OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"], model=model)


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
    """Self-check: blueprint, tool schemas and the soft handoff, no network."""
    import asyncio

    bp = load_blueprint("control-industry")
    assert bp["start"] == "receptionist", bp["start"]
    assert tool_names(bp) == [
        "handoff_to_scheduler",
        "end_call",
        "schedule_appointment",
    ], tool_names(bp)
    assert "Bluejay's Repair Services" in system_prompt(bp)
    assert set(RUNTIMES) == {
        "cascaded",
        "openai-realtime-2.1",
        "gemini-flash-live-3.1",
    }

    # the scripted opener must stay verbatim from the industry prompt, and only
    # the runtimes that cannot speak first may be given a greeting TTS
    assert f'say: "{GREETING}"' in bp["agents"]["receptionist"]["instructions"], GREETING
    assert GREETING_TTS_RUNTIMES <= set(RUNTIMES), GREETING_TTS_RUNTIMES
    assert "cascaded" not in GREETING_TTS_RUNTIMES

    # (the greeting-only TTS gate needs pipecat installed — see check.py)

    state = {"agent": bp["start"]}
    res, stop = asyncio.run(run_tool("handoff_to_scheduler", {}, bp, state))
    assert res["success"] and state["agent"] == "scheduler" and not stop, res
    assert "schedule_appointment" in res["instructions"]

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
