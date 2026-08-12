"""Smoke tests for every industry state API + the generic /tools/{name} dispatch.

The invariant: every non-handoff, non-session tool in an industry's tools.json
is dispatchable via POST /tools/{name} {"arguments": {...}} and answers with
that industry's declared envelope; session/handoff/unknown names 404.
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
def _load_tool_server(industry: str):
    """Import industries/<industry>/tool_server.py under a unique module name,
    pointing MIVAS_DB_PATH at a temp file (DB_PATH is read at import).
    Restores the previous MIVAS_DB_PATH value and deletes the temp dir on exit."""
    original = os.environ.get("MIVAS_DB_PATH")
    with tempfile.TemporaryDirectory(prefix=f"mivas-{industry}-") as tmp:
        os.environ["MIVAS_DB_PATH"] = str(Path(tmp) / "runtime.db")
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
        "resolve_inbound_context": {"caller_ani": "+12125550100"},
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
        "search_flights": {"origin": "ORD", "destination": "SEA", "earliest_date": "2026-08-09"},
        "get_flight_status": {"flight_number": "CX771", "date": "2026-08-09"},
        "get_credit_balance": {"summit_number": "SC2019773"},
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
                    if entry.get("session") or entry.get("handoff"):
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
                    if not (flags.get(spec["name"], {}).get("session")
                            or flags.get(spec["name"], {}).get("handoff"))
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
    with _load_tool_server("travel") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        found = tool("find_reservation", {"last_name": "Solberg", "confirmation_code": "RT2LKD"})
        assert found["ok"] and found["data"]["verified"]

        saver = tool("quote_change", {"confirmation_code": "QK4TZP", "new_flight": "CX119"})
        assert saver["ok"] is False and saver["error_code"] == "SAVER_NOT_CHANGEABLE"

        quote = tool("quote_change", {"confirmation_code": "HB9WQM", "new_flight": "CX404"})
        assert quote["ok"], quote
        token = quote["data"]["confirmation_token"]
        assert tool("confirm_change", {"confirmation_token": token})["data"]["status"] == "changed"
        reuse = tool("confirm_change", {"confirmation_token": token})
        assert reuse["ok"] is False and reuse["error_code"] == "TOKEN_ALREADY_USED"


if __name__ == "__main__":
    test_dispatch_every_industry_tool()
    test_control_industry_rest_and_dispatch()
    test_legal_guards_survive_dispatch()
    test_finance_guards_survive_dispatch()
    test_healthcare_flow_through_dispatch()
    test_travel_guards_survive_dispatch()
    print("ok test_tool_server")
