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
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
INDUSTRIES = ("control-industry", "healthcare", "legal", "travel")


def _load_tool_server(industry: str):
    """Import industries/<industry>/tool_server.py under a unique module name,
    pointing MIVAS_DB_PATH at a throwaway file (DB_PATH is read at import)."""
    os.environ["MIVAS_DB_PATH"] = str(
        Path(tempfile.mkdtemp(prefix=f"mivas-{industry}-")) / "runtime.db"
    )
    path = ROOT / "industries" / industry / "tool_server.py"
    name = f"tool_server_{industry.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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


def test_dispatch_every_industry_tool() -> None:
    for industry in INDUSTRIES:
        module = _load_tool_server(industry)
        tools = json.loads(
            (ROOT / "industries" / industry / "tools.json").read_text()
        )["tools"]
        flags = _tool_flags(industry)
        with TestClient(module.app) as client:
            assert client.get("/health").status_code == 200, industry
            assert client.get("/state").status_code == 200, industry

            assert client.post("/tools/not_a_real_tool", json={"arguments": {}}).status_code == 404

            for spec in tools:
                name = spec["name"]
                entry = flags.get(name, {})
                resp = client.post(f"/tools/{name}", json={"arguments": _sample_args(spec)})
                if entry.get("session") or entry.get("handoff"):
                    assert resp.status_code == 404, f"{industry}/{name} must stay harness-native"
                    continue
                assert resp.status_code == 200, f"{industry}/{name}: {resp.text[:200]}"
                body = resp.json()
                assert isinstance(body, dict), f"{industry}/{name}"
                # each industry's declared envelope: ok/data/... or success/...
                assert "ok" in body or "success" in body, f"{industry}/{name}: {body}"

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
    module = _load_tool_server("control-industry")
    with TestClient(module.app) as client:
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
    module = _load_tool_server("legal")
    with TestClient(module.app) as client:
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
    module = _load_tool_server("healthcare")
    with TestClient(module.app) as client:
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


def test_travel_guards_survive_dispatch() -> None:
    module = _load_tool_server("travel")
    with TestClient(module.app) as client:
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
    test_healthcare_flow_through_dispatch()
    test_travel_guards_survive_dispatch()
    print("ok test_tool_server")
