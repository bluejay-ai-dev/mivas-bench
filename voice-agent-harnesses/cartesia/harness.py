"""Blueprint → Cartesia Line agent (deployed), plus the harness-side tool runner.

Line is code-first, so the "provider-side agent config" is `line_agent/main.py`
plus a generated `line_agent/blueprint.json` (the deployed runtime has no repo).
`ensure_agent()` deploys that directory with the `cartesia` CLI and caches the
resulting agent id in `.agents.json`.

Tools run provider-side: `schedule_appointment` is an `http_server_tool` inside
the Line agent that POSTs to `{TOOL_BASE_URL}/tool/schedule_appointment`, i.e.
back into `adapters/chirp.py`. That webhook is what emits the `execute_tool`
span and forwards to the industry tool server, so `run_tool` here is the
webhook's body — nothing calls it from the audio path.

The tunnel URL is ephemeral, so `ensure_agent()` re-pushes `TOOL_BASE_URL` with
`cartesia env set` on every chirp boot; `get_agent` reads it per call, so a new
tunnel needs no redeploy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = Path(__file__).resolve().parent
AGENT_DIR = HARNESS_DIR / "line_agent"
AGENT_CACHE_PATH = HARNESS_DIR / ".agents.json"
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
CLI = os.environ.get("CARTESIA_CLI", str(Path.home() / ".cartesia" / "bin" / "cartesia"))


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


def _cli(*args: str) -> str:
    proc = subprocess.run([CLI, *args], capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise SystemExit(f"cartesia {' '.join(args)} failed:\n{out}")
    return out


def _cache() -> dict[str, Any]:
    if not AGENT_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(AGENT_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def export_blueprint(industry_dir: str | Path) -> str:
    """Bake the industry prompts + tool schemas into the deployable directory."""
    bp = load_blueprint(industry_dir)
    name = Path(bp["industry_dir"]).name
    (AGENT_DIR / "blueprint.json").write_text(
        json.dumps({k: v for k, v in bp.items() if k != "industry_dir"}, indent=2) + "\n"
    )
    return name


def ensure_agent(industry_dir: str | Path, *, public_url: str | None = None) -> str:
    """Deploy (once per industry) and refresh the deployed agent's environment.

    CARTESIA_AGENT_ID skips the cache; a cached id skips the deploy. `env set`
    runs every boot because TOOL_BASE_URL follows the cloudflared tunnel.
    """
    name = export_blueprint(industry_dir)
    cache = _cache()
    agent_id = os.environ.get("CARTESIA_AGENT_ID", "").strip() or cache.get(name, {}).get("agent_id")

    if not agent_id:
        _cli("init", "--new", f"mivas-{name}", str(AGENT_DIR))
        # `init --new` writes .cartesia/config.toml: agent-id = 'agent_…'
        agent_id = (AGENT_DIR / ".cartesia" / "config.toml").read_text().split("'")[1]

    env = {"TOOL_BASE_URL": public_url or os.environ.get("PUBLIC_URL", "")}
    for key in ("OPENAI_API_KEY", "MIVAS_MODEL", "MIVAS_GREETING"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env = {k: v for k, v in env.items() if v}
    # `env set` rolls a new deployment version (~2 min), so only push on change —
    # by digest, so the cache file never holds the API key it carries.
    digest = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()[:16]
    if cache.get(name, {}).get("env_digest") != digest:
        _cli("env", "set", "--agent-id", agent_id, *[f"{k}={v}" for k, v in env.items()])
        cache.setdefault(name, {}).update(agent_id=agent_id, env_digest=digest)
        AGENT_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")

    if cache.get(name, {}).get("agent_id") != agent_id or os.environ.get("CARTESIA_REDEPLOY"):
        print(f"cartesia deploy {agent_id} …", flush=True)
        print(_cli("deploy", "--agent-id", agent_id, str(AGENT_DIR)), flush=True)
        cache.setdefault(name, {})["agent_id"] = agent_id
        AGENT_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")

    # `env set` and `deploy` both roll a new version; calls hit the old one (or
    # fail) until it builds, so block rather than start the bridge on stale env.
    deadline = time.monotonic() + float(os.environ.get("CARTESIA_DEPLOY_TIMEOUT", "420"))
    while time.monotonic() < deadline:
        status = _cli("status", agent_id)
        if re.search(r"Status\s+Ready", status):
            return agent_id
        if re.search(r"Status\s+Failed", status):
            raise SystemExit(status)
        print("cartesia deployment building …", flush=True)
        time.sleep(10)
    raise SystemExit(f"cartesia deployment for {agent_id} never became Ready")


async def _post_appointment(date: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TOOL_SERVER_URL}/appointments", json={"date": date})
        resp.raise_for_status()
        body = resp.json()
    return {"success": True, "date": body["date"]}


async def _execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "schedule_appointment":
        return await _post_appointment(args["date"])
    return {"success": False, "error": f"unknown tool {name}"}


async def run_tool(name: str, args: dict[str, Any], *, call_id: str | None = None) -> dict[str, Any]:
    """Run an industry tool under an execute_tool span. Never raises — a failed
    tool must not take down the bridge before OTel flushes."""
    from report import call_offset_ms, finish_tool_span, tool_span
    with tool_span(name, args, call_id=call_id) as span:
        try:
            result = await _execute_tool(name, args)
            finish_tool_span(span, result, ok=True)
            return result
        except Exception as e:
            err = {"success": False, "error": f"{type(e).__name__}: {e}"}
            finish_tool_span(span, err, ok=False)
            return err
