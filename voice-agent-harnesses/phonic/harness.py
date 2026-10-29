"""Industry pack → Phonic multi-agent system (Phonic speech-to-speech WebSocket).

Every blueprint stage is its own Phonic agent in the project `mivas-<industry>`:
its own system prompt and only its own tools. Handoff tools (`handoff: true`) are
Phonic `built_in_transfer_to_agent` tools whose target is the stage agent, so the
transfer happens inside Phonic (same socket, target agent's prompt and tools, history
kept); nothing on our side swaps prompts. Every other tool is a `custom_websocket`
tool executed here against the industry tool server; `end_call` and the human
escalation session tools hang up after their output.

`provision()` upserts the agents and tools from the pack, so the Phonic side is
rebuilt from the repo alone. Conversations: `wss://api.phonic.ai/v1/sts/ws`,
`Authorization: Bearer`, `config` names the entry agent and pins model + 16 kHz PCM.

Docs: docs.phonic.ai/api-reference/conversations/conversations,
docs.phonic.ai/docs/build/tools/transfer_tools#transfer-to-agent
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import headers as tool_headers, log_ws_accept, set_call_id  # noqa: E402,F401
from pack_clock import today_clock_line, with_pack_clock  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("PHONIC_API_URL", "https://api.phonic.ai/v1").rstrip("/")
URL = os.environ.get("PHONIC_STS_URL", "wss://api.phonic.ai/v1/sts/ws")
MODEL = os.environ.get("PHONIC_MODEL", "phonic-1")
INTELLIGENCE = os.environ.get("PHONIC_INTELLIGENCE", "high")
VOICE = os.environ.get("PHONIC_VOICE", "grant")
AUDIO_FORMAT = "pcm_16000"  # CHIRP's rate, both directions: no resampling
TOOL_TIMEOUT_MS = 30_000
# Phonic's benchmark recommendation: the agent may defer a reply, then speaks after 3 s of silence.
RESPOND_AFTER_SEC = float(os.environ.get("PHONIC_RESPOND_AFTER_SEC", "3"))
STAY_SILENT_TOOL = "choose_not_to_respond"


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    return (REPO_ROOT / "industries" / str(name)).resolve()


@dataclass
class Pack:
    industry_dir: Path
    industry: str
    start: str
    greeting: str
    prompts: dict[str, str]
    tools: dict[str, list[dict[str, Any]]]  # stage → blueprint tool entries
    catalog: dict[str, dict[str, Any]]  # tool name → tools.json spec

    def entry(self, stage: str, tool: str) -> dict[str, Any] | None:
        return next((t for t in self.tools[stage] if t["name"] == tool), None)


def load_pack(industry: str | Path) -> Pack:
    d = industry_path(industry)
    bp = json.loads((d / "agent_blueprint.json").read_text())
    catalog = {t["name"]: t for t in json.loads((d / "tools.json").read_text())["tools"]}
    name = os.environ.get("INDUSTRY", "").strip() if d == Path("/app/industry") else d.name
    return Pack(
        industry_dir=d,
        industry=name or d.name,
        start=bp["agents"][0]["name"],
        greeting=str(bp.get("greeting") or "").strip(),
        prompts={a["name"]: (d / a["system_prompt"]).read_text() for a in bp["agents"]},
        tools={a["name"]: [t for t in a["tools"] if t["name"] in catalog] for a in bp["agents"]},
        catalog=catalog,
    )


def project_name(pack: Pack) -> str:
    return f"mivas-{pack.industry}".replace("_", "-").lower()


def agent_name(stage: str) -> str:
    """Phonic agent names allow lowercase letters, digits and hyphens only."""
    return stage.replace("_", "-").lower()


def stage_of(agent: str, pack: Pack) -> str:
    return next((s for s in pack.tools if agent_name(s) == agent), agent)


# Phonic's tool schema rejects every other JSON-schema keyword ("Unrecognized key"), measured
# 2026-10-27: pattern, format, minLength, maxLength, minimum, maximum, default, examples.
_SCHEMA_KEYS = {"type", "description", "enum", "items", "minItems", "properties", "required", "additionalProperties"}


def _phonic_schema(node: Any) -> Any:
    """Keep what Phonic accepts; fold the rest into the field description so the model still
    sees the pack's constraints (the tool server enforces them either way)."""
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    dropped = []
    for k, v in node.items():
        if k == "properties" and isinstance(v, dict):
            out[k] = {name: _phonic_schema(p) for name, p in v.items()}
        elif k == "items":
            out[k] = _phonic_schema(v)
        elif k in _SCHEMA_KEYS:
            out[k] = v
        else:
            dropped.append(f"{k} {json.dumps(v) if not isinstance(v, str) else v}")
    if dropped:
        out["description"] = (str(out.get("description") or "").rstrip(". ") + ". Constraints: " + "; ".join(dropped) + ".").lstrip(". ")
    return out


def tool_parameters(spec: dict[str, Any]) -> dict[str, Any]:
    params = _phonic_schema(json.loads(json.dumps(spec.get("inputSchema") or {"type": "object"})))
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    params["additionalProperties"] = False  # Phonic's JSON-schema form requires it
    return params


def tool_specs(pack: Pack) -> list[dict[str, Any]]:
    """One Phonic tool per pack tool used by any stage."""
    out: dict[str, dict[str, Any]] = {}
    for entries in pack.tools.values():
        for e in entries:
            spec = pack.catalog[e["name"]]
            body: dict[str, Any] = {
                "name": e["name"],
                "description": spec.get("description") or e["name"],
                "execution_mode": "sync",
            }
            if e.get("handoff"):
                body.update(type="built_in_transfer_to_agent", agents_to_transfer_to=[agent_name(e["handoff_to"])])
            else:
                # uninterruptible: caller speech would cancel a sync call Phonic-side after the tool
                # server already wrote it, so the model retries and double-books (seen 2026-10-27)
                body.update(type="custom_websocket", parameters=tool_parameters(spec),
                            tool_call_output_timeout_ms=TOOL_TIMEOUT_MS, uninterruptible=True)
            out[e["name"]] = body
    return list(out.values())


def system_prompt(pack: Pack, stage: str) -> str:
    return with_pack_clock(pack.prompts[stage], pack.industry_dir)


def agent_specs(pack: Pack) -> list[dict[str, Any]]:
    out = []
    for stage, entries in pack.tools.items():
        body: dict[str, Any] = {
            "name": agent_name(stage),
            "system_prompt": system_prompt(pack, stage),
            "phonic_model": MODEL,
            "intelligence_level": INTELLIGENCE,
            "voice_id": VOICE,
            "audio_format": AUDIO_FORMAT,
            "tools": [e["name"] for e in entries] + [STAY_SILENT_TOOL],
            "built_in_tool_configs": {f"tool_{STAY_SILENT_TOOL}": {"respond_after_sec": RESPOND_AFTER_SEC}},
            "welcome_message": None,
            "generate_welcome_message": False,
        }
        if stage == pack.start:  # only the entry agent opens the call; transfers continue it
            if pack.greeting:
                body["welcome_message"] = pack.greeting
            else:
                body["generate_welcome_message"] = True
        out.append(body)
    return out


def api_key() -> str:
    key = os.environ.get("PHONIC_API_KEY", "").strip()
    if not key:
        raise SystemExit("need PHONIC_API_KEY")
    return key


def _api(client: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
    return client.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {api_key()}"}, **kw)


def provision(pack: Pack) -> dict[str, Any]:
    """Upsert the pack's Phonic project, stage agents and tools. Idempotent; safe to race."""
    proj = project_name(pack)
    with httpx.Client(timeout=30.0) as c:
        names = {p["name"] for p in _api(c, "GET", "/projects").json().get("projects", [])}
        if proj not in names:
            r = _api(c, "POST", "/projects", json={"name": proj})
            if r.status_code not in (200, 201, 409):
                raise RuntimeError(f"create project {proj}: {r.status_code} {r.text[:300]}")
        agents = agent_specs(pack)
        for a in agents:  # transfer targets must exist before transfer tools reference them
            r = _api(c, "PUT", "/agents/upsert", params={"project": proj}, json={"name": a["name"]})
            if r.status_code >= 400:
                raise RuntimeError(f"agent {a['name']}: {r.status_code} {r.text[:300]}")
        for t in tool_specs(pack):
            r = _api(c, "POST", "/tools", params={"project": proj}, json=t)
            if r.status_code == 409:
                patch = {k: v for k, v in t.items() if k != "type"}
                r = _api(c, "PATCH", f"/tools/{t['name']}", params={"project": proj}, json=patch)
            if r.status_code >= 400:
                raise RuntimeError(f"tool {t['name']}: {r.status_code} {r.text[:400]}")
        for a in agents:
            r = _api(c, "PUT", "/agents/upsert", params={"project": proj}, json=a)
            if r.status_code >= 400:
                raise RuntimeError(f"agent {a['name']}: {r.status_code} {r.text[:400]}")
    return {"project": proj, "agents": [a["name"] for a in agents], "entry": agent_name(pack.start)}


def config_message(pack: Pack) -> dict[str, Any]:
    """Conversation start: the entry agent of the pack's project, model and audio pinned."""
    return {
        "type": "config",
        "project": project_name(pack),
        "agent": agent_name(pack.start),
        "phonic_model": MODEL,
        "intelligence_level": INTELLIGENCE,
        "input_format": AUDIO_FORMAT,
        "output_format": AUDIO_FORMAT,
    }


def connect():
    import websockets

    return websockets.connect(URL, additional_headers={"Authorization": f"Bearer {api_key()}"}, max_size=None)


TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


async def post_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}", json={"arguments": args}, headers=tool_headers()
        )
    try:
        body = resp.json()
    except ValueError:
        body = {"success": False, "error": f"tool server {resp.status_code}: {resp.text[:200]}"}
    return body if isinstance(body, dict) else {"result": body}


async def run_tool(
    pack: Pack, stage: str, name: str, args: dict[str, Any], *, call_id: str | None = None, parent=None
) -> tuple[dict[str, Any], bool]:
    """Execute one Phonic websocket tool call. Returns (result, hang up)."""
    from report import finish_tool_span, tool_span

    entry = pack.entry(stage, name)
    with tool_span(name, args, call_id=call_id, parent=parent) as span:
        hangup = False
        try:
            if entry is None:
                result = {"success": False, "error": f"tool {name} is not available in stage {stage}"}
            elif name == "end_call":
                result, hangup = {"success": True}, True
            else:
                result = await post_tool(name, args)
                hangup = bool(entry.get("session"))
            # tool servers answer {"ok": true}; only transfer_*/end_call use "success"
            ok = bool(result.get("ok") or result.get("success"))
        except Exception as e:  # noqa: BLE001
            result, ok = {"success": False, "error": f"{type(e).__name__}: {e}"}, False
        finish_tool_span(span, result, ok=ok)
    return result, hangup


def record_transfer(pack: Pack, stage: str, name: str, target_agent: str, parent=None) -> str:
    """Span for a Phonic-executed transfer (Bluejay reads handoffs from execute_tool spans)."""
    from report import finish_tool_span, tool_span

    target = stage_of(target_agent, pack)
    with tool_span(name, {}, parent=parent) as span:
        finish_tool_span(span, {"success": True, "transferred_to": target}, ok=True)
    return target


def tool_call_output(tool_call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_call_output", "tool_call_id": tool_call_id, "output": result}


def demo() -> None:
    import asyncio

    for ind in ("control-industry", "healthcare", "legal", "customer-support"):
        d = REPO_ROOT / "industries" / ind
        if not d.is_dir():
            continue
        pack = load_pack(d)
        agents, tools = agent_specs(pack), {t["name"]: t for t in tool_specs(pack)}
        assert [a["name"] for a in agents] == [agent_name(s) for s in pack.tools]
        assert sum(bool(a["welcome_message"] or a["generate_welcome_message"]) for a in agents) == 1
        for stage, entries in pack.tools.items():
            a = next(x for x in agents if x["name"] == agent_name(stage))
            assert a["tools"] == [e["name"] for e in entries] + [STAY_SILENT_TOOL]  # only its own tools
            assert a["built_in_tool_configs"][f"tool_{STAY_SILENT_TOOL}"]["respond_after_sec"] == 3
            assert today_clock_line(pack.industry_dir) in a["system_prompt"]
            for e in entries:
                t = tools[e["name"]]
                if e.get("handoff"):
                    assert t["type"] == "built_in_transfer_to_agent"
                    assert t["agents_to_transfer_to"] == [agent_name(e["handoff_to"])]
                    assert agent_name(e["handoff_to"]) in {x["name"] for x in agents}
                else:
                    assert t["type"] == "custom_websocket" and t["parameters"]["additionalProperties"] is False
                    assert t["uninterruptible"] is True
        for t in tools.values():
            props = t.get("parameters", {}).get("properties", {})
            assert all(set(p) <= _SCHEMA_KEYS for p in props.values()), t["name"]
        cfg = config_message(pack)
        assert cfg["agent"] == agent_name(pack.start) and cfg["input_format"] == cfg["output_format"] == AUDIO_FORMAT
    pack = load_pack(REPO_ROOT / "industries" / "control-industry")
    res, hangup = asyncio.run(run_tool(pack, pack.start, "end_call", {"reason": "done"}))
    assert hangup and res == {"success": True}
    res, _ = asyncio.run(run_tool(pack, pack.start, "no_such_tool", {}))
    assert res["success"] is False
    print(f"phonic self-check ok model={MODEL} intelligence={INTELLIGENCE}")


if __name__ == "__main__":
    demo()
