"""Automated smoke tests for control-industry state API + SQLite."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "industries" / "control-industry"))

from tool_server import app  # noqa: E402


def test_create_appointment() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/state").json() == {"appointments": []}
        assert client.get("/appointments").json() == []

        created = client.post("/appointments", json={"date": "08/15/2026"})
        assert created.status_code == 201
        body = created.json()
        assert body["date"] == "08/15/2026"
        assert "id" in body

        state = client.get("/state").json()
        assert len(state["appointments"]) == 1
        assert state["appointments"][0]["date"] == "08/15/2026"


def test_no_tools_mirror_routes() -> None:
    with TestClient(app) as client:
        assert client.post("/tools/schedule_appointment", json={"date": "08/15/2026"}).status_code == 404
        assert client.post("/tools/end_call", json={"reason": "done"}).status_code == 404


if __name__ == "__main__":
    test_create_appointment()
    test_no_tools_mirror_routes()
    print("ok test_tool_server")
