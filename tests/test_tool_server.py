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
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
INDUSTRIES = ("control-industry", "customer-support", "healthcare", "legal")


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
    if prop.get("enum"):
        return prop["enum"][0]
    t = prop.get("type", "string")
    if t == "boolean":
        return False
    if t in ("integer", "number"):
        return 1
    if t == "array":
        items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
        return [_sample_value("item", items)] if items else []
    if t == "object":
        return {}
    if prop.get("format") == "date" or prop.get("pattern") == r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$":
        return "2026-08-01"
    if prop.get("format") == "date-time" or "T[0-9]{2}:[0-9]{2}" in str(prop.get("pattern") or ""):
        return "2026-08-01T09:00"
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
        "record_intake": {
            "practice_area": "premises_liability",
            "state": "CA",
            "incident_date": "2026-01-18",
            "summary": "",
        },
    },
    "healthcare": {
        "list_locations": {"zip": "10016"},
        "verify_identity": {"full_name": "Jordan Lee", "dob": "1990-04-12"},
        "get_patient_summary": {},
        "find_slots": {"location_ids": ["loc_park_ave"]},
        "explain_charge": {"line_item_id": "li_noshow"},
        "check_plan_accepted": {"carrier": "aetna", "location_id": "loc_park_ave"},
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
    "legal": ["lookup_caller"],
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


def test_legal_record_intake_rejects_garbage() -> None:
    with _load_tool_server("legal") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        found = tool("lookup_caller", {"full_name": "Dana Whitfield", "phone": "5105550142"})
        assert found["ok"]

        base = {
            "practice_area": "premises_liability",
            "state": "CA",
            "incident_date": "2026-01-18",
            "summary": "",
        }
        for args in (
            {**base, "state": ""},
            {**base, "state": "{{state}}"},
            {**base, "incident_date": "nope"},
        ):
            body = tool("record_intake", args)
            assert body["ok"] is False, args

        good = tool("record_intake", base)
        assert good["ok"] is True
        ny = tool("record_intake", {**base, "state": "NY"})
        assert ny["ok"] is True
        named = tool("record_intake", {**base, "state": "illinois"})
        assert named["ok"] is True
        intakes = {row["id"]: row for row in client.get("/state").json()["intakes"]}
        assert intakes[good["data"]["intake_id"]]["state"] == "CA"
        assert intakes[good["data"]["intake_id"]]["incident_date"] == "2026-01-18"
        assert intakes[ny["data"]["intake_id"]]["state"] == "NY"
        assert intakes[named["data"]["intake_id"]]["state"] == "IL"


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
    clinical.md orders spoken; D4 office hours, transit, and parking must come from
    list_locations for the office literally named "Park Avenue"; list_locations
    demanded a zip while reception.md promised it resolved office nicknames; a
    rejected carrier came back with sibling offices as "alternatives" that reject
    it too.
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
                       {"category": "nurse_question", "priority": priority})
            assert msg["ok"], msg
            assert expect in msg["data"]["callback_window"], (priority, msg["data"])
            assert msg["data"]["spoken_commitment"], msg["data"]

        # D4 — office hours, transit, parking, and services come from list_locations
        park = tool("list_locations", {"location_id": "loc_park_ave"})["data"]["locations"][0]
        assert "Mon-Fri" in park["hours"], park
        assert park["transit"] and park["parking"] and park["services"], park

        # list_locations by location_id, with no zip at all
        by_id = tool("list_locations", {"location_id": "loc_brooklyn_heights"})
        assert by_id["ok"], by_id
        first = by_id["data"]["locations"][0]
        assert first["id"] == "loc_brooklyn_heights", first
        assert first["floor"], first
        # zip still works and still wins for the caller's own zip
        assert tool("list_locations", {"zip": "34786"})["data"]["locations"][0]["id"] == "loc_windermere"

        # a rejected carrier gets no misleading alternatives
        med = tool("check_plan_accepted", {"carrier": "medicaid", "location_id": "loc_park_ave"})
        assert med["data"]["accepted"] is False
        assert med["data"]["alternative_locations"] == [], med["data"]
        assert "not accepted at any" in med["data"]["notes"], med["data"]


def test_allergy_service_window_and_idempotent() -> None:
    with _load_tool_server("healthcare") as module, TestClient(module.app) as client:
        booked = client.post(
            "/tools/schedule_allergy_service",
            json={"arguments": {
                "service": "skin_testing",
                "location_id": "loc_park_ave",
                "window_start": "2026-08-24T00:00",
                "window_end": "2026-08-24T17:00",
            }},
        ).json()
        assert booked["ok"], booked
        assert booked["data"]["appointment"]["start"] == "2026-08-24T09:00"

        tz_ok = client.post(
            "/tools/schedule_allergy_service",
            json={"arguments": {
                "service": "food_challenge",
                "location_id": "loc_park_ave",
                "window_start": "2026-08-24T09:00:00-04:00",
                "window_end": "2026-08-24T17:00:00-04:00",
            }},
        ).json()
        assert tz_ok["ok"], tz_ok
        assert tz_ok["data"]["appointment"]["start"] == "2026-08-24T09:00"

        unavailable = client.post(
            "/tools/schedule_allergy_service",
            json={"arguments": {
                "service": "patch_testing",
                "location_id": "loc_park_ave",
                "window_start": "2026-08-24T00:00",
                "window_end": "2026-08-24T01:00",
            }},
        ).json()
        assert unavailable["error_code"] == "NO_AVAILABILITY"

        args = {
            "service": "allergy_shot",
            "location_id": "loc_brooklyn_heights",
            "window_start": "2026-08-24T00:00",
            "window_end": "2026-08-24T17:00",
        }
        assert client.post(
            "/tools/verify_identity",
            json={"arguments": {"full_name": "Leo Park", "dob": "2016-03-22"}},
        ).json()["ok"]
        first = client.post("/tools/schedule_allergy_service", json={"arguments": args}).json()
        repeated = client.post("/tools/schedule_allergy_service", json={"arguments": args}).json()
        assert first["ok"] and repeated["ok"]
        assert repeated["data"]["appointment"]["id"] == first["data"]["appointment"]["id"]
        assert repeated["data"]["idempotent"] is True
        tight = {**args, "window_end": "2026-08-24T01:00"}
        again = client.post("/tools/schedule_allergy_service", json={"arguments": tight}).json()
        assert again["ok"] and again["data"]["idempotent"] is True
        matching = [
            row for row in client.get("/state").json()["appointments"]
            if row["patient_id"] == "pat_leo_park"
            and row["appointment_type_code"] == "ALLERGY_ALLERGY_SHOT"
            and row["status"] == "booked"
        ]
        assert len(matching) == 1


def test_refill_tool_takes_only_medication_name() -> None:
    tools = json.loads((ROOT / "industries" / "healthcare" / "tools.json").read_text())["tools"]
    refill = next(tool for tool in tools if tool["name"] == "request_rx_refill")
    assert refill["inputSchema"]["required"] == ["medication_name"]
    assert set(refill["inputSchema"]["properties"]) == {"medication_name"}


def test_find_slots_window_end_requires_full_duration() -> None:
    with _load_tool_server("healthcare") as module, TestClient(module.app) as client:
        def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            resp = client.post(f"/tools/{name}", json={"arguments": args})
            assert resp.status_code == 200, resp.text
            return resp.json()

        args = {"location_ids": ["loc_park_ave"]}
        open_slots = tool("find_slots", args)
        assert open_slots["ok"] and open_slots["data"]["count"] > 0
        first = open_slots["data"]["slots"][0]
        assert first["start"] == "2026-08-24T09:00"
        assert first["end"] == "2026-08-24T09:30"

        inside = tool("find_slots", {**args, "window_end": "2026-08-24T09:15"})
        assert inside["ok"]
        starts = [slot["start"] for slot in inside["data"]["slots"]]
        assert "2026-08-24T09:00" not in starts

        fits = tool("find_slots", {**args, "window_end": "2026-08-24T09:30"})
        assert any(slot["start"] == "2026-08-24T09:00" for slot in fits["data"]["slots"])


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
            "start": slot["start"], "end": slot["end"],
        })
        assert booked["ok"], booked
        state = client.get("/state").json()
        assert any(a["status"] == "booked" and a["start"] == slot["start"]
                   for a in state["appointments"])
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
        })],
        "legal": [
            ("lookup_caller", {"full_name": "Dana Whitfield", "phone": "5105550142"}),
            ("take_message", {"for_whom": "reception", "message": "please call back"}),
        ],
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


_FREE_STRING_ALLOWLIST = {
    ("identify_patient", "first_name"),
    ("identify_patient", "last_name"),
    ("verify_identity", "full_name"),
    ("request_rx_refill", "medication_name"),
}


def test_healthcare_tool_inputs_are_closed() -> None:
    """Every healthcare input is an enum, format, pattern, number, or bool.

    Identity names and medication_name stay as patterned strings because they
    are facts the caller speaks. Prose fields (summaries, queries, reasons)
    are not allowed.
    """
    tools = json.loads(
        (ROOT / "industries" / "healthcare" / "tools.json").read_text()
    )["tools"]

    def closed(prop: dict[str, Any]) -> bool:
        if prop.get("enum") or prop.get("format") or prop.get("pattern"):
            return True
        types = prop.get("type")
        type_set = {types} if isinstance(types, str) else set(types or [])
        if type_set <= {"boolean", "integer", "number"}:
            return True
        if "array" in type_set:
            items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
            return bool(items) and closed(items)
        if "object" in type_set:
            nested = prop.get("properties") or {}
            return all(closed(child) for child in nested.values()) if nested else True
        return False

    leaks: list[str] = []
    for spec in tools:
        props = (spec.get("inputSchema") or {}).get("properties") or {}
        for key, prop in props.items():
            if (spec["name"], key) in _FREE_STRING_ALLOWLIST:
                assert prop.get("pattern") or prop.get("enum"), (spec["name"], key)
                continue
            if not closed(prop):
                leaks.append(f"{spec['name']}.{key}")
    assert leaks == []


def test_healthcare_expected_calls_match_schema() -> None:
    """Checked-in expected calls must only send keys the tool actually accepts."""
    tools = {
        spec["name"]: set((spec.get("inputSchema") or {}).get("properties") or {})
        for spec in json.loads(
            (ROOT / "industries" / "healthcare" / "tools.json").read_text()
        )["tools"]
    }
    extras: list[str] = []
    for path in sorted((ROOT / "industries" / "healthcare" / "tasks").glob("*/task.json")):
        task = json.loads(path.read_text())
        for call in task.get("exp_tool_calls") or []:
            name = call.get("name")
            allowed = tools.get(name)
            if allowed is None:
                extras.append(f"{path.parent.name} unknown tool {name}")
                continue
            extra = sorted(set(call.get("parameters") or {}) - allowed)
            if extra:
                extras.append(f"{path.parent.name} {name} extra {extra}")
    assert extras == []


_PIPE_ENUM = re.compile(
    r"\b([a-z_][a-z0-9_]*)\s*\(\s*([a-z][a-z0-9_]*(?:\s*\|\s*[a-z][a-z0-9_]*)+)\s*\)",
    re.IGNORECASE,
)


def test_healthcare_prompt_enums_match_schema() -> None:
    """Every enum token a prompt lists must exist on that field in tools.json."""
    tools = json.loads(
        (ROOT / "industries" / "healthcare" / "tools.json").read_text()
    )["tools"]
    by_key: dict[str, set[str]] = {}
    for spec in tools:
        props = (spec.get("inputSchema") or {}).get("properties") or {}
        for key, prop in props.items():
            if not isinstance(prop, dict):
                continue
            if prop.get("enum"):
                by_key.setdefault(key, set()).update(str(v) for v in prop["enum"])
            items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
            if items.get("enum"):
                by_key.setdefault(key, set()).update(str(v) for v in items["enum"])

    drift: list[str] = []
    prompt_dir = ROOT / "industries" / "healthcare" / "system-prompts"
    for path in sorted(prompt_dir.glob("*.md")):
        for match in _PIPE_ENUM.finditer(path.read_text()):
            field = match.group(1).lower()
            allowed = {value.lower() for value in by_key.get(field, ())}
            if not allowed:
                continue
            listed = {part.strip().lower() for part in match.group(2).split("|")}
            extra = sorted(listed - allowed)
            if extra:
                drift.append(f"{path.name} {field}: {extra}")
    assert drift == []


if __name__ == "__main__":
    test_dispatch_every_industry_tool()
    test_control_industry_rest_and_dispatch()
    test_legal_guards_survive_dispatch()
    test_healthcare_flow_through_dispatch()
    test_healthcare_calls_are_isolated()
    test_healthcare_tool_inputs_are_closed()
    test_healthcare_expected_calls_match_schema()
    test_healthcare_prompt_enums_match_schema()
    test_healthcare_prompt_demands_are_satisfiable()
    test_healthcare_list_locations_has_what_the_prompts_require()
    print("ok test_tool_server")
