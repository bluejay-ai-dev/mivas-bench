"""Retell create-retell-llm payload for multi-agent industries."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "retell"
if str(FAMILY) not in sys.path:
    sys.path.insert(0, str(FAMILY))

from harness import load_blueprint, _custom_tool, _llm_payload  # noqa: E402

PUBLIC = "https://example.test"


def _array_params(payload: dict) -> list[tuple[str, str, str, dict]]:
    found = []
    for state in payload["states"]:
        for tool in state.get("tools") or []:
            props = (tool.get("parameters") or {}).get("properties") or {}
            for key, schema in props.items():
                if schema.get("type") == "array":
                    found.append((state["name"], tool["name"], key, schema))
    return found


def test_healthcare_array_params_include_items() -> None:
    payload = _llm_payload(load_blueprint(ROOT / "industries" / "healthcare"), PUBLIC)
    arrays = _array_params(payload)
    assert arrays, "healthcare find_slots/join_waitlist must still be array-typed"
    for state, tool, key, schema in arrays:
        assert "items" in schema, f"{state}.{tool}.{key} missing items"


def test_healthcare_reception_keeps_every_handoff_edge() -> None:
    payload = _llm_payload(load_blueprint(ROOT / "industries" / "healthcare"), PUBLIC)
    reception = next(s for s in payload["states"] if s["name"] == "reception")
    dests = {e["destination_state_name"] for e in reception["edges"]}
    assert dests == {"identity", "scheduling", "coverage", "cosmetic"}


def test_control_industry_still_one_scheduler_edge() -> None:
    payload = _llm_payload(load_blueprint(ROOT / "industries" / "control-industry"), PUBLIC)
    recv = next(s for s in payload["states"] if s["name"] == "receptionist")
    assert [e["destination_state_name"] for e in recv["edges"]] == ["scheduler"]


def test_custom_tool_fills_missing_array_items() -> None:
    tool = _custom_tool(
        {
            "name": "find_slots",
            "description": "x",
            "inputSchema": {
                "type": "object",
                "properties": {"location_ids": {"type": "array"}},
            },
        },
        PUBLIC,
    )
    assert tool["parameters"]["properties"]["location_ids"] == {
        "type": "array",
        "items": {"type": "string"},
    }
