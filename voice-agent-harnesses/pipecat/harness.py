"""Blueprint → Pipecat services, tools and nodes, for three runtimes.

Pipecat runs our code, so every blueprint tool is a plain Python coroutine wrapped
in `report.tool_span` — handoffs, session tools (`end_call`) and industry tools all
produce real `execute_tool` spans, no untimeable gaps. Industry tools dispatch
generically to the tool server's POST /tools/{name} route.

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

import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import begin_session, end_session, headers as tool_headers, set_call_id  # noqa: E402

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

STT_MODEL = os.environ.get("PIPECAT_STT_MODEL", "").strip() or "flux-general-en"
TTS_MODEL = os.environ.get("PIPECAT_TTS_MODEL", "").strip() or "eleven_flash_v2_5"
TTS_VOICE_ID = os.environ.get("PIPECAT_TTS_VOICE_ID", "").strip() or "21m00Tcm4TlvDq8ikWAM"

# Fallback only for packs that omit `greeting` (control-industry). Healthcare
# and the other industry packs put the spoken opener on agent_blueprint.json;
# generate_reply/say/TTSSpeakFrame must use that, not this repair-shop line.
GREETING = "Welcome to Bluejay's Repair Services!"
# Gemini 3.1 Live will not speak until the caller does, so it opens with dead
# air until the digital human gives up and prompts — ~64 s of a 180 s call. A TTS
# is attached for that runtime purely so the scripted opener can be spoken; the
# model still says everything else itself. Same workaround as the LiveKit harness
# (`session.say(GREETING)`), for the same plugin limitation.
GREETING_TTS_RUNTIMES = frozenset({"gemini-flash-live-3.1"})
# Matches *step 1* of an agent's own flow, e.g. scheduler.md. Used so a Gemini
# Live handoff target can speak a first line (the model rejects speaking first).
_OPENER_RE = re.compile(r'(?:^|\n)\s*1\.\s*(?:Ask|Say):\s*"([^"]+)"', re.IGNORECASE)
GENERIC_OPENER = "Okay, I can help you with that."


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
        "greeting": (blueprint.get("greeting") or "").strip(),
    }


def agent_order(bp: dict[str, Any]) -> list[str]:
    """Blueprint agents, starting agent first."""
    return [bp["start"]] + [n for n in bp["agents"] if n != bp["start"]]


def pack_greeting(bp: dict[str, Any] | None = None) -> str:
    """Spoken opener for this pack. Blueprint wins; GREETING is control-industry."""
    if bp is not None and (bp.get("greeting") or "").strip():
        return str(bp["greeting"]).strip()
    return GREETING


def today_clock() -> str:
    d = _dt.date.today()
    return f"Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}."


def with_clock(text: str) -> str:
    """gpt-4.1 (and the S2S models) have no 'today'; relative dates invent years."""
    clock = today_clock()
    if clock in text:
        return text
    return f"{text.rstrip()}\n\n{clock}"


def resolve_agent_name(default: str) -> str:
    """LiveKit dispatch name. Unique per k8s slug so two industries cannot collide.

    Local/dev (no MIVAS_SLUG) keeps the runtime default (`mivas-pipecat-cascaded`).
    LIVEKIT_AGENT_NAME wins when set.
    """
    explicit = os.environ.get("LIVEKIT_AGENT_NAME", "").strip()
    if explicit:
        return explicit
    slug = os.environ.get("MIVAS_SLUG", "").strip()
    if slug:
        return f"mivas-{slug}"
    return default


def sim_result_id_from_job_metadata(raw: Any) -> str | None:
    """Bluejay puts X-Simulation-Result-Id on the LiveKit job metadata JSON."""
    if not raw:
        return None
    meta = raw if isinstance(raw, dict) else None
    if meta is None:
        try:
            meta = json.loads(str(raw))
        except Exception:
            return None
    if not isinstance(meta, dict):
        return None
    for key in (
        "X-Simulation-Result-Id",
        "x-simulation-result-id",
        "simulation_result_id",
        "simulationResultId",
    ):
        val = meta.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def agent_opener(bp: dict[str, Any], agent: str) -> str:
    """First spoken line for a Gemini Live handoff target, from *this* agent's prompt."""
    match = _OPENER_RE.search(bp["agents"][agent]["instructions"])
    return match.group(1) if match else GENERIC_OPENER


def instructions(
    bp: dict[str, Any], agent: str, *, speak_first: bool = True
) -> str:
    """This agent's prompt, plus today's date, plus a start-agent opener.

    Packs that already script the greeting in the prompt (control-industry) are
    left alone. Packs that assume the harness already spoke it (healthcare)
    get an explicit first-utterance line so cascaded / OpenAI Realtime actually
    greet. Pass ``speak_first=False`` for Gemini Live: TTSSpeakFrame owns the
    opener and a prompt line would make the model re-greet after the caller.
    """
    text = with_clock(bp["agents"][agent]["instructions"])
    if not speak_first or agent != bp["start"]:
        return text
    greeting = pack_greeting(bp)
    if greeting in text:
        return text
    return (
        f"{text.rstrip()}\n\n"
        f'When the call starts, greet the caller with exactly: "{greeting}"'
    )


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


def _tool_entry(bp: dict[str, Any], agent: str, name: str) -> dict[str, Any] | None:
    """The blueprint entry for a tool, preferring the current agent's copy."""
    for owner in [agent] + [a for a in bp["agents"] if a != agent]:
        for t in bp["agents"][owner]["tools"]:
            if t["name"] == name:
                return t
    return None


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Run a blueprint tool. Returns (result, should_end_call).

    Handoff and session tools are harness-native; every other tool is POSTed to
    {TOOL_SERVER_URL}/tools/{name} and the server's envelope is the result. A
    handoff only resolves and records the target here; moving the call is the
    caller's job, because *how* you move it is a runtime property (a Flows node
    transition, or an `LLMSwitcher` swap to the target agent's own S2S session).
    """
    entry = _tool_entry(bp, state["agent"], name)
    if entry is not None and entry.get("handoff"):
        target = handoff_target(bp, state["agent"], name)
        if not target:
            return {"success": False, "error": "unknown handoff target"}, False
        state["agent"] = target
        return {"success": True, "role": target}, False

    if entry is not None and entry.get("session"):
        return {"success": True}, True

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{tool_server_url()}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        return resp.json(), False


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
            ok = bool(result.get("ok", result.get("success", True)))
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
    speak_first = runtime not in GREETING_TTS_RUNTIMES
    return {
        agent: build_llm(
            runtime,
            instructions(bp, agent, speak_first=speak_first),
            agent_tools_schema(bp, agent, handler),
        )
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
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramFluxSTTService.Settings(
            model=STT_MODEL,
            # LiveKit cascaded uses the same 0.4: Flux starts the LLM before a
            # high-confidence EndOfTurn. Default (off) waits for full EOT.
            eager_eot_threshold=0.4,
        ),
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

    # control-industry scripts the opener in the prompt; healthcare puts it on
    # the blueprint and the harness must inject it (except Gemini Live TTS).
    assert f'say: "{GREETING}"' in instructions(bp, "receptionist"), GREETING
    assert pack_greeting(bp) == GREETING
    assert GREETING_TTS_RUNTIMES <= S2S_RUNTIMES
    assert today_clock() in instructions(bp, "receptionist")
    hc = load_blueprint("healthcare")
    assert "Straus Dermatology" in pack_greeting(hc)
    assert pack_greeting(hc) in instructions(hc, hc["start"])
    assert pack_greeting(hc) not in instructions(
        hc, hc["start"], speak_first=False
    )
    prev_name, prev_slug = os.environ.pop("LIVEKIT_AGENT_NAME", None), os.environ.pop(
        "MIVAS_SLUG", None
    )
    try:
        assert resolve_agent_name("mivas-pipecat-cascaded") == "mivas-pipecat-cascaded"
        os.environ["MIVAS_SLUG"] = "pipecat-cascaded-healthcare"
        assert resolve_agent_name("mivas-pipecat-cascaded") == (
            "mivas-pipecat-cascaded-healthcare"
        )
    finally:
        os.environ.pop("MIVAS_SLUG", None)
        if prev_slug is not None:
            os.environ["MIVAS_SLUG"] = prev_slug
        if prev_name is not None:
            os.environ["LIVEKIT_AGENT_NAME"] = prev_name

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
