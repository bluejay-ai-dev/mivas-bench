"""Bland pathway_graph: control-industry stays two-agent; healthcare compiles every agent."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "voice-agent-harnesses" / "bland" / "harness.py"


def _load():
    sys.path.insert(0, str(HARNESS.parent))
    try:
        spec = importlib.util.spec_from_file_location("bland_harness", HARNESS)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HARNESS.parent))


def test_control_industry_graph_unchanged():
    bland = _load()
    bp = bland.load_blueprint(ROOT / "industries" / "control-industry")
    graph = bland.pathway_graph(bp, "https://example.test")
    nodes = {n["id"]: n for n in graph["nodes"]}
    hops = {(e["source"], e["target"]) for e in graph["edges"]}
    assert hops == {
        ("receptionist", "handoff"),
        ("scheduler", "book"),
        ("receptionist", "end_receptionist"),
        ("scheduler", "end_scheduler"),
    }
    assert nodes["handoff"]["data"]["url"].endswith("/tool/handoff_to_scheduler")
    assert all(e.get("description") and "data" not in e for e in graph["edges"])


def test_healthcare_graph_has_every_agent_and_handoff():
    bland = _load()
    bp = bland.load_blueprint(ROOT / "industries" / "healthcare")
    graph = bland.pathway_graph(bp, "https://example.test")
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes[bp["start"]]["data"]["isStart"]
    for name, agent in bp["agents"].items():
        assert nodes[name]["type"] == "Default"
        assert nodes[f"end_{name}"]["type"] == "End Call"
        prompt = nodes[name]["data"]["prompt"]
        assert not any(tool in prompt for tool in bp["catalog"])
        for tool in agent["tools"]:
            if not tool.get("handoff"):
                continue
            nid = f"{name}__{tool['name']}"
            assert nodes[nid]["type"] == "Webhook"
            assert nodes[nid]["data"]["url"] == (
                f"https://example.test/tool/{tool['name']}"
            )
            assert nodes[nid]["data"]["responsePathways"][0][3]["id"] == tool["handoff_to"]
    hops = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("reception", "reception__transfer_to_identity") in hops
    assert ("reception", "reception__transfer_to_coverage") in hops
    assert ("reception", "reception__transfer_to_cosmetic") in hops
    assert ("reception", "reception__transfer_to_scheduling") in hops
    assert all(e.get("description") and "data" not in e for e in graph["edges"])
    for edge in graph["edges"]:
        for tool_name in bp["catalog"]:
            assert tool_name not in edge["description"], (tool_name, edge["description"])
    reception_targets = [
        e["target"] for e in graph["edges"] if e["source"] == "reception"
    ]
    assert reception_targets[-1] == "end_reception"
    assert "reception__transfer_to_identity" in reception_targets[:-1]
    assert "reception__classify_visit_request" not in reception_targets
    assert "reception__list_locations" not in reception_targets
    assert ("scheduling", "scheduling__classify_visit_request") in hops
    assert ("identity", "identity__transfer_to_billing") in hops
