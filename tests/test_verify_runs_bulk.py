"""Hole-fill and Postgres grouping for bulk scoring."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "verify_runs_bulk", ROOT / "scripts" / "verify_runs_bulk.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bulk = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_runs_bulk"] = bulk
_SPEC.loader.exec_module(bulk)


def test_fill_holes_keeps_completed_and_replaces_by_case_key() -> None:
    primary = [
        {"result_id": "1", "case_key": "C1-E1", "status": "COMPLETED", "passed": True},
        {"result_id": "2", "case_key": "C1-E1", "status": "NO_ANSWER", "passed": False},
        {"result_id": "3", "case_key": "C2-H1", "status": "DISPATCHED", "passed": False},
        {"result_id": "4", "case_key": "C3-H4", "status": "CONVERSATION_ENDED", "passed": False},
    ]
    retries = [[
        {"result_id": "9", "case_key": "C1-E1", "status": "COMPLETED", "passed": True},
        {"result_id": "8", "case_key": "C2-H1", "status": "COMPLETED", "passed": True},
        {"result_id": "7", "case_key": "C3-H4", "status": "COMPLETED", "passed": True},
    ]]
    out = bulk.fill_holes(primary, retries)
    assert [r["result_id"] for r in out] == ["1", "9", "8", "7"]
    assert out[3]["status"] == "COMPLETED"


def test_fill_holes_replaces_completed_void_extraction() -> None:
    primary = [
        {
            "result_id": "1",
            "case_key": "C1-E1",
            "digital_human_id": 10,
            "status": "COMPLETED",
            "void_reason": "tool list empty while tools were expected — extraction did not land",
        },
        {
            "result_id": "2",
            "case_key": "C1-E1",
            "digital_human_id": 11,
            "status": "COMPLETED",
        },
    ]
    retries = [[
        {
            "result_id": "9",
            "case_key": "C1-E1",
            "digital_human_id": 10,
            "status": "COMPLETED",
        },
    ]]
    out = bulk.fill_holes(primary, retries)
    assert [r["result_id"] for r in out] == ["9", "2"]


def test_group_tool_calls_sorts_actuals() -> None:
    groups = bulk.group_tool_calls(
        [
            {"name": "b", "parameters": {"x": 2}, "start_offset_ms": 20},
            {"name": "a", "parameters": {"x": 1}, "start_offset_ms": 10},
        ],
        [{"name": "a", "parameters": {}, "output": None}],
    )
    by_name = {g["name"]: g for g in groups}
    assert by_name["a"]["expected"]
    assert by_name["a"]["actual"][0]["start_offset_ms"] == 10
    assert by_name["b"]["actual"][0]["parameters"] == {"x": 2}
