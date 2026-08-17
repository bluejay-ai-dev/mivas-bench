"""Prompt-adherence substring + state checks (no live API)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "verify_task_run", ROOT / "scripts" / "verify_task_run.py"
)
assert _SPEC is not None and _SPEC.loader is not None
vtr = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_task_run"] = vtr
_SPEC.loader.exec_module(vtr)


def test_empty_substrs_pass() -> None:
    assert vtr.match_agent_substrs("anything", []) == {
        "passed": True, "missing": [], "found": [],
    }
    assert vtr.match_agent_substrs("", None)["passed"] is True


def test_substring_names_casefold_verbatim_stays_exact() -> None:
    text = "I can confirm Alice Romano is on file. The deductible or copay is listed."
    ok = vtr.match_agent_substrs(text, ["Alice Romano", "deductible or copay"])
    assert ok["passed"] is True
    assert ok["missing"] == []

    miss = vtr.match_agent_substrs(text, ["Alice Romano", "did I get that right?"])
    assert miss["passed"] is False
    assert miss["missing"] == ["did I get that right?"]

    case = vtr.match_agent_substrs(text, ["alice romano"])
    assert case["passed"] is True

    verbatim = vtr.match_agent_substrs(
        "Sorry, I cannot help with that.",
        ["Sorry, I can't help with that."],
    )
    assert verbatim["passed"] is False


def test_agent_transcript_ignores_caller_turns() -> None:
    lines = [
        "USER: Alice Romano",
        "CALLER: deductible or copay",
        "AGENT: Thanks for calling Straus.",
        "ASSISTANT: I can look that up.",
    ]
    text = vtr.agent_transcript(lines)
    assert "Thanks for calling Straus." in text
    assert "I can look that up." in text
    assert "Alice Romano" not in text
    assert "deductible or copay" not in text
    leaked = vtr.match_agent_substrs(text, ["Alice Romano"])
    assert leaked["passed"] is False


def test_states_match_via_canonical_state() -> None:
    expected = {"patients": [{"id": "p1", "created_at": "now"}], "waitlist": []}
    actual = {"patients": [{"id": "p1", "created_at": "later"}], "waitlist": []}
    assert vtr.canonical_state(expected) == {"patients": [{"id": "p1"}], "waitlist": []}
    assert vtr.states_match(expected, actual) is True
    assert vtr.states_match(expected, {"patients": [], "waitlist": []}) is False


def test_office_states_ignore_tool_events_and_description() -> None:
    expected = {
        "patients": [{"id": "p1", "phone_e164": "+17185550191"}],
        "appointments": [{"id": 4, "status": "booked", "description": "fixture note"}],
        "waitlist": [],
        "locations": [{"id": "loc_park_ave"}],
        "tool_events": [{"kind": "transfer", "payload": {"context_summary": "R-E1: Ask for a person"}}],
    }
    actual = {
        "patients": [{"id": "p1", "phone_e164": "718-555-0191"}],
        "appointments": [{"id": 4, "status": "booked", "description": "live note"}],
        "waitlist": [],
        "locations": [{"id": "loc_brooklyn_heights"}],
        "tool_events": [{"kind": "transfer", "payload": {"context_summary": "Caller wants a person"}}],
    }
    assert vtr.office_states_match(expected, actual) is True
    actual["appointments"] = [{"id": 4, "status": "cancelled", "description": "live note"}]
    assert vtr.office_states_match(expected, actual) is False


def test_verify_result_ignores_tool_events_in_state() -> None:
    task = {
        "prompt_adherence_substrs": [],
        "exp_db_state": {
            "patients": [{"id": "p1", "phone_e164": "+17185550191"}],
            "appointments": [],
            "waitlist": [],
            "tool_events": [{"kind": "transfer", "payload": {"context_summary": "R-E1"}}],
        },
    }
    result = {"transcript": [{"role": "agent", "text": "hello"}]}
    actual = {
        "patients": [{"id": "p1", "phone_e164": "718-555-0191"}],
        "appointments": [],
        "waitlist": [],
        "locations": [{"id": "loc_other"}],
        "tool_events": [{"kind": "transfer", "payload": {"context_summary": "different"}}],
    }
    out = vtr.verify_result(result, task, actual)
    assert out["state"]["passed"] is True
    assert out["passed"] is True


def test_verify_result_empty_substrs_and_matching_state() -> None:
    task = {"prompt_adherence_substrs": [], "exp_db_state": {"waitlist": []}}
    result = {"transcript": [{"role": "agent", "text": "hello"}]}
    out = vtr.verify_result(result, task, {"waitlist": [], "created_at": "x"})
    assert out["substr"]["passed"] is True
    assert out["state"]["passed"] is True
    assert out["passed"] is True


def test_verify_result_skips_state_when_dump_missing() -> None:
    task = {"prompt_adherence_substrs": ["hello"], "exp_db_state": {"waitlist": []}}
    result = {"messages": [{"role": "assistant", "content": "hello there"}]}
    out = vtr.verify_result(result, task, None, state_note="S3 not configured")
    assert out["substr"]["passed"] is True
    assert out["state"]["skipped"] is True
    assert out["state"]["note"] == "S3 not configured"
    assert out["passed"] is True


def test_tool_call_adherence_requires_every_expected_name() -> None:
    empty = vtr.tool_call_adherence([], [{"name": "end_call"}])
    assert empty["passed"] is True
    assert empty["score"] == 1.0

    ok = vtr.tool_call_adherence(
        [{"name": "transfer_to_coverage"}, {"name": "check_plan_accepted"}],
        [
            {"name": "transfer_to_coverage"},
            {"name": "search_practice_kb"},
            {"name": "check_plan_accepted"},
        ],
    )
    assert ok["passed"] is True
    assert ok["missing"] == []

    miss = vtr.tool_call_adherence(
        [{"name": "transfer_to_coverage"}, {"name": "check_plan_accepted"}],
        [{"name": "transfer_to_coverage"}],
    )
    assert miss["passed"] is False
    assert miss["missing"] == ["check_plan_accepted"]
    assert miss["score"] == 0.5


def test_handoff_adherence_is_in_order_subsequence() -> None:
    exact = vtr.handoff_adherence(
        ["transfer_to_identity", "transfer_to_billing"],
        ["transfer_to_identity", "transfer_to_billing"],
    )
    assert exact["passed"] is True
    assert exact["verdict"] == "exact"

    extras = vtr.handoff_adherence(
        ["transfer_to_scheduling"],
        ["transfer_to_coverage", "transfer_to_scheduling"],
    )
    assert extras["passed"] is True
    assert extras["verdict"] == "in_order_with_extras"

    incomplete = vtr.handoff_adherence(
        ["transfer_to_identity", "transfer_to_billing"],
        ["transfer_to_identity"],
    )
    assert incomplete["passed"] is False
    assert incomplete["verdict"] == "incomplete"

    none = vtr.handoff_adherence([], [])
    assert none["passed"] is True
    unexpected = vtr.handoff_adherence([], ["transfer_to_human"])
    assert unexpected["passed"] is False
    assert unexpected["verdict"] == "unexpected_handoffs"


def test_verify_result_includes_call_and_handoff() -> None:
    task = {
        "prompt_adherence_substrs": ["hello"],
        "exp_db_state": {"waitlist": []},
        "exp_tool_calls": [{"name": "list_locations"}],
        "exp_handoff_path": [],
    }
    result = {
        "messages": [{"role": "assistant", "content": "hello there"}],
        "tool_calls": [{"name": "list_locations", "actual": [{"ok": True}]}],
    }
    out = vtr.verify_result(result, task, {"waitlist": []})
    assert out["call"]["passed"] is True
    assert out["handoff"]["passed"] is True
    assert out["passed"] is True


def test_case_key_from_trait_or_test_name() -> None:
    assert vtr.case_key_from_dh({
        "traits": [{"trait_name": "case_key", "value": "C1-E1-BG"}],
        "test_name": "ignored",
    }) == "C1-E1-BG"
    assert vtr.case_key_from_dh({"test_name": "C5-H3: Itemized balance"}) == "C5-H3"


def test_substring_folds_dates_phones_911_money_floors() -> None:
    text = (
        "Jordan Lee, April 12, 1990 — yes. Please call 9-1-1. "
        "Park Avenue is the fourth floor. The deposit is one hundred twenty-five dollars. "
        "The number is 718-555-0191."
    )
    ok = vtr.match_agent_substrs(text, [
        "April twelfth, nineteen ninety",
        "call 911",
        "4th floor",
        "7185550191",
    ])
    assert ok["passed"] is True
    spoken = vtr.match_agent_substrs(
        "March 22, 2016. June 30, 1972.",
        [
            "March twenty-second, two thousand sixteen",
            "June thirtieth, nineteen seventy-two",
        ],
    )
    assert spoken["passed"] is True
    money = vtr.match_agent_substrs(
        "A one hundred twenty-five dollar deposit may apply.",
        ["$125"],
    )
    assert money["passed"] is True


def test_substring_does_not_fold_verbatim_standing_lines() -> None:
    close = vtr.match_agent_substrs(
        "I cannot take a card number over the phone.",
        ["I can't take a card number by voice"],
    )
    assert close["passed"] is False
    exact = vtr.match_agent_substrs(
        "I can't take a card number by voice, I'll send a link.",
        ["I can't take a card number by voice"],
    )
    assert exact["passed"] is True


def test_tool_call_adherence_ignores_prose_args() -> None:
    schemas = vtr.load_tool_schemas("healthcare")
    expected = [{
        "name": "transfer_to_human",
        "parameters": {
            "destination": "patient_support_center",
            "reason": "caller_request",
        },
        "output": {"ok": True},
    }]
    actual = [{
        "name": "transfer_to_human",
        "parameters": {
            "destination": "patient_support_center",
            "reason": "caller_request",
        },
        "ok": True,
    }]
    ok = vtr.tool_call_adherence(expected, actual, schemas=schemas)
    assert ok["passed"] is True

    wrong_dest = [{
        "name": "transfer_to_human",
        "parameters": {
            "destination": "billing_team",
            "reason": "caller_request",
        },
        "ok": True,
    }]
    miss = vtr.tool_call_adherence(expected, wrong_dest, schemas=schemas)
    assert miss["passed"] is False

    spoken_reason = [{
        "name": "transfer_to_human",
        "parameters": {
            "destination": "patient_support_center",
            "reason": "Caller requested a human representative.",
        },
        "ok": True,
    }]
    enum_miss = vtr.tool_call_adherence(expected, spoken_reason, schemas=schemas)
    assert enum_miss["passed"] is False


def test_tool_call_adherence_name_only_when_actual_args_missing() -> None:
    expected = [{
        "name": "transfer_to_human",
        "parameters": {"destination": "patient_support_center", "reason": "caller_request"},
    }]
    actual = [{"name": "transfer_to_human", "parameters": {}}]
    assert vtr.tool_call_adherence(expected, actual)["passed"] is True


def test_tool_call_adherence_ignores_harness_routing_args() -> None:
    schemas = vtr.load_tool_schemas("healthcare")
    expected = [{"name": "transfer_to_coverage"}]
    actual = [{
        "name": "transfer_to_coverage",
        "parameters": {"to": "coverage", "from": "reception"},
    }]
    assert vtr.tool_call_adherence(expected, actual, schemas=schemas)["passed"] is True


def test_tool_call_adherence_aliases_office_names() -> None:
    schemas = vtr.load_tool_schemas("healthcare")
    expected = [{
        "name": "check_plan_accepted",
        "parameters": {"carrier": "aetna", "location_id": "loc_park_ave"},
        "output": {"ok": True},
    }]
    actual = [{
        "name": "check_plan_accepted",
        "parameters": {
            "carrier": "Aetna",
            "location_id": "Park Avenue office in Manhattan",
        },
        "ok": True,
    }]
    assert vtr.tool_call_adherence(expected, actual, schemas=schemas)["passed"] is True
    wrong = [{
        "name": "check_plan_accepted",
        "parameters": {"carrier": "Aetna", "location_id": "Windermere"},
        "ok": True,
    }]
    assert vtr.tool_call_adherence(expected, wrong, schemas=schemas)["passed"] is False
