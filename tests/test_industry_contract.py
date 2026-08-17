"""The industry contract, parametrised over every directory in industries/.

One suite, every industry: a contract change that breaks a sibling industry
surfaces immediately. Stub industries (README only, no blueprint) are skipped.
Known gaps in legacy industries are listed per-industry in KNOWN_GAPS rather
than loosening the contract for everyone.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
INDUSTRY_ROOT = ROOT / "industries"

# Documented debt, not contract exceptions. Remove an entry when the gap is fixed.
KNOWN_GAPS: dict[str, set[str]] = {
    "control-industry": {"mmd", "selfcheck", "seeded_reference"},
    "healthcare": {"selfcheck"},
}

REQUIRED_FILES = {"README.md", "agent_blueprint.json", "tools.json",
                  "tool_server.py", "requirements.txt"}
ALLOWED_EXTRA = {"agent_blueprint.mmd", "db", "system-prompts", "docs", "tasks",
                 "__pycache__", ".DS_Store"}


def _industry_params():
    params = []
    for d in sorted(INDUSTRY_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "agent_blueprint.json").is_file():
            params.append(pytest.param(
                d.name, marks=pytest.mark.skip(reason=f"{d.name} is a stub")))
        else:
            params.append(pytest.param(d.name))
    return params


industry = pytest.mark.parametrize("industry", _industry_params())


def _gaps(name: str) -> set[str]:
    return KNOWN_GAPS.get(name, set())


def _blueprint(name: str) -> dict:
    return json.loads((INDUSTRY_ROOT / name / "agent_blueprint.json").read_text())


def _tools(name: str) -> list[dict]:
    return json.loads((INDUSTRY_ROOT / name / "tools.json").read_text())["tools"]


def _tool_flags(name: str) -> dict[str, dict]:
    flags: dict[str, dict] = {}
    for agent in _blueprint(name)["agents"]:
        for t in agent["tools"]:
            flags.setdefault(t["name"], t)
    return flags


def _load_tool_server(name: str):
    """Import tool_server.py under a unique module name with a temp DB."""
    original = os.environ.get("MIVAS_DB_PATH")
    original_shared = os.environ.get("MIVAS_DB_SHARED")
    tmp = tempfile.mkdtemp(prefix=f"mivas-contract-{name}-")
    os.environ["MIVAS_DB_PATH"] = str(Path(tmp) / "runtime.db")
    os.environ["MIVAS_DB_SHARED"] = "1"
    try:
        mod_name = f"contract_tool_server_{name.replace('-', '_')}"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(
            mod_name, INDUSTRY_ROOT / name / "tool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if original is None:
            os.environ.pop("MIVAS_DB_PATH", None)
        else:
            os.environ["MIVAS_DB_PATH"] = original
        if original_shared is None:
            os.environ.pop("MIVAS_DB_SHARED", None)
        else:
            os.environ["MIVAS_DB_SHARED"] = original_shared


@industry
def test_structure(industry: str) -> None:
    d = INDUSTRY_ROOT / industry
    entries = {p.name for p in d.iterdir()}
    missing = REQUIRED_FILES - entries
    assert not missing, f"missing {sorted(missing)}"
    if "mmd" not in _gaps(industry):
        assert (d / "agent_blueprint.mmd").is_file()
    unexpected = entries - REQUIRED_FILES - ALLOWED_EXTRA
    assert not unexpected, f"unexpected entries {sorted(unexpected)}"
    assert (d / "db" / "schema.sql").is_file()
    assert (d / "db" / "seed.sql").is_file()
    prompts = list((d / "system-prompts").iterdir())
    md = [p for p in prompts if p.suffix == ".md"]
    assert md, "system-prompts/ must contain at least one .md"
    assert all(p.suffix == ".md" or p.name == "__pycache__" for p in prompts)


@industry
def test_database_applies(industry: str) -> None:
    d = INDUSTRY_ROOT / industry / "db"
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript((d / "schema.sql").read_text())
        seed = (d / "seed.sql").read_text().strip()
        if seed:
            conn.executescript(seed)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        assert tables, "schema created no tables"
        if "seeded_reference" not in _gaps(industry):
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in tables}
            assert any(counts.values()), f"no seeded reference data: {counts}"
    finally:
        conn.close()


@industry
def test_server_boots_and_serves_fixtures(industry: str) -> None:
    module = _load_tool_server(industry)
    with TestClient(module.app) as client:
        assert client.get("/health").status_code == 200
        state = client.get("/state", headers={"X-Mivas-Call-Id": "contract"})
        assert state.status_code == 200
        assert isinstance(state.json(), dict)


@industry
def test_dispatch_parity_both_directions(industry: str) -> None:
    flags = _tool_flags(industry)
    declared = {t["name"] for t in _tools(industry)
                if t["name"] != "end_call"
                and not flags.get(t["name"], {}).get("handoff")}
    module = _load_tool_server(industry)
    assert hasattr(module, "DISPATCH"), "tool_server must define DISPATCH"
    dispatch = set(module.DISPATCH)
    assert declared == dispatch, sorted(declared ^ dispatch)
    with TestClient(module.app) as client:
        assert client.post("/tools/not_a_real_tool",
                           json={"arguments": {}}).status_code == 404
        representative = sorted(declared)[0]
        resp = client.post(f"/tools/{representative}", json={"arguments": {}})
        assert resp.status_code == 200
        body = resp.json()
        assert "ok" in body or "success" in body, body


@industry
def test_selfcheck_passes(industry: str) -> None:
    """Twice against the SAME MIVAS_DB_PATH: a selfcheck that mutates seeded rows
    must not inherit its own leftovers from the previous run."""
    if "selfcheck" in _gaps(industry):
        pytest.skip(f"{industry} has no --selfcheck yet (KNOWN_GAPS)")
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "MIVAS_DB_PATH": str(Path(tmp) / "runtime.db"),
            "PYTHONPATH": str(ROOT / "runtime")
            + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
        }
        for run in (1, 2):
            proc = subprocess.run(
                [sys.executable, str(INDUSTRY_ROOT / industry / "tool_server.py"),
                 "--selfcheck"],
                capture_output=True, text=True, env=env, timeout=120)
            assert proc.returncode == 0, f"run {run}: " + proc.stdout + proc.stderr


@industry
def test_blueprint_prompts_and_tools(industry: str) -> None:
    d = INDUSTRY_ROOT / industry
    bp = _blueprint(industry)
    tool_names = {t["name"] for t in _tools(industry)}
    agent_names = {a["name"] for a in bp["agents"]}
    referenced = []
    for agent in bp["agents"]:
        prompt = d / agent["system_prompt"]
        assert prompt.is_file(), f"{agent['name']}: {agent['system_prompt']} missing"
        referenced.append(prompt.resolve())
        for t in agent["tools"]:
            if not (t.get("handoff") and "handoff_tools_in_catalog" in _gaps(industry)):
                assert t["name"] in tool_names, \
                    f"{agent['name']}: {t['name']} not in tools.json"
            if t.get("handoff"):
                assert t["handoff_to"] in agent_names
    on_disk = sorted(p.resolve() for p in (d / "system-prompts").glob("*.md"))
    if "orphan_prompts" in _gaps(industry):
        assert set(referenced) <= set(on_disk)
    else:
        assert sorted(referenced) == on_disk, "every prompt file referenced exactly once"


@industry
def test_every_agent_reachable(industry: str) -> None:
    bp = _blueprint(industry)
    agents = {a["name"]: a for a in bp["agents"]}
    seen, frontier = set(), [bp["agents"][0]["name"]]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        for t in agents[name]["tools"]:
            if t.get("handoff"):
                frontier.append(t["handoff_to"])
    unreachable = set(agents) - seen
    assert not unreachable, f"unreachable agents: {sorted(unreachable)}"


_EDGE = re.compile(r"^\s*(\w[\w-]*)\s*-->\s*\|(.+?)\|\s*(\w[\w-]*)")


@industry
def test_mermaid_matches_blueprint(industry: str) -> None:
    if "mmd" in _gaps(industry):
        pytest.skip(f"{industry} has no .mmd yet (KNOWN_GAPS)")
    bp = _blueprint(industry)
    expected = {(a["name"], t["name"], t["handoff_to"])
                for a in bp["agents"] for t in a["tools"] if t.get("handoff")}
    found = set()
    for line in (INDUSTRY_ROOT / industry / "agent_blueprint.mmd").read_text().splitlines():
        m = _EDGE.match(line)
        if not m:
            continue
        src, label, dst = m.groups()
        tool = label.strip().strip('"').split("(")[0].strip()
        if tool.startswith("transfer_to_"):
            found.add((src, tool, dst))
    assert found == expected, (
        f"mmd/blueprint edge drift — only in mmd: {sorted(found - expected)}, "
        f"only in blueprint: {sorted(expected - found)}")


@industry
def test_harness_wiring_check(industry: str) -> None:
    """run.py --check equivalent: the openai harness builds agents from this
    blueprint. Skipped when the openai agents SDK is not installed."""
    if importlib.util.find_spec("agents") is None:
        pytest.skip("openai agents SDK not installed")
    proc = subprocess.run(
        [sys.executable,
         str(ROOT / "voice-agent-harnesses" / "openai" / "realtime-2.1" / "agent.py"),
         industry, "--check"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ok {industry}" in proc.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
