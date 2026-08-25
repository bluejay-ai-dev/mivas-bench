"""Replay expected tool calls onto a fresh seed and dump GET /state."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "expected_final_state", ROOT / "verifiers" / "expected_final_state.py"
)
assert _SPEC is not None and _SPEC.loader is not None
efs = importlib.util.module_from_spec(_SPEC)
sys.modules["expected_final_state"] = efs
_SPEC.loader.exec_module(efs)

call_id_for = efs.call_id_for
canonical_state = efs.canonical_state
case_key = efs.case_key
is_harness_native = efs.is_harness_native
load_tool_server = efs.load_tool_server
replay_case = efs.replay_case
states_match = efs.states_match
tool_flags = efs.tool_flags
write_industry = efs.write_industry

CONTROL_DH = {
    "id": 1,
    "name": "C1-E1 Booker",
    "test_name": "C1-E1: schedule a repair",
    "traits": [{"trait_name": "case_key", "trait_data_type": "STRING", "value": "C1-E1"}],
    "expected_tool_calls": [
        {"name": "schedule_appointment", "parameters": {"date": "08/15/2026"}},
        {"name": "end_call", "parameters": None},
    ],
}

SUPPORT_READ = {
    "id": 2,
    "name": "Fee lookup",
    "test_name": "T1-E1: Haul-away with a delivery",
    "traits": [{"trait_name": "case_key", "trait_data_type": "STRING", "value": "T1-E1"}],
    "expected_tool_calls": [
        {
            "name": "get_fee",
            "parameters": {"fee": "haul away with delivery"},
        },
        {"name": "transfer_to_orders", "parameters": None},
    ],
}

SUPPORT_WRITE = {
    "id": 3,
    "name": "Escalate",
    "test_name": "R-E1: Ask for a person",
    "traits": [{"trait_name": "case_key", "trait_data_type": "STRING", "value": "R-E1"}],
    "expected_tool_calls": [
        {
            "name": "escalate_to_human",
            "parameters": {"reason_code": "caller_request"},
        },
    ],
}


def test_case_key_prefers_trait() -> None:
    assert case_key(SUPPORT_READ) == "T1-E1"
    assert case_key({"test_name": "T2-H1: Return a phone", "name": "x"}) == "T2-H1"
    assert case_key({"id": 99, "name": "nobody"}) == "dh-99"


def test_canonical_state_drops_created_at() -> None:
    raw = {"appointments": [{"id": 1, "date": "08/15/2026", "created_at": "now"}]}
    assert canonical_state(raw) == {"appointments": [{"id": 1, "date": "08/15/2026"}]}
    assert states_match(raw, {"appointments": [{"id": 1, "date": "08/15/2026", "created_at": "later"}]})


def test_control_replay_books_and_skips_end_call() -> None:
    flags = tool_flags("control-industry")
    assert is_harness_native("end_call", flags)
    with load_tool_server("control-industry") as module, TestClient(module.app) as client:
        seed = client.get("/state", headers={"X-Mivas-Call-Id": "seed"}).json()
        result = replay_case(client, CONTROL_DH, flags)
    assert result["skipped"] == ["end_call"]
    assert [row["name"] for row in result["replayed"]] == ["schedule_appointment"]
    assert result["replayed"][0]["ok"] is True
    assert seed["appointments"] == []
    dates = [row["date"] for row in result["state"]["appointments"]]
    assert dates == ["08/15/2026"]
    assert "created_at" not in result["state"]["appointments"][0]


def test_support_read_leaves_write_tables_empty() -> None:
    flags = tool_flags("customer-support")
    assert is_harness_native("transfer_to_orders", flags)
    with load_tool_server("customer-support") as module, TestClient(module.app) as client:
        result = replay_case(client, SUPPORT_READ, flags)
    assert result["skipped"] == ["transfer_to_orders"]
    assert result["replayed"][0]["ok"] is True
    assert result["state"]["escalations"] == []
    assert result["state"]["rmas"] == []
    assert result["state"]["customers"], "seed customers must be in the dump"


def test_human_transfer_is_session_but_still_replayed() -> None:
    """Human-transfer tools end the call but still POST, so expected state includes them."""
    for industry, tool in (
        ("customer-support", "escalate_to_human"),
        ("legal", "escalate_to_human"),
        ("healthcare", "transfer_to_human"),
    ):
        flags = tool_flags(industry)
        assert flags[tool].get("session") is True, industry
        assert not is_harness_native(tool, flags), industry
        assert is_harness_native("end_call", flags), industry


def test_support_write_appends_escalation() -> None:
    flags = tool_flags("customer-support")
    assert flags["escalate_to_human"].get("session") is True
    assert not is_harness_native("escalate_to_human", flags)
    with load_tool_server("customer-support") as module, TestClient(module.app) as client:
        result = replay_case(client, SUPPORT_WRITE, flags)
    assert result["skipped"] == []
    assert result["state"]["escalations"] == [
        {"id": 1, "customer_id": "", "reason_code": "caller_request"},
    ]


def test_write_industry_emits_one_file_per_case(tmp_path: Path) -> None:
    manifest = write_industry("control-industry", [CONTROL_DH], tmp_path)
    path = tmp_path / "control-industry" / "C1-E1.final.json"
    assert path.is_file()
    assert manifest["count"] == 1
    dumped = json.loads(path.read_text())
    assert dumped["appointments"][0]["date"] == "08/15/2026"


def test_healthcare_expected_calls_replay() -> None:
    """Every healthcare expected industry call must succeed against a fresh seed,
    except the one case that declares a payer failure."""
    tasks_mod = importlib.util.spec_from_file_location(
        "tasks_to_digital_humans", ROOT / "scripts" / "tasks_to_digital_humans.py"
    )
    assert tasks_mod is not None and tasks_mod.loader is not None
    tdh = importlib.util.module_from_spec(tasks_mod)
    sys.modules["tasks_to_digital_humans"] = tdh  # dataclass ns lookup needs it
    tasks_mod.loader.exec_module(tdh)
    humans = tdh.build("healthcare")
    flags = tool_flags("healthcare")
    with load_tool_server("healthcare") as module, TestClient(module.app) as client:
        for dh in humans:
            result = replay_case(client, dh, flags)
            expected = [
                call for call in dh.get("expected_tool_calls") or []
                if call.get("name") and not is_harness_native(call["name"], flags)
            ]
            assert len(result["replayed"]) == len(expected), result["case_key"]
            for replayed, call in zip(result["replayed"], expected, strict=True):
                assert replayed["status_code"] == 200, (
                    f"{result['case_key']}/{replayed['name']}: {replayed}"
                )
                want = call.get("output") or {}
                if want.get("ok") is False:
                    assert replayed["ok"] is False, (result["case_key"], replayed)
                    assert replayed["error_code"] == want["error_code"], (
                        result["case_key"], replayed
                    )
                else:
                    assert replayed["ok"] is True, (
                        f"{result['case_key']}/{replayed['name']} "
                        f"args={replayed['arguments']} → {replayed}"
                    )


def test_isolated_call_ids_do_not_share_writes() -> None:
    flags = tool_flags("control-industry")
    other = {
        **CONTROL_DH,
        "id": 9,
        "traits": [{"trait_name": "case_key", "trait_data_type": "STRING", "value": "C1-E2"}],
        "expected_tool_calls": [
            {"name": "schedule_appointment", "parameters": {"date": "09/01/2026"}},
        ],
    }
    with load_tool_server("control-industry") as module, TestClient(module.app) as client:
        a = replay_case(client, CONTROL_DH, flags)
        b = replay_case(client, other, flags)
    assert [row["date"] for row in a["state"]["appointments"]] == ["08/15/2026"]
    assert [row["date"] for row in b["state"]["appointments"]] == ["09/01/2026"]
    assert call_id_for("C1-E1") != call_id_for("C1-E2")
