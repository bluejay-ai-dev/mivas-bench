"""Blueprint → Cartesia Line agent (deployed), plus the harness-side tool runner.

Line is code-first, so the "provider-side agent config" is `line_agent/main.py`
plus a generated `line_agent/blueprint.json` (the deployed runtime has no repo).
`ensure_agent()` deploys that directory with the `cartesia` CLI and caches the
resulting agent id in `.agents.json`.

Tools run provider-side: each industry tool is an `http_server_tool` inside
the Line agent that POSTs to `{TOOL_BASE_URL}/tool/<name>`, i.e. back into
`adapters/chirp.py`. That webhook is what emits the `execute_tool` span and
forwards verbatim to the industry tool server's POST /tools/{name} dispatch,
so `run_tool` here is the webhook's body — nothing calls it from the audio path.

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
import sys
import time
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


def _agent_id_for_name(desired: str) -> str | None:
    """Return the cloud agent id for `desired`, if it already exists."""
    for line in _cli("agents", "ls").splitlines():
        if desired not in line.split():
            continue
        match = re.search(r"agent_[A-Za-z0-9]+", line)
        if match:
            return match.group(0)
    return None


def export_blueprint(industry_dir: str | Path) -> str:
    """Bake the industry prompts + tool schemas into the deployable directory."""
    bp = load_blueprint(industry_dir)
    name = Path(bp["industry_dir"]).name
    (AGENT_DIR / "blueprint.json").write_text(
        json.dumps({k: v for k, v in bp.items() if k != "industry_dir"}, indent=2) + "\n"
    )
    return name


def _ensure_auth() -> None:
    key = os.environ.get("CARTESIA_API_KEY", "").strip()
    if not key:
        raise SystemExit("CARTESIA_API_KEY is required")
    proc = subprocess.run([CLI, "auth", "login", key], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"cartesia auth login failed:\n{(proc.stdout + proc.stderr).strip()}"
        )


def _line_src_digest() -> str:
    """Hash the deployed Line agent so a main.py change triggers `cartesia deploy`."""
    hasher = hashlib.sha256()
    for rel in ("main.py", "pyproject.toml", "blueprint.json"):
        path = AGENT_DIR / rel
        if path.is_file():
            hasher.update(rel.encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()[:16]


def ensure_agent(industry_dir: str | Path, *, public_url: str | None = None) -> str:
    """Deploy (once per industry) and refresh the deployed agent's environment.

    CARTESIA_AGENT_ID skips the cache; a cached id skips the deploy. `env set`
    runs every boot because TOOL_BASE_URL follows the cloudflared tunnel.
    """
    _ensure_auth()
    name = export_blueprint(industry_dir)
    cache = _cache()
    cached_entry = cache.get(name, {})
    cached_agent_id = cached_entry.get("agent_id")
    agent_id = os.environ.get("CARTESIA_AGENT_ID", "").strip() or cached_agent_id
    agent_changed = bool(cached_agent_id and agent_id != cached_agent_id)
    created_new = False
    desired = f"mivas-{name}"

    if not agent_id:
        agent_id = _agent_id_for_name(desired)
        if agent_id:
            print(f"cartesia reusing {desired} ({agent_id})", flush=True)
            _cli("init", "--overwrite", "--agent-id", agent_id, str(AGENT_DIR))
        else:
            _cli("init", "--overwrite", "--new", desired, str(AGENT_DIR))
            # `init --new` writes .cartesia/config.toml: agent-id = 'agent_…'
            agent_id = (AGENT_DIR / ".cartesia" / "config.toml").read_text().split("'")[1]
            created_new = True

    env = {"TOOL_BASE_URL": public_url or os.environ.get("PUBLIC_URL", "")}
    for key in ("OPENAI_API_KEY", "MIVAS_MODEL", "MIVAS_GREETING"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env = {k: v for k, v in env.items() if v}
    # `env set` rolls a new deployment version (~2 min), so only push on change —
    # by digest, so the cache file never holds the API key it carries.
    digest = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()[:16]
    src_digest = _line_src_digest()
    needs_env_set = agent_changed or cached_entry.get("env_digest") != digest
    needs_deploy = (
        created_new
        or agent_changed
        or bool(os.environ.get("CARTESIA_REDEPLOY"))
        or cached_entry.get("src_digest") != src_digest
    )
    # New pods have an empty emptyDir cache, so both digests always miss.
    # Re-running `env set` / `deploy` 3× races Cartesia's API (HTTP 500). Skip
    # when the cloud agent is already Ready unless CARTESIA_REDEPLOY=1 (use
    # that when line_agent/ or TOOL_BASE_URL actually changed).
    already_ready = False
    if not created_new and not os.environ.get("CARTESIA_REDEPLOY"):
        try:
            status = _cli("status", agent_id)
        except SystemExit:
            status = ""
        already_ready = bool(re.search(r"Status\s+Ready", status))
    if needs_env_set:
        if already_ready:
            print(f"cartesia skip env set, {agent_id} already Ready", flush=True)
        else:
            _cli("env", "set", "--agent-id", agent_id, *[f"{k}={v}" for k, v in env.items()])
        cache.setdefault(name, {}).update(agent_id=agent_id, env_digest=digest)
        AGENT_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")

    if needs_deploy:
        if already_ready:
            print(f"cartesia skip deploy, {agent_id} already Ready", flush=True)
        else:
            print(f"cartesia deploy {agent_id} …", flush=True)
            print(_cli("deploy", "--agent-id", agent_id, str(AGENT_DIR)), flush=True)
        cache.setdefault(name, {}).update(agent_id=agent_id, src_digest=src_digest)
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


async def _execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Generic dispatch: POST /tools/{name}; the server's envelope is the result.
    A 404 for an unknown/non-dispatchable tool has no `ok`/`success` key, so
    run_tool's `default=False` fallback (not an exception here) is what turns
    it into a failure, matching how the other harnesses handle the same case."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        return resp.json()


async def run_tool(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str | None = None,
    emit_span: bool = True,
) -> dict[str, Any]:
    """Run an industry tool under an execute_tool span. Never raises — a failed
    tool must not take down the bridge before OTel flushes."""
    from report import finish_tool_span, tool_span
    if not emit_span:
        if call_id:
            for_provider(call_id)
        try:
            return await _execute_tool(name, args)
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    with tool_span(name, args, call_id=call_id) as span:
        try:
            if call_id:
                for_provider(call_id)
            result = await _execute_tool(name, args)
            ok = bool(result.get("ok", result.get("success", False)))
            finish_tool_span(span, result, ok=ok)
            return result
        except Exception as e:
            err = {"success": False, "error": f"{type(e).__name__}: {e}"}
            finish_tool_span(span, err, ok=False)
            return err
