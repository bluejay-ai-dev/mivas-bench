"""Smoke tests for every industry state API + the generic /tools/{name} dispatch.

The invariant: every non-handoff tool in an industry's tools.json except
`end_call` is dispatchable via POST /tools/{name} {"arguments": {...}} and
answers with that industry's declared envelope; handoff/`end_call`/unknown
names 404. Human-transfer session tools still dispatch (they write state).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
INDUSTRIES = ("control-industry", "finance", "healthcare", "legal", "travel")


@contextmanager
def _load_tool_server(industry: str, *, shared: bool = True):
    """Import industries/<industry>/tool_server.py under a unique module name,
    pointing MIVAS_DB_PATH at a temp file (DB_PATH is read at import).
    Restores the previous MIVAS_DB_PATH value and deletes the temp dir on exit.
    shared=True (default) keeps existing tests working without a call-id header.
    Isolation tests pass shared=False and send X-Mivas-Call-Id."""
    original = os.environ.get("MIVAS_DB_PATH")
    original_shared = os.environ.get("MIVAS_DB_SHARED")
    with tempfile.TemporaryDirectory(prefix=f"mivas-{industry}-") as tmp:
        os.environ["MIVAS_DB_PATH"] = str(Path(tmp) / "runtime.db")
        if shared:
            os.environ["MIVAS_DB_SHARED"] = "1"
        else:
            os.environ.pop("MIVAS_DB_SHARED", None)
        try:
            name = f"tool_server_{industry.replace('-', '_')}"
            if name in sys.modules:
                del sys.modules[name]
            path = ROOT / "industries" / industry / "tool_server.py"
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            yield module
        finally:
            if original is None:
                os.environ.pop("MIVAS_DB_PATH", None)
            else:
                os.environ["MIVAS_DB_PATH"] = original
            if original_shared is None:
                os.environ.pop("MIVAS_DB_SHARED", None)
            else:
                os.environ["MIVAS_DB_SHARED"] = original_shared


def _tool_flags(industry: str) -> dict[str, dict[str, Any]]:
    bp = json.loads((ROOT / "industries" / industry / "agent_blueprint.json").read_text())
    flags: dict[str, dict[str, Any]] = {}
    for agent in bp["agents"]:
        for t in agent["tools"]:
            flags.setdefault(t["name"], t)
    return flags


def _sample_value(name: str, prop: dict[str, Any]) -> Any:
    t = prop.get("type", "string")
    if t == "boolean":
        return False
    if t in ("integer", "number"):
        return 1
    if t == "array":
        return []
    if t == "object":
        return {}
    if "date" in name or name in ("start", "end", "earliest", "latest", "dob"):
        return "2026-08-01"
    return "test"


def _sample_args(spec: dict[str, Any]) -> dict[str, Any]:
    schema = spec.get("inputSchema") or {}
    props = schema.get("properties") or {}
    return {name: _sample_value(name, props[name]) for name in schema.get("required") or []}


# Curated sample argument sets for tools that should return ok/success == True.
# This is a targeted smoke signal: a regressed handler that always soft-fails will
# fail these assertions even though the generic floor below still passes.
_KNOWN_GOOD_ARGS: dict[str, dict[str, dict[str, Any]]] = {
    "control-industry": {
        "schedule_appointment": {"date": "08/15/2026"},
    },
    "legal": {
        "lookup_caller": {"full_name": "Dana Whitfield", "phone": "5105550142"},
        "check_practice_area": {"practice_area": "auto_accident"},
        "calculate_filing_deadline": {
            "state": "CA",
            "practice_area": "auto_accident",
            "incident_date": "2024-09-15",
        },
        "find_evaluation_slots": {
            "practice_area": "auto_accident",
            "state": "CA",
            "earliest_date": "2026-08-01",
        },
        "get_attorney": {"attorney_id": "a_10"},
    },
    "healthcare": {
        "list_locations": {"zip": "10016"},
        "verify_identity": {"full_name": "Jordan Lee", "dob": "1990-04-12"},
        "get_patient_summary": {},
        "find_slots": {"location_ids": ["loc_park_ave"]},
        "explain_charge": {"line_item_id": "li_noshow"},
        "search_practice_kb": {"query": "open"},
    },
    "finance": {
        "search_kb": {"query": "routing number"},
        "get_branch_info": {"branch": "Granford"},
        "get_fee": {"fee": "overdraft"},
        "check_membership_eligibility": {"county": "Chester"},
        "identify_member": {"full_name": "Marisol Vega", "phone": "6105550142"},
        "verify_identity": {"dob": "1988-03-14", "member_number_last4": "4471"},
        "get_member_summary": {},
        "get_balance": {"account": "checking"},
        "get_transactions": {"account": "checking"},
        "get_cards": {},
    },
    "travel": {
        "find_reservation": {"last_name": "Solberg", "confirmation_code": "RT2LKD"},
        "get_reservation": {"confirmation_code": "RT2LKD"},
        "get_traveler_list": {"confirmation_code": "RT2LKD"},
        "get_disruption_entitlement": {"confirmation_code": "RT2LKD"},
        "get_fare_rules": {"confirmation_code": "RT2LKD"},
        "search_flights": {"origin": "ORD", "destination": "SEA", "earliest_date": "2026-08-09"},
        "get_flight_status": {"flight_number": "JA771", "date": "2026-08-09"},
        "get_credit_balance": {"miles_number": "JR2019773"},
        "get_elite_status": {"miles_number": "JR4471902"},
        "get_pass_status": {"miles_number": "JR8827104"},
        "get_seat_map": {"flight_number": "JA812", "date": "2026-08-18"},
        "escalate_to_human": {"reason_code": "caller_request"},
    },
}


def _dispatch_args(industry: str, spec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return the arguments and whether this tool must return ok/success == True."""
    name = spec["name"]
    known = _KNOWN_GOOD_ARGS.get(industry, {})
    if name in known:
        return known[name], True
    return _sample_args(spec), False


# Some known-good tools only succeed once a prerequisite tool has seeded session
# state (e.g. healthcare's get_patient_summary/explain_charge need verify_identity
# to have run first). Listed explicitly so the smoke test's dispatch order doesn't
# silently depend on tools.json's declaration order.
_DISPATCH_BEFORE: dict[str, list[str]] = {
    "healthcare": ["verify_identity"],
    "finance": ["identify_member", "verify_identity"],
    "travel": ["find_reservation"],
}


def _ordered_tools(industry: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder `tools` so each name in _DISPATCH_BEFORE[industry] comes first,
    in that order, ahead of everything else (original order otherwise)."""
    first = [n for n in _DISPATCH_BEFORE.get(industry, []) if any(t["name"] == n for t in tools)]
    by_name = {t["name"]: t for t in tools}
    rest = [t for t in tools if t["name"] not in first]
    return [by_name[n] for n in first] + rest


def test_dispatch_every_industry_tool() -> None:
    for industry in INDUSTRIES:
        with _load_tool_server(industry) as module:
            tools = json.loads(
                (ROOT / "industries" / industry / "tools.json").read_text()
            )["tools"]
            flags = _tool_flags(industry)
            with TestClient(module.app) as client:
                assert client.get("/health").status_code == 200, industry
                assert client.get("/state").status_code == 200, industry

                assert client.post("/tools/not_a_real_tool", json={"arguments": {}}).status_code == 404

                for spec in _ordered_tools(industry, tools):
                    name = spec["name"]
                    entry = flags.get(name, {})
                    args, must_succeed = _dispatch_args(industry, spec)
                    resp = client.post(f"/tools/{name}", json={"arguments": args})
                    if entry.get("handoff") or name == "end_call":
                        assert resp.status_code == 404, f"{industry}/{name} must stay harness-native"
                        continue
                    assert resp.status_code == 200, f"{industry}/{name}: {resp.text[:200]}"
                    body = resp.json()
                    assert isinstance(body, dict), f"{industry}/{name}"
                    # floor: every dispatchable tool returns the industry envelope
                    assert "ok" in body or "success" in body, f"{industry}/{name}: {body}"
                    if must_succeed:
                        assert body.get("ok") is True or body.get("success") is True, (
                            f"{industry}/{name} must succeed with known-good args: {body}"
                        )

                # reverse direction: no DISPATCH entry without a tools.json tool
                declared = {
                    spec["name"]
                    for spec in tools
                    if spec["name"] != "end_call"
                    and not flags.get(spec["name"], {}).get("handoff")
                }
                orphans = set(module.DISPATCH) - declared
                assert not orphans, f"{industry}: DISPATCH entries not in tools.json: {sorted(orphans)}"


def test_control_industry_rest_and_dispatch() -> None:
    with _load_tool_server("control-industry") as module, TestClient(module.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/state").json() == {"appointments": []}
        assert client.get("/appointments").json() == []

        created = client.post("/appointments", json={"date": "08/15/2026"})
        assert created.status_code == 201
        assert created.json()["date"] == "08/15/2026"

        dispatched = client.post(
            "/tools/schedule_appointment", json={"arguments": {"date": "08/16/2026"}}
        )
        assert dispatched.status_code == 200
        assert dispatched.json() == {"success": True, "date": "08/16/2026"}

        state = client.get("/state").json()
        assert [a["date"] for a in state["appointments"]] == ["08/15/2026", "08/16/2026"]

        # session tools stay harness-native
        assert client.post("/tools/end_call", json={"arguments": {"reason": "done"}}).status_code == 404


def test_legal_guards_survive_dispatch() -> None:
    with _load_tool_server("legal") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        found = tool("lookup_caller", {"full_name": "Dana Whitfield", "phone": "5105550142"})
        assert found["ok"] and found["data"]["caller_id"] == "c_001"
        # caller pinned server-side: no caller_id in the tool signature
        matters = tool("get_caller_matters", {})
        assert matters["ok"] and isinstance(matters["data"]["matters"], list)

        held = tool("hold_evaluation", {"slot_id": "s_110", "practice_area": "auto_accident"})
        assert held["ok"], held
        token = held["data"]["confirmation_token"]
        assert tool("confirm_evaluation", {"confirmation_token": "made-up"})["ok"] is False
        assert tool("confirm_evaluation", {"confirmation_token": token})["data"]["status"] == "booked"
        reuse = tool("confirm_evaluation", {"confirmation_token": token})
        assert reuse["ok"] is False and reuse["error_code"], "token must stay single-use"


def test_healthcare_list_locations_has_what_the_prompts_require() -> None:
    """Prompts forbid guessing an office address/floor and require reading the
    office back "WITH THE FLOOR" before booking, so list_locations must supply
    them. When it did not, a correct agent looped on the lookup and gave up
    instead of booking (openai result 716985)."""
    with _load_tool_server("healthcare") as module, TestClient(module.app) as client:
        resp = client.post("/tools/list_locations", json={"arguments": {"zip": "10016"}})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"], body
        locations = body["data"]["locations"]
        assert locations, body
        for loc in locations:
            for field in ("address", "floor", "suite", "hours", "services", "transit", "parking"):
                assert loc.get(field), f"{loc['id']} has no {field}: {loc}"
        # the caller's own zip sorts first, so the office they named is the one read back
        assert locations[0]["zip"] == "10016", locations[0]


def test_healthcare_prompt_demands_are_satisfiable() -> None:
    """Every fact a prompt orders the agent to say must be gettable from a tool.

    Each assertion here is a triage defect that failed a case while the agent behaved
    correctly (run 228930): D3 create_clinical_message returned no callback window that
    clinical.md orders spoken; D4 the KB could not answer an hours question about the
    office literally named "Park Avenue"; list_locations demanded a zip while
    reception.md promised it resolved office nicknames; a rejected carrier came back
    with sibling offices as "alternatives" that reject it too.
    """
    with _load_tool_server("healthcare") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        # D3 — a spoken callback window exists, and tracks priority
        assert tool("verify_identity", {"full_name": "Sam Nguyen", "dob": "1985-11-03"})["ok"]
        for priority, expect in (("stat", "hour"), ("urgent", "four"), ("routine", "business day")):
            msg = tool("create_clinical_message",
                       {"category": "nurse_question", "priority": priority, "summary": "x"})
            assert msg["ok"], msg
            assert expect in msg["data"]["callback_window"], (priority, msg["data"])
            assert msg["data"]["spoken_commitment"], msg["data"]

        # D4 — the hours question, phrased the way a caller phrases it, returns hours
        for query in ("Park Avenue office hours closing time",
                      "what time does the Park Avenue office close",
                      "when do you open"):
            kb = tool("search_practice_kb", {"query": query})
            assert kb["data"]["source"] == "hours", (query, kb["data"])
        # and parking still reaches directions
        assert tool("search_practice_kb", {"query": "where do I park"})["data"]["source"] == "directions"

        # list_locations by NAME, with no zip at all
        by_name = tool("list_locations", {"name": "Brooklyn Heights"})
        assert by_name["ok"], by_name
        first = by_name["data"]["locations"][0]
        assert first["id"] == "loc_brooklyn_heights", first
        assert first["floor"], first
        # zip still works and still wins for the caller's own zip
        assert tool("list_locations", {"zip": "34786"})["data"]["locations"][0]["id"] == "loc_windermere"

        # a rejected carrier gets no misleading alternatives
        med = tool("check_plan_accepted", {"carrier": "Medicaid", "location_id": "Park Avenue"})
        assert med["data"]["accepted"] is False
        assert med["data"]["alternative_locations"] == [], med["data"]
        assert "not accepted at any" in med["data"]["notes"], med["data"]


def test_healthcare_calls_are_isolated() -> None:
    """Two concurrent calls must not share an identity pin, a balance or a row.

    Without X-Mivas-Call-Id isolation, call B's get_account_balance reads call A's
    verified patient and call B's cancel hits a row A already cancelled.
    GET /state is scoped the same way so evals dump one conversation, not the pod.
    """
    with _load_tool_server("healthcare", shared=False) as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any], call: str | None = None) -> dict[str, Any]:
            headers = {"X-Mivas-Call-Id": call} if call else {}
            resp = client.post(f"/tools/{name}", json={"arguments": args}, headers=headers)
            assert resp.status_code == 200, resp.text
            return resp.json()

        a = tool("verify_identity", {"full_name": "Jordan Lee", "dob": "1990-04-12"}, "call_a")
        assert a["ok"] and a["data"]["patient_id"] == "pat_jordan_lee"

        # call B has verified nobody: A's pin must not leak in
        leaked = tool("get_account_balance", {}, "call_b")
        assert leaked["ok"] is False and leaked["error_code"] == "NOT_VERIFIED", leaked

        b = tool("verify_identity", {"full_name": "Maria Alvarez", "dob": "1972-06-30"}, "call_b")
        assert b["ok"] and b["data"]["patient_id"] == "pat_maria_alvarez"
        assert tool("get_account_balance", {}, "call_a")["data"]["balance_cents"] == 12500
        assert tool("get_account_balance", {}, "call_b")["data"]["balance_cents"] == 48000

        # both calls cancel appointment 1 / 2 in their own DB copy, twice over
        for call, appt in (("call_a", "1"), ("call_b", "2")):
            args = {"appointment_id": appt, "cancellation_reason_code": "patient_request"}
            assert tool("cancel_appointment", args, call)["data"]["status"] == "fee_disclosure_required"
        third = tool("verify_identity", {"full_name": "Jordan Lee", "dob": "1990-04-12"}, "call_c")
        assert third["ok"], third
        fresh = tool("cancel_appointment",
                     {"appointment_id": "1", "cancellation_reason_code": "patient_request"}, "call_c")
        assert fresh["data"]["status"] == "fee_disclosure_required", fresh
        assert fresh["data"]["fee_cents"] == 5000

        # cosmetic window: appointment 2 is inside 72 h → $125
        cos = tool("cancel_appointment",
                   {"appointment_id": "2", "cancellation_reason_code": "patient_request"}, "call_b")
        assert cos["data"]["fee_cents"] == 12500, cos
        # appointment 3 is outside every window → straight to cancelled, no fee
        alice = tool("verify_identity", {"full_name": "Alice Romano", "dob": "1995-09-08"}, "call_d")
        assert alice["ok"], alice
        free = tool("cancel_appointment",
                    {"appointment_id": "3", "cancellation_reason_code": "patient_request"}, "call_d")
        assert free["data"]["status"] == "cancelled" and free["data"]["fee_charged_cents"] == 0, free

        # financing gate: only Maria clears $250
        assert tool("offer_financing", {"amount_cents": 48000}, "call_b")["data"]["eligible"] is True
        assert tool("offer_financing", {"amount_cents": 12500}, "call_a")["data"]["eligible"] is False

        def state(call: str) -> dict[str, Any]:
            resp = client.get("/state", params={"call_id": call})
            assert resp.status_code == 200, resp.text
            return resp.json()

        dumped = state("call_d")
        seed = state("call_fresh")
        by_header = client.get("/state", headers={"X-Mivas-Call-Id": "call_d"})
        assert by_header.status_code == 200
        assert by_header.json() == dumped
        statuses = {row["id"]: row["status"] for row in dumped["appointments"]}
        seed_statuses = {row["id"]: row["status"] for row in seed["appointments"]}
        assert statuses[3] == "cancelled"
        assert seed_statuses[3] == "booked"
        assert seed_statuses == {
            row["id"]: row["status"] for row in state("call_a")["appointments"]
        }
        assert client.get("/state").status_code == 400


def test_healthcare_flow_through_dispatch() -> None:
    with _load_tool_server("healthcare") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        # identity gate: summary before verification fails safe
        locked = tool("get_patient_summary", {})
        assert locked["ok"] is False and locked["error_code"] == "NOT_VERIFIED"

        verified = tool("verify_identity", {"full_name": "Jordan Lee", "dob": "1990-04-12"})
        assert verified["ok"] and verified["data"]["patient_id"] == "pat_jordan_lee"
        summary = tool("get_patient_summary", {})
        assert summary["ok"] and summary["data"]["balance_cents"] == 12500

        # cancellation fee gate: seeded appt is inside the 24 h medical window
        first = tool("cancel_appointment",
                     {"appointment_id": "1", "cancellation_reason_code": "patient_request"})
        assert first["data"]["status"] == "fee_disclosure_required", first
        second = tool("cancel_appointment",
                      {"appointment_id": "1", "cancellation_reason_code": "patient_request",
                       "fee_disclosed_and_accepted": True})
        assert second["data"]["status"] == "cancelled"
        assert second["data"]["fee_charged_cents"] == 5000

        # a fixture-backed write lands in durable state
        slots = tool("find_slots", {"location_ids": ["loc_park_ave"]})
        assert slots["ok"] and slots["data"]["count"] > 0
        slot = slots["data"]["slots"][0]
        booked = tool("book_appointment", {
            "slot_id": slot["slot_id"], "appointment_type_code": "MED_FOLLOWUP",
            "location_id": slot["location_id"], "provider_id": slot["provider_id"],
            "start": slot["start"], "end": slot["end"], "description": "follow-up",
        })
        assert booked["ok"], booked
        state = client.get("/state").json()
        assert any(a["status"] == "booked" and a["start"] == slot["start"]
                   for a in state["appointments"])


def test_finance_guards_survive_dispatch() -> None:
    with _load_tool_server("finance") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        # GLBA gate: identified is not verified; verified unlocks
        locked = tool("get_member_summary", {})
        assert locked["ok"] is False and locked["error_code"] == "IDENTITY_NOT_VERIFIED"
        found = tool("identify_member", {"full_name": "Marisol Vegga", "phone": "0142"})
        assert found["ok"] and found["data"]["record_found"]
        assert tool("get_balance", {"account": "checking"})["ok"] is False
        assert tool("verify_identity",
                    {"dob": "1988-03-14", "member_number_last4": "4471"})["ok"]
        bal = tool("get_balance", {"account": "checking"})
        assert bal["ok"] and bal["data"]["available_cents"] == 238012

        # wire: tier math, warning gate, token single-use
        q = tool("quote_wire", {"destination_type": "domestic", "amount": 2500,
                                "beneficiary": "Test Person"})
        assert q["ok"] and q["data"]["fee"] == "$30.00"
        token = q["data"]["confirmation_token"]
        needs_warning = tool("confirm_wire", {"confirmation_token": token,
                                              "fraud_warning_acknowledged": False})
        assert needs_warning["error_code"] == "WIRE_WARNING_REQUIRED"
        sent = tool("confirm_wire", {"confirmation_token": token,
                                     "fraud_warning_acknowledged": True})
        assert sent["ok"] and sent["data"]["status"] == "sent"
        reuse = tool("confirm_wire", {"confirmation_token": token,
                                      "fraud_warning_acknowledged": True})
        assert reuse["ok"] is False and reuse["error_code"] == "TOKEN_ALREADY_USED"

        # dispute: disclosure gate, Reg E script, durable claim row
        tool("identify_member", {"full_name": "Alma Reyes", "phone": "6105550129"})
        tool("verify_identity", {"dob": "1992-12-05", "member_number_last4": "5518"})
        first = tool("file_dispute", {"transaction_id": "t_701",
                                      "reason": "unauthorized"})
        assert first["error_code"] == "DISCLOSURE_REQUIRED"
        assert "10 business days" in first["member_safe_message"]
        filed = tool("file_dispute", {"transaction_id": "t_701",
                                      "reason": "unauthorized",
                                      "disclosures_acknowledged": True})
        assert filed["ok"] and filed["data"]["regulation"] == "reg_e"
        state = client.get("/state").json()
        assert any(c["transaction_id"] == "t_701" for c in state["claims"])


def test_travel_guards_survive_dispatch() -> None:
    """Identity gate, the disrupted-booking precedence trap, silent elite waivers,
    and token discipline, all through POST /tools/{name}."""
    with _load_tool_server("travel") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        # protected data is closed until a reservation is verified on this call
        assert tool("get_reservation", {})["error_code"] == "IDENTITY_NOT_VERIFIED"

        # tolerant on spelling and on a spaced-out code, strict on identity
        found = tool("find_reservation",
                     {"last_name": "Sollberg", "confirmation_code": "rt 2 l k d"})
        assert found["ok"] and found["data"]["verified"]
        assert found["data"]["confirmation_code"] == "RT2LKD"

        # the precedence trap: RT2LKD's flight is cancelled, so a voluntary change
        # must be refused rather than quoted a fee
        disrupted = tool("quote_change", {"new_flight": "JA775"})
        assert disrupted["ok"] is False
        assert disrupted["error_code"] == "DISRUPTED_USE_IRROPS", disrupted
        assert disrupted["data"]["recoverable"] is False

        # the free rebook is what that traveller is actually owed, at zero
        rebook = tool("quote_involuntary_rebook", {"new_flight": "JA775"})
        assert rebook["ok"] and rebook["data"]["total"] == 0.0, rebook
        token = rebook["data"]["confirmation_token"]
        assert tool("confirm_involuntary_rebook",
                    {"confirmation_token": token})["data"]["status"] == "rebooked"
        reuse = tool("confirm_involuntary_rebook", {"confirmation_token": token})
        assert reuse["ok"] is False and reuse["error_code"] == "TOKEN_ALREADY_USED"

        # a dead carrier's code is a non-recoverable refusal, not a NOT_FOUND
        ceased = tool("find_reservation",
                      {"last_name": "Quintero-Namm", "confirmation_code": "VA774193"})
        assert ceased["error_code"] == "CARRIER_CEASED_OPERATIONS", ceased
        assert ceased["data"]["recoverable"] is False

    # silent waivers and touchpoint pricing, on a fresh server so the session is clean
    with _load_tool_server("travel") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        assert tool("find_reservation",
                    {"last_name": "Ingersoll", "confirmation_code": "ZC8MRF"})["ok"]
        first = tool("get_bag_price",
                     {"bag_kind": "checked_first", "touchpoint": "booking"})
        assert first["data"]["price"] == 0.0, "platinum covers the first checked bag"
        assert first["data"]["base_price"] == 30.0
        carry = tool("get_bag_price", {"bag_kind": "carry on", "touchpoint": "gate"})
        assert carry["data"]["price"] == 79.0, "no tier ever covers the carry-on"

        # a charge has to have been quoted on this call
        assert tool("quote_payment",
                    {"amount": 500})["error_code"] == "AMOUNT_NOT_QUOTED"


def test_control_industry_calls_are_isolated() -> None:
    """Two overlapping bookings must not share a SQLite file."""
    with _load_tool_server("control-industry", shared=False) as module:
        with TestClient(module.app) as client:
            def book(call: str, date: str) -> dict[str, Any]:
                resp = client.post(
                    "/tools/schedule_appointment",
                    json={"arguments": {"date": date}},
                    headers={"X-Mivas-Call-Id": call},
                )
                assert resp.status_code == 200, resp.text
                return resp.json()

            def state(call: str) -> dict[str, Any]:
                resp = client.get("/state", headers={"X-Mivas-Call-Id": call})
                assert resp.status_code == 200, resp.text
                return resp.json()

            assert book("675", "08/15/2026")["success"] is True
            assert book("676", "08/16/2026")["success"] is True
            a = state("675")["appointments"]
            b = state("676")["appointments"]
            c = state("677")["appointments"]
            assert [row["date"] for row in a] == ["08/15/2026"]
            assert [row["date"] for row in b] == ["08/16/2026"]
            assert c == []


def test_state_query_alias_scopes_control_industry() -> None:
    """Evals dump one conversation via GET /state?call_id= — no prior tool still seed."""
    with _load_tool_server("control-industry", shared=False) as module:
        with TestClient(module.app) as client:
            booked = client.post(
                "/tools/schedule_appointment",
                json={"arguments": {"date": "08/15/2026"}},
                headers={"X-Mivas-Call-Id": "675"},
            )
            assert booked.status_code == 200, booked.text
            assert booked.json()["success"] is True

            via_query = client.get("/state?call_id=675")
            via_header = client.get("/state", headers={"X-Mivas-Call-Id": "675"})
            untouched = client.get("/state?call_id=676")
            assert via_query.status_code == 200, via_query.text
            assert via_header.json() == via_query.json()
            assert [row["date"] for row in via_query.json()["appointments"]] == [
                "08/15/2026"
            ]
            assert untouched.status_code == 200, untouched.text
            assert untouched.json()["appointments"] == []
            assert client.get("/state").status_code == 400


def test_missing_call_id_is_400() -> None:
    with _load_tool_server("control-industry", shared=False) as module:
        with TestClient(module.app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/state").status_code == 400
            assert client.post(
                "/tools/schedule_appointment",
                json={"arguments": {"date": "08/15/2026"}},
            ).status_code == 400


def test_industry_writes_do_not_leak_across_call_ids() -> None:
    """Each industry: a write on call A is invisible to call B; call C matches seed."""
    writers: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "control-industry": [("schedule_appointment", {"date": "08/15/2026"})],
        "healthcare": [("create_callback_task", {
            "queue": "front_desk",
            "callback_number": "+12125550100",
            "topic": "isolation write",
        })],
        "finance": [("escalate_to_human", {"reason_code": "caller_request"})],
        "legal": [
            ("lookup_caller", {"full_name": "Dana Whitfield", "phone": "5105550142"}),
            ("take_message", {"for_whom": "reception", "message": "please call back"}),
        ],
        "travel": [("escalate_to_human", {"reason_code": "caller_request"})],
    }
    for industry, steps in writers.items():
        with _load_tool_server(industry, shared=False) as module:
            with TestClient(module.app) as client:
                def tool(call: str) -> None:
                    for name, args in steps:
                        resp = client.post(
                            f"/tools/{name}",
                            json={"arguments": args},
                            headers={"X-Mivas-Call-Id": call},
                        )
                        assert resp.status_code == 200, (
                            f"{industry}/{name}: {resp.text[:200]}"
                        )

                def state(call: str) -> dict[str, Any]:
                    resp = client.get("/state", headers={"X-Mivas-Call-Id": call})
                    assert resp.status_code == 200, resp.text
                    return resp.json()

                seed = state("call_c")
                tool("call_a")
                after_a = state("call_a")
                after_b = state("call_b")
                after_c = state("call_c")
                assert after_b == seed, industry
                assert after_c == seed, industry
                assert after_a != seed, industry


if __name__ == "__main__":
    test_dispatch_every_industry_tool()
    test_control_industry_rest_and_dispatch()
    test_legal_guards_survive_dispatch()
    test_finance_guards_survive_dispatch()
    test_healthcare_flow_through_dispatch()
    test_healthcare_calls_are_isolated()
    test_healthcare_prompt_demands_are_satisfiable()
    test_healthcare_list_locations_has_what_the_prompts_require()
    test_travel_guards_survive_dispatch()
    print("ok test_tool_server")
