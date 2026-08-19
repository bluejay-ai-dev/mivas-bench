"""Tool, handoff, and office-state checks (no live API)."""

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


def test_office_waitlist_allows_extra_rows_when_expected_is_nonempty() -> None:
    expected_row = {
        "patient_id": "pat_jordan_lee",
        "location_id": "loc_park_ave",
        "earliest": "2026-08-24T00:00:00",
        "latest": "2026-09-30T23:59:59",
    }
    extra_row = {
        "patient_id": "pat_jordan_lee",
        "location_id": "loc_park_ave",
        "earliest": "2026-08-18T00:00:00",
        "latest": "2026-09-30T23:59:59",
    }
    expected = {"patients": [], "appointments": [], "waitlist": [expected_row]}
    actual = {"patients": [], "appointments": [], "waitlist": [expected_row, extra_row]}
    assert vtr.office_states_match(expected, actual, industry="healthcare") is True
    missing = {"patients": [], "appointments": [], "waitlist": [extra_row]}
    assert vtr.office_states_match(expected, missing, industry="healthcare") is False
    empty_expected = {"patients": [], "appointments": [], "waitlist": []}
    assert vtr.office_states_match(empty_expected, actual, industry="healthcare") is False
    # extra listing consumes id=1; the matching window is id=2
    expected_with_id = {
        "patients": [],
        "appointments": [],
        "waitlist": [{**expected_row, "id": 1}],
    }
    actual_id_shifted = {
        "patients": [],
        "appointments": [],
        "waitlist": [
            {**extra_row, "id": 1},
            {**expected_row, "id": 2},
        ],
    }
    assert vtr.office_states_match(expected_with_id, actual_id_shifted, industry="healthcare") is True
    assert vtr.office_states_match(empty_expected, {"patients": [], "appointments": [], "waitlist": []}, industry="healthcare") is True


def test_office_datetimes_match_at_minute_precision() -> None:
    expected_row = {
        "patient_id": "pat_jordan_lee",
        "location_id": "loc_park_ave",
        "earliest": "2026-08-24T00:00:00",
        "latest": "2026-09-30T23:59:59",
    }
    actual_row = {
        **expected_row,
        "latest": "2026-09-30T23:59:00",
    }
    expected = {"patients": [], "appointments": [], "waitlist": [expected_row]}
    actual = {"patients": [], "appointments": [], "waitlist": [actual_row]}
    assert vtr.office_canonical(expected)["waitlist"][0]["latest"] == "2026-09-30T23:59"
    assert vtr.office_canonical(actual)["waitlist"][0]["latest"] == "2026-09-30T23:59"
    assert vtr.office_states_match(expected, actual, industry="healthcare") is True
    minute_off = {
        "patients": [],
        "appointments": [],
        "waitlist": [{**actual_row, "latest": "2026-09-30T23:58:00"}],
    }
    assert vtr.office_states_match(expected, minute_off, industry="healthcare") is False
    expected_apt = {
        "patients": [],
        "appointments": [{"id": 1, "start": "2026-08-24T09:00:00"}],
        "waitlist": [],
    }
    actual_apt = {
        "patients": [],
        "appointments": [{"id": 1, "start": "2026-08-24T09:00:59.500000"}],
        "waitlist": [],
    }
    assert vtr.office_states_match(expected_apt, actual_apt) is True


def test_verify_result_ignores_tool_events_in_state() -> None:
    task = {
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


def test_verify_result_matching_state() -> None:
    task = {"exp_db_state": {"waitlist": []}}
    result = {"transcript": [{"role": "agent", "text": "hello"}]}
    out = vtr.verify_result(result, task, {"waitlist": [], "created_at": "x"})
    assert out["state"]["passed"] is True
    assert out["passed"] is True


def test_verify_result_skips_state_when_dump_missing() -> None:
    task = {"exp_db_state": {"waitlist": []}}
    result = {"messages": [{"role": "assistant", "content": "hello there"}]}
    out = vtr.verify_result(result, task, None, state_note="S3 not configured")
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
    assert none["verdict"] == "exact"
    extras_ok = vtr.handoff_adherence([], ["transfer_to_human"])
    assert extras_ok["passed"] is True
    assert extras_ok["verdict"] == "none_required"


def test_actual_tool_calls_sort_by_start_offset_not_group_order() -> None:
    """Bluejay groups by name; C5-M2 listed billing before identity."""
    result = {
        "tool_calls": [
            {
                "name": "transfer_to_billing",
                "actual": [{
                    "parameters": {"to": "billing", "from": "identity"},
                    "start_offset_ms": 91015,
                }],
            },
            {
                "name": "transfer_to_identity",
                "actual": [{
                    "parameters": {"to": "identity", "from": "reception"},
                    "start_offset_ms": 23688,
                }],
            },
        ],
    }
    names = [call["name"] for call in vtr.actual_tool_calls(result)]
    assert names == ["transfer_to_identity", "transfer_to_billing"]
    path = vtr.handoff_adherence(
        ["transfer_to_identity", "transfer_to_billing"],
        vtr.actual_handoffs(names),
    )
    assert path["passed"] is True
    assert path["verdict"] == "exact"


def test_verify_result_includes_call_and_handoff() -> None:
    task = {
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
            "location_id": "Park Avenue",
        },
        "ok": True,
    }]
    assert vtr.tool_call_adherence(expected, actual, schemas=schemas)["passed"] is True
    loose = [{
        "name": "check_plan_accepted",
        "parameters": {
            "carrier": "Aetna",
            "location_id": "Park Avenue office in Manhattan",
        },
        "ok": True,
    }]
    assert vtr.tool_call_adherence(expected, loose, schemas=schemas)["passed"] is False
    wrong = [{
        "name": "check_plan_accepted",
        "parameters": {"carrier": "Aetna", "location_id": "Windermere"},
        "ok": True,
    }]
    assert vtr.tool_call_adherence(expected, wrong, schemas=schemas)["passed"] is False


def test_location_ids_require_exact_set() -> None:
    schemas = vtr.load_tool_schemas("healthcare")
    expected = [{
        "name": "find_slots",
        "parameters": {"location_ids": ["loc_park_ave"]},
    }]
    extra = [{
        "name": "find_slots",
        "parameters": {"location_ids": ["loc_park_ave", "loc_windermere"]},
    }]
    assert vtr.tool_call_adherence(expected, extra, schemas=schemas)["passed"] is False
    ok = [{
        "name": "find_slots",
        "parameters": {"location_ids": ["Park Avenue"]},
    }]
    assert vtr.tool_call_adherence(expected, ok, schemas=schemas)["passed"] is True


def test_send_sms_is_required_only_when_listed() -> None:
    schemas = vtr.load_tool_schemas("healthcare")
    booked = [{"name": "book_appointment", "parameters": {}}]
    with_sms = [
        {"name": "book_appointment"},
        {
            "name": "send_sms",
            "parameters": {
                "template_id": "appointment_confirmation",
                "mobile_e164": "+12125550100",
            },
        },
    ]
    miss = vtr.tool_call_adherence(with_sms, booked, schemas=schemas)
    assert miss["passed"] is False
    assert "send_sms" in miss["missing"]
    skip_ok = vtr.tool_call_adherence(
        [{"name": "book_appointment"}],
        booked,
        schemas=schemas,
        industry="healthcare",
    )
    assert skip_ok["passed"] is True
    extra_sms = vtr.tool_call_adherence(
        [{"name": "book_appointment"}],
        with_sms,
        schemas=schemas,
        industry="healthcare",
    )
    assert extra_sms["passed"] is False
    assert extra_sms["extra"] == ["send_sms"]
    extras_ok_elsewhere = vtr.tool_call_adherence(
        [{"name": "book_appointment"}],
        with_sms,
        schemas=schemas,
    )
    assert extras_ok_elsewhere["passed"] is True
    empty_sms = vtr.tool_call_adherence([], with_sms, schemas=schemas, industry="healthcare")
    assert empty_sms["passed"] is False
    assert empty_sms["extra"] == ["send_sms"]


def test_clinical_message_allows_irrelevant_optional_parameters() -> None:
    schemas = vtr.load_tool_schemas("healthcare")
    expected = [{
        "name": "create_clinical_message",
        "parameters": {"category": "results_followup"},
    }]
    actual = [{
        "name": "create_clinical_message",
        "parameters": {
            "category": "results_followup",
            "priority": "urgent",
            "callback_number": "+12125550100",
        },
        "ok": True,
    }]
    assert vtr.tool_call_adherence(expected, actual, schemas=schemas)["passed"] is True

    wrong_category = [{
        "name": "create_clinical_message",
        "parameters": {
            "category": "nurse_question",
            "priority": "routine",
        },
    }]
    result = vtr.tool_call_adherence(expected, wrong_category, schemas=schemas)
    assert result["passed"] is False
    assert result["missing"] == ["create_clinical_message"]


def test_for_whom_matches_casefold() -> None:
    assert vtr._values_equal("for_whom", "Daniel Okonkwo", "daniel okonkwo") is True
    assert vtr._values_equal("for_whom", "Allison Fontaine", " allison fontaine ") is True
    assert vtr._values_equal("for_whom", "Daniel Okonkwo", "Allison Fontaine") is False
    expected = {"name": "take_message", "parameters": {"for_whom": "Daniel Okonkwo"}}
    actual = {"name": "take_message", "parameters": {"for_whom": "daniel okonkwo"}}
    assert vtr._calls_match(expected, actual, None, "legal") is True


def test_opposing_party_matches_substring_and_casefold() -> None:
    assert vtr._values_equal(
        "opposing_party",
        "St. Benedict Medical Center",
        "St. Benedict Medical Center and the surgeon involved",
    ) is True
    assert vtr._values_equal(
        "opposing_party",
        "Harborline Industries",
        "Harbor Line Industries",
    ) is True
    assert vtr._values_equal("opposing_party", "Vertex Logistics", "vertex logistics") is True
    assert vtr._values_equal("opposing_party", "Vertex Logistics", "Northgate Insurance") is False
    expected = {
        "name": "check_conflict",
        "parameters": {"opposing_party": "St. Benedict Medical Center"},
    }
    actual = {
        "name": "check_conflict",
        "parameters": {
            "opposing_party": "St. Benedict Medical Center and the surgeon involved",
        },
    }
    assert vtr._calls_match(expected, actual, None, "legal") is True


def test_legal_full_name_is_presence_only() -> None:
    expected = {
        "name": "lookup_caller",
        "parameters": {"full_name": "Curtis Beaumont", "phone": "555-555-0012"},
    }
    actual = {
        "name": "lookup_caller",
        "parameters": {"full_name": "Curtis Bowman", "phone": "5555550012"},
    }
    assert vtr._calls_match(expected, actual, None, "legal") is True
    healthcare = {
        "name": "lookup_patient",
        "parameters": {"full_name": "Curtis Beaumont"},
    }
    healthcare_asr = {
        "name": "lookup_patient",
        "parameters": {"full_name": "Curtis Bowman"},
    }
    assert vtr._calls_match(healthcare, healthcare_asr, None, "healthcare") is False


def test_legal_office_states_ignore_message_prose() -> None:
    expected = {
        "messages": [{
            "id": 1,
            "caller_id": "c_new",
            "for_whom": "Daniel Okonkwo",
            "message": "Callback requested.",
        }],
    }
    actual = {
        "messages": [{
            "id": 1,
            "caller_id": "c_new",
            "for_whom": "Daniel Okonkwo",
            "message": "The forms arrived; question on page four.",
        }],
    }
    assert vtr.office_states_match(expected, actual, industry="legal") is True


def test_legal_empty_expected_messages_allows_extra_rows() -> None:
    expected = {"messages": []}
    actual = {
        "messages": [{
            "id": 1,
            "caller_id": "c_004",
            "for_whom": "intake",
            "message": "Please call back.",
        }],
    }
    assert vtr.office_states_match(expected, actual, industry="legal") is True


def test_legal_empty_expected_intakes_rejects_extra_rows() -> None:
    expected = {"intakes": []}
    actual = {
        "intakes": [{
            "id": 1,
            "caller_id": "c_new",
            "practice_area": "auto",
            "state": "TX",
            "incident_date": "2026-01-18",
        }],
    }
    assert vtr.office_states_match(expected, actual, industry="legal") is False


def test_legal_booking_rows_ignore_slot_identity() -> None:
    expected = {
        "evaluations": [{
            "id": "ev_001",
            "caller_id": "c_new",
            "slot_id": "s_110",
            "attorney_id": "a_11",
            "starts_at": "2026-08-11T09:00",
            "status": "booked",
        }],
        "holds": [{
            "token": "HR-EVAL-3092",
            "kind": "evaluation",
            "caller_id": "c_new",
            "slot_id": "s_110",
            "practice_area": "auto_accident",
            "consumed": 1,
        }],
    }
    actual = {
        "evaluations": [{
            "id": "ev_001",
            "caller_id": "c_new",
            "slot_id": "s_111",
            "attorney_id": "a_10",
            "starts_at": "2026-08-25T14:00",
            "status": "booked",
        }],
        "holds": [{
            "token": "HR-EVAL-3092",
            "kind": "evaluation",
            "caller_id": "c_new",
            "slot_id": "s_111",
            "practice_area": "auto_accident",
            "consumed": 1,
        }],
    }
    assert vtr.office_states_match(expected, actual, industry="legal") is True


