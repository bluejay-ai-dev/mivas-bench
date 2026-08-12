"""Blueprint → Bland Conversational Pathway (their native multi-agent).

Bland is fully in-house (own STT/LLM/TTS), so there is nothing to pin per
component — the knobs are `model` ("base": every feature; "turbo" trades
features for latency), the voice, and `interruption_threshold`.

Multi-agent on Bland is a **pathway**, not a squad: a node graph where each
blueprint agent is a Default node and the blueprint's handoff is an edge whose
label is the transition condition. Industry tools are Webhook nodes that call
the harness back over HTTPS (`{PUBLIC_URL}/tool/<name>`, served by
`adapters/chirp.py`) — that inbound request is also what times the
`execute_tool` span, so the tool has to be provider-side, not client-side.

The tunnel URL is ephemeral, so `ensure_agent` re-pushes the whole graph on
every chirp boot; only the pathway/agent IDs are cached in `.agents.json`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = Path(__file__).resolve().parent
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")

API_BASE = "https://api.bland.ai"
WS_BASE = os.environ.get("BLAND_WS_BASE", "wss://stream-v2.aws.dc8.bland.ai/ws/connect/blandshared")
AGENT_CACHE_PATH = HARNESS_DIR / ".agents.json"

# Bland's newest in-house TTS (BTTS_V3), described by them as warm/friendly at a
# moderate pace for customer support — the closest thing they ship to a
# production receptionist voice.
DEFAULT_VOICE = os.environ.get("BLAND_VOICE", "Jordan")
DEFAULT_MODEL = os.environ.get("BLAND_MODEL", "base")
# Bland's default is 500 and their own docs recommend 50–200; 500 makes the agent
# wait so long after the digital human stops that turns collide.
INTERRUPTION_THRESHOLD = int(os.environ.get("BLAND_INTERRUPTION_THRESHOLD", "150"))
MAX_DURATION_MIN = int(os.environ.get("BLAND_MAX_DURATION_MIN", "5"))


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
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


def _api_key() -> str:
    key = os.environ.get("BLAND_API_KEY")
    if not key:
        raise SystemExit("need BLAND_API_KEY")
    return key


def _headers() -> dict[str, str]:
    # Bland wants the raw key — no "Bearer" prefix.
    return {"authorization": _api_key(), "Content-Type": "application/json"}


# A pathway Default node has no tools bound — tools are Webhook nodes and handoffs are
# edges — so any prompt sentence that tells the model to *call* one is an instruction it
# can only satisfy by emitting tool syntax into its dialogue, which Bland's TTS then reads
# out ("<tool_call>schedule_appointment</tool_call>", "handoff underscore to scheduler").
# The pathway routes on the edge descriptions, so those sentences are redundant as well as
# harmful: strip them, and keep the note for the surrounding narration only.
PATHWAY_NOTE = """

# Bland pathway
Never announce a transfer or that you are looking something up, and never read out any
name written in snake_case — say your line, and the pathway moves you on by itself.
"""

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _strip_tool_instructions(instructions: str, tool_names: Iterable[str]) -> str:
    """Drop every sentence that names a blueprint tool, then any heading left empty."""
    named = re.compile("|".join(re.escape(name) for name in tool_names))
    kept = []
    for line in instructions.splitlines():
        if line.strip():
            line = " ".join(s for s in _SENTENCE.split(line) if not named.search(s))
            if not line.strip():
                continue
        kept.append(line)
    body = []
    for i, line in enumerate(kept):
        if line.startswith("#"):
            after = [x for x in kept[i + 1 :] if x.strip()]
            if not after or after[0].startswith("#"):
                continue
        body.append(line)
    return "\n".join(body).rstrip()


def _adapt_prompt(instructions: str, tool_names: Iterable[str]) -> str:
    return _strip_tool_instructions(instructions, tool_names) + PATHWAY_NOTE


def _model_options() -> dict[str, Any]:
    return {"modelName": DEFAULT_MODEL, "interruptionThreshold": INTERRUPTION_THRESHOLD}


def _extract_vars(spec: dict[str, Any]) -> list[list[str]]:
    """tools.json inputSchema → Bland's positional [name, type, description] triples."""
    schema = spec.get("inputSchema") or {}
    return [
        [name, prop.get("type", "string"), prop.get("description", name)]
        for name, prop in (schema.get("properties") or {}).items()
    ]


def _webhook_node(
    node_id: str,
    spec: dict[str, Any],
    public_url: str,
    *,
    next_node: tuple[str, str],
    failure_node: tuple[str, str] | None = None,
    response_data: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A tool as a Webhook node.

    Webhook nodes do NOT leave via edges — `responsePathways` is their routing,
    and `["Default/Webhook Completion", "", "", target]` is the unconditional
    "carry on when the call returns" branch.
    """
    variables = _extract_vars(spec)
    return {
        "id": node_id,
        "type": "Webhook",
        "data": {
            "name": spec["name"],
            "url": f"{public_url.rstrip('/')}/tool/{spec['name']}",
            "method": "POST",
            "body": json.dumps({v[0]: "{{" + v[0] + "}}" for v in variables}),
            "extractVars": variables,
            "responseData": response_data or [],
            "timeoutValue": 10,
            "modelOptions": {"skipUserResponse": True},
            "responsePathways": [
                [
                    "Default/Webhook Completion",
                    "",
                    "",
                    {"id": next_node[0], "name": next_node[1]},
                ],
            ]
            + (
                [
                    [
                        "Default/Webhook Failure",
                        "",
                        "",
                        {"id": failure_node[0], "name": failure_node[1]},
                    ]
                ]
                if failure_node
                else []
            ),
        },
    }


def pathway_graph(bp: dict[str, Any], public_url: str) -> dict[str, Any]:
    """Blueprint → {nodes, edges}.

    receptionist ──"caller wants an appointment"──▶ handoff_to_scheduler (Webhook)
      ──▶ scheduler ──"concrete date agreed"──▶ schedule_appointment (Webhook) ──▶ End Call

    The handoff is a webhook node rather than a bare edge so the transition shows
    up as a timed `execute_tool` span like every other blueprint tool.

    Routing lives in the edge's `description` (`label` is just the editor's display
    name), and it has to be sent **top-level on the edge** — Bland silently drops
    anything you nest under `edge.data` and stores its own `data` copy, so an edge
    written the way the docs example shows arrives with no description and never fires.
    """
    start = bp["agents"][bp["start"]]
    handoff = next(t for t in start["tools"] if t.get("handoff"))
    target = bp["agents"][handoff["handoff_to"]]
    booking = next(t for t in target["tools"] if not t.get("session") and not t.get("handoff"))
    booking_spec = bp["catalog"][booking["name"]]

    nodes = [
        {
            "id": "receptionist",
            "type": "Default",
            "data": {
                "name": start["name"],
                "prompt": _adapt_prompt(start["instructions"], bp["catalog"]),
                "isStart": True,
                "modelOptions": _model_options(),
            },
        },
        _webhook_node(
            "handoff",
            bp["catalog"][handoff["name"]],
            public_url,
            next_node=("scheduler", target["name"]),
        ),
        {
            "id": "scheduler",
            "type": "Default",
            "data": {
                "name": target["name"],
                "prompt": _adapt_prompt(target["instructions"], bp["catalog"]),
                "modelOptions": _model_options(),
            },
        },
        _webhook_node(
            "book",
            booking_spec,
            public_url,
            next_node=("end", "end_call"),
            failure_node=("scheduler", target["name"]),
            response_data=[{"name": "{{booked_date}}", "data": "$.date", "context": ""}],
        ),
        {
            "id": "end",
            "type": "End Call",
            "data": {
                "name": "end_call",
                "prompt": "Confirm the repair appointment is booked for {{booked_date}}, "
                "say goodbye, and end the call.",
                "modelOptions": {"skipUserResponse": True},
            },
        },
        {
            "id": "end_receptionist",
            "type": "End Call",
            "data": {
                "name": "end_call_receptionist",
                "prompt": "Say goodbye and end the call.",
                "modelOptions": {"skipUserResponse": True},
            },
        },
        {
            "id": "end_scheduler",
            "type": "End Call",
            "data": {
                "name": "end_call_scheduler",
                "prompt": "Say goodbye and end the call.",
                "modelOptions": {"skipUserResponse": True},
            },
        },
    ]
    # Webhook nodes route via responsePathways, so the only edges are the two
    # LLM-decided transitions out of the Default nodes.
    edges = [
        {
            "id": "e_handoff",
            "source": "receptionist",
            "target": "handoff",
            "label": "receptionist → scheduler",
            "description": "Take this path as soon as the caller says they want to "
            "schedule, book, or set up a repair appointment.",
        },
        {
            "id": "e_book",
            "source": "scheduler",
            "target": "book",
            "label": "scheduler → schedule_appointment",
            "description": "Take this path once a concrete calendar date has been given "
            "or agreed on, so the appointment can be booked. Do not take it while the "
            "date is still vague (\"next week\", \"soon\").",
        },
        {
            "id": "e_end_receptionist",
            "source": "receptionist",
            "target": "end_receptionist",
            "label": "receptionist → end_call",
            "description": "Take this path when the caller is done, says goodbye, or says "
            "this is a wrong number.",
        },
        {
            "id": "e_end_scheduler",
            "source": "scheduler",
            "target": "end_scheduler",
            "label": "scheduler → end_call",
            "description": "Take this path when the caller is done, says goodbye, or says "
            "this is a wrong number.",
        },
    ]
    return {"nodes": nodes, "edges": edges}


def _load_cache() -> dict[str, Any]:
    if not AGENT_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(AGENT_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def ensure_agent(industry_dir: str | Path, public_url: str) -> dict[str, str]:
    """Create-or-reuse the pathway + web agent, then re-push the graph.

    Re-pushing is not an optimization skip: the webhook nodes carry this run's
    cloudflared URL, which changes every boot.
    """
    bp = load_blueprint(industry_dir)
    industry_name = Path(bp["industry_dir"]).name
    cache = _load_cache()
    entry = dict(cache.get(industry_name) or {})
    agent_config = {
        "prompt": bp["agents"][bp["start"]]["instructions"],
        "pathway_id": entry.get("pathway_id"),
        "voice": DEFAULT_VOICE,
        "model": DEFAULT_MODEL,
        "language": "ENG",
        "max_duration": MAX_DURATION_MIN,
        "interruption_threshold": INTERRUPTION_THRESHOLD,
    }

    if not entry.get("pathway_id"):
        r = httpx.post(
            f"{API_BASE}/v1/convo_pathway/create",
            headers=_headers(),
            json={"name": f"mivas-{industry_name}", "description": "MIVAS bench control industry"},
            timeout=30.0,
        )
        r.raise_for_status()
        entry["pathway_id"] = r.json()["data"]["pathway_id"]

    graph = pathway_graph(bp, public_url)
    r = httpx.post(
        f"{API_BASE}/v1/pathway/{entry['pathway_id']}",
        headers=_headers(),
        json={"name": f"mivas-{industry_name}", **graph},
        timeout=60.0,
    )
    r.raise_for_status()

    if not entry.get("agent_id"):
        r = httpx.post(
            f"{API_BASE}/v1/agents",
            headers=_headers(),
            json={**agent_config, "pathway_id": entry["pathway_id"]},
            timeout=30.0,
        )
        r.raise_for_status()
        entry["agent_id"] = r.json()["agent"]["agent_id"]
    else:
        agent_config["pathway_id"] = entry["pathway_id"]
        r = httpx.patch(
            f"{API_BASE}/v1/agents/{entry['agent_id']}",
            headers=_headers(),
            json=agent_config,
            timeout=30.0,
        )
        r.raise_for_status()

    cache[industry_name] = entry
    AGENT_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")
    return entry


async def session_ws_url(agent_id: str) -> str:
    """Mint a single-use session token and build the stream URL for one call."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{API_BASE}/v1/agents/{agent_id}/authorize", headers={"authorization": _api_key()}
        )
        r.raise_for_status()
        token = r.json()["token"]
    return f"{WS_BASE}?agent={agent_id}&token={token}"


def _tool_flags(name: str) -> dict[str, Any]:
    """Blueprint flags for a tool (handoff/session), from the INDUSTRY env."""
    global _FLAGS
    if _FLAGS is None:
        bp = load_blueprint(os.environ.get("INDUSTRY", "control-industry"))
        _FLAGS = {}
        for entry in bp["agents"].values():
            for t in entry["tools"]:
                _FLAGS.setdefault(t["name"], t)
    return _FLAGS.get(name, {})


_FLAGS: dict[str, dict[str, Any]] | None = None


async def _execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Industry tools dispatch to POST /tools/{name}; handoff webhook nodes have
    no server state to write and session tools stay harness-native."""
    flags = _tool_flags(name)
    if flags.get("handoff") or flags.get("session"):
        return {"success": True}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TOOL_SERVER_URL}/tools/{name}", json={"arguments": args})
        return resp.json()


async def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a pathway webhook call under an execute_tool span. Never raises."""
    from report import finish_tool_span, tool_span
    with tool_span(name, args) as span:
        try:
            result = await _execute_tool(name, args)
            ok = bool(result.get("ok", result.get("success", True)))
        except Exception as e:
            result, ok = {"success": False, "error": f"{type(e).__name__}: {e}"}, False
        finish_tool_span(span, result, ok=ok)
        return result
