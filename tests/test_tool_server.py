"""Automated smoke tests for control-industry tool_server + SQLite state."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "industries" / "control-industry"))

from tool_server import app  # noqa: E402


def test_schedule_appointment_writes_state() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/state").json() == {"appointments": [], "call_events": []}

        booked = client.post(
            "/tools/schedule_appointment",
            json={"date": "08/15/2026"},
        )
        assert booked.status_code == 200
        assert booked.json() == {"success": True, "date": "08/15/2026"}

        state = client.get("/state").json()
        assert len(state["appointments"]) == 1
        assert state["appointments"][0]["date"] == "08/15/2026"


def test_end_call_writes_event() -> None:
    with TestClient(app) as client:
        ended = client.post("/tools/end_call", json={"reason": "done"})
        assert ended.status_code == 200
        assert ended.json() == {"success": True}
        events = client.get("/state").json()["call_events"]
        assert len(events) == 1
        assert events[0]["reason"] == "done"


def test_unknown_tool_404() -> None:
    with TestClient(app) as client:
        resp = client.post("/tools/not_a_real_tool", json={})
        assert resp.status_code == 404


if __name__ == "__main__":
    test_schedule_appointment_writes_state()
    test_end_call_writes_event()
    test_unknown_tool_404()
    print("ok test_tool_server")
