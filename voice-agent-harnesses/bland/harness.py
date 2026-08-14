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
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import (  # noqa: E402
    begin_session,
    bind_provider,
    end_session,
    for_provider,
    headers as tool_headers,
    provider_id_from_payload,
    provider_id_from_request,
    set_call_id,
    unbind_provider,
)

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
Never announce a transfer, never say you are looking something up, and never read
out snake_case, handoff_summary, next_intent, or a tool name. A short "Sure —"
or "Okay." is enough — the pathway moves you on by itself.
"""

# Reception's job is routing, not identity. Left in the prompt, "ask for name
# and date of birth" makes Bland interview and then speak a fake transfer.
START_NODE_NOTE = """
Do not ask for a name or date of birth. As soon as you know they want to cancel,
book, check insurance, talk billing, or ask about Botox, stop talking.
Do not list appointment times, doctors, or locations — that happens after transfer.
Cancel and reschedule are never a new-patient booking — stop after you hear cancel.
"""

_LEAK = re.compile(
    r"handoff_summary|next_intent|call the transfer tool|every transfer takes|"
    r"call exactly one",
    re.I,
)

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _strip_tool_instructions(instructions: str, tool_names: Iterable[str]) -> str:
    """Drop every sentence that names a blueprint tool, then any heading left empty."""
    named = re.compile("|".join(re.escape(name) for name in tool_names))
    kept = []
    for line in instructions.splitlines():
        if line.strip():
            line = " ".join(
                s
                for s in _SENTENCE.split(line)
                if not named.search(s) and not _LEAK.search(s)
            )
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


# Shared tools on every healthcare specialist. Wiring each as its own edge
# from every Default node drowns the routing criterion; they stay callable
# from the industry tool server if a later specialist node adds them.
_SHARED_SKIP = frozenset(
    {
        "search_practice_kb",
        "create_callback_task",
        "send_sms",
        "transfer_to_human",
    }
)

# Reception must route, not interview. Wiring classify/list_locations as edges
# makes Bland webhook-pause ("one moment while I look that up") and quote slots
# before transfer_to_scheduling, which leaves a 25s+ agent-to-agent gap across
# the new-patient info dump (max_punctuation_latency).
_START_EDGE_SKIP = frozenset(
    {
        "classify_visit_request",
        "list_locations",
    }
)

# Edge `description` is the routing criterion (never the tool name — Bland TTS
# will read snake_case if the model copies it into dialogue).
_HANDOFF_WHEN = {
    "handoff_to_scheduler": (
        "the caller wants to schedule, book, or set up a repair appointment"
    ),
    "transfer_to_identity": (
        "the caller says cancel, cancellation, reschedule, or move an existing "
        "appointment, or is an existing patient who needs their chart for a "
        "bill, results, refill, or insurance update. Take this as soon as you "
        "hear cancel or that they have a bill — before goodbye"
    ),
    "transfer_to_scheduling": (
        "a new patient wants to book a first visit and no existing chart "
        "needs to be looked up. Do not take this path to cancel or "
        "reschedule an appointment that already exists"
    ),
    "transfer_to_coverage": (
        "the caller asks about insurance, whether a plan is accepted, "
        "referrals, eligibility, or what their copay will be"
    ),
    "transfer_to_cosmetic": (
        "the caller asks about Botox, fillers, lasers, peels, cosmetic "
        "pricing, or booking a cosmetic consult"
    ),
    "transfer_to_billing": (
        "the caller is already verified and wants a balance, a charge "
        "explained, a payment link, financing, or a fee waiver"
    ),
    "transfer_to_clinical": (
        "the caller is already verified and wants results status, a refill, "
        "a nurse message, the patient portal, or records"
    ),
}


def _take_path_when(clause: str) -> str:
    clause = clause.strip().rstrip(".")
    return f"Take this path when {clause}."


def _tool_edge_description(spec: dict[str, Any]) -> str:
    name = spec["name"]
    if name in _HANDOFF_WHEN:
        return _take_path_when(_HANDOFF_WHEN[name])
    desc = (spec.get("description") or name).strip()
    desc = re.sub(re.escape(name), "", desc, flags=re.I)
    desc = desc.split(".")[0].strip(" -")
    if re.match(r"invisible handoff", desc, re.I):
        found = re.search(r"\b(?:when|for)\s+(.+)", desc, re.I)
        desc = found.group(1) if found else desc
    if not desc:
        desc = f"the next step is {name.replace('_', ' ')}"
    return _take_path_when(desc)


def _is_control_two_agent(bp: dict[str, Any]) -> bool:
    start = bp["agents"][bp["start"]]
    handoffs = [t for t in start["tools"] if t.get("handoff")]
    return (
        len(bp["agents"]) == 2
        and len(handoffs) == 1
        and handoffs[0]["name"] == "handoff_to_scheduler"
        and handoffs[0].get("handoff_to") in bp["agents"]
    )


def pathway_graph(bp: dict[str, Any], public_url: str) -> dict[str, Any]:
    """Blueprint → {nodes, edges}.

    Control-industry stays the two-node repair-shop graph. Every other
    industry compiles one Default node per agent, a Webhook node per
    handoff and specialist tool, and an End Call node per agent.

    Routing lives in the edge's `description` (`label` is just the editor's display
    name), and it has to be sent **top-level on the edge** — Bland silently drops
    anything you nest under `edge.data` and stores its own `data` copy, so an edge
    written the way the docs example shows arrives with no description and never fires.
    """
    if _is_control_two_agent(bp):
        return _control_pathway_graph(bp, public_url)
    return _blueprint_pathway_graph(bp, public_url)


def _blueprint_pathway_graph(bp: dict[str, Any], public_url: str) -> dict[str, Any]:
    """One Default node per agent; webhooks for handoffs and specialist tools."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    start_name = bp["start"]
    catalog = bp["catalog"]

    for name, agent in bp["agents"].items():
        nodes.append(
            {
                "id": name,
                "type": "Default",
                "data": {
                    "name": name,
                    "prompt": _adapt_prompt(agent["instructions"], catalog)
                    + (START_NODE_NOTE if name == start_name else ""),
                    "isStart": name == start_name,
                    "modelOptions": _model_options(),
                },
            }
        )

    for name, agent in bp["agents"].items():
        end_id = f"end_{name}"
        nodes.append(
            {
                "id": end_id,
                "type": "End Call",
                "data": {
                    "name": f"end_call_{name}",
                    "prompt": "Say goodbye and end the call.",
                    "modelOptions": {"skipUserResponse": True},
                },
            }
        )
        for tool in agent["tools"]:
            tname = tool["name"]
            if tool.get("session") or tname in _SHARED_SKIP:
                continue
            if name == start_name and tname in _START_EDGE_SKIP:
                continue
            spec = catalog[tname]
            webhook_id = f"{name}__{tname}"
            if tool.get("handoff"):
                nxt = tool["handoff_to"]
                nodes.append(
                    _webhook_node(
                        webhook_id,
                        spec,
                        public_url,
                        next_node=(nxt, nxt),
                        failure_node=(name, name),
                    )
                )
            else:
                nodes.append(
                    _webhook_node(
                        webhook_id,
                        spec,
                        public_url,
                        next_node=(name, name),
                        failure_node=(name, name),
                    )
                )
            edges.append(
                {
                    "id": f"e_{name}_{tname}",
                    "source": name,
                    "target": webhook_id,
                    "label": f"{name} → {tname}",
                    "description": _tool_edge_description(spec),
                }
            )
        # end_call last: Bland often picks the first matching edge, and "thank you"
        # after a fake "I'll cancel that" used to skip identity entirely.
        edges.append(
            {
                "id": f"e_end_{name}",
                "source": name,
                "target": end_id,
                "label": f"{name} → end_call",
                "description": (
                    "Take this path only when the caller says goodbye or that "
                    "this is a wrong number, and you are not canceling, booking, "
                    "checking insurance, or talking about a bill."
                ),
            }
        )
    return {"nodes": nodes, "edges": edges}


def _control_pathway_graph(bp: dict[str, Any], public_url: str) -> dict[str, Any]:
    """receptionist → handoff_to_scheduler → scheduler → schedule_appointment → End Call.

    The handoff is a webhook node rather than a bare edge so the transition shows
    up as a timed `execute_tool` span like every other blueprint tool.
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
            json={"name": f"mivas-{industry_name}", "description": f"MIVAS bench {industry_name}"},
            timeout=30.0,
        )
        r.raise_for_status()
        entry["pathway_id"] = r.json()["data"]["pathway_id"]

    graph = pathway_graph(bp, public_url)
    r = httpx.post(
        f"{API_BASE}/v1/pathway/{entry['pathway_id']}",
        headers=_headers(),
        json={"name": f"mivas-{industry_name}", **graph},
        timeout=120.0,
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
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
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
