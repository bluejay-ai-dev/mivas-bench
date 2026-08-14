"""ElevenLabs Convai agent payloads for healthcare-style blueprints."""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "voice-agent-harnesses" / "elevenlabs" / "harness.py"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("elevenlabs_harness", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


def test_client_tool_properties_always_have_descriptions() -> None:
    module = _load()
    tools = json.loads((ROOT / "industries" / "healthcare" / "tools.json").read_text())["tools"]
    for spec in tools:
        converted = module._client_tool(spec)
        for key, prop in converted["parameters"]["properties"].items():
            assert prop.get("description"), (spec["name"], key, prop)
            assert prop.get("type")


def test_healthcare_reception_collapses_handoffs_into_one_transfer() -> None:
    module = _load()
    bp = module.load_blueprint(ROOT / "industries" / "healthcare")
    ids = {name: f"el_{name}" for name in bp["agents"]}
    tools = module._build_tools(bp["agents"]["reception"], bp, agent_ids=ids)
    transfers = [t for t in tools if t.get("name") == "transfer_to_agent"]
    assert len(transfers) == 1, tools
    rows = transfers[0]["params"]["transfers"]
    dest = {row["agent_id"] for row in rows}
    assert dest == {
        "el_identity",
        "el_scheduling",
        "el_coverage",
        "el_cosmetic",
    }
    assert all(row.get("transfer_message") == "One moment." for row in rows)
    assert "transfer_to_human" not in dest
    assert any(t.get("name") == "classify_visit_request" for t in tools)


def test_healthcare_handoffs_are_downstream_only() -> None:
    """identity→scheduling stays; scheduling→identity is dropped (2-cycle)."""
    module = _load()
    bp = module.load_blueprint(ROOT / "industries" / "healthcare")
    ids = {name: f"el_{name}" for name in bp["agents"]}
    sched = module._build_tools(bp["agents"]["scheduling"], bp, agent_ids=ids)
    sched_xfer = [t for t in sched if t.get("name") == "transfer_to_agent"]
    assert sched_xfer, sched
    sched_dest = {row["agent_id"] for row in sched_xfer[0]["params"]["transfers"]}
    assert "el_identity" not in sched_dest
    assert "el_coverage" in sched_dest or "el_cosmetic" in sched_dest
    ident = module._build_tools(bp["agents"]["identity"], bp, agent_ids=ids)
    ident_xfer = [t for t in ident if t.get("name") == "transfer_to_agent"]
    ident_dest = {row["agent_id"] for row in ident_xfer[0]["params"]["transfers"]}
    assert "el_scheduling" in ident_dest
    assert "el_billing" in ident_dest


def test_healthcare_blueprint_greeting_is_loaded() -> None:
    module = _load()
    bp = module.load_blueprint(ROOT / "industries" / "healthcare")
    assert "Straus" in bp["greeting"]
    payload = module._agent_payload(
        name="x",
        prompt="hi",
        first_message="",
        tools=[],
        voice_id="voice",
    )
    assert "first_message" not in payload["conversation_config"]["agent"]
    assert payload["conversation_config"]["turn"]["turn_eagerness"] == "normal"
    assert payload["conversation_config"]["turn"]["turn_timeout"] == 7


def test_reception_payload_includes_greeting_first_message() -> None:
    module = _load()
    bp = module.load_blueprint(ROOT / "industries" / "healthcare")
    greeting = module._greeting(bp)
    payload = module._agent_payload(
        name="reception",
        prompt="hi",
        first_message=greeting,
        tools=[],
        voice_id="voice",
    )
    assert payload["conversation_config"]["agent"]["first_message"] == greeting
    assert "Straus" in greeting


def test_chirp_forwards_user_pcm_only_after_ready_and_speech() -> None:
    path = ROOT / "voice-agent-harnesses" / "elevenlabs" / "adapters" / "chirp.py"
    family = path.parent.parent
    sys.path.insert(0, str(family))
    for cached in ("harness", "pcm", "report", "adapters", "adapters.chirp"):
        sys.modules.pop(cached, None)
    try:
        spec = importlib.util.spec_from_file_location("elevenlabs_chirp", path)
        chirp = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(chirp)
    finally:
        sys.path.remove(str(family))
    assert chirp._forward_user_pcm(ready=False, listening=True) is False
    assert chirp._forward_user_pcm(ready=True, listening=False) is False
    assert chirp._forward_user_pcm(ready=True, listening=True) is True
    src = inspect.getsource(chirp.main)
    assert src.index("ensure_agents") < src.index("asyncio.run")
