"""Scenario clock: one TODAY across tools, fixtures, DHs, and hangup copy."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from pack_clock import pack_today, read_pack_today  # noqa: E402

INDUSTRIES = ROOT / "industries"
HEALTHCARE_SLOTS = {"2026-08-24T09:00", "2026-08-25T11:30", "2026-08-26T14:00"}
BOOKING_TOOLS = {
    "book_appointment",
    "book_cosmetic_consult",
    "schedule_allergy_service",
    "reschedule_appointment",
    "book_service_appointment",
    "hold_evaluation",
    "confirm_evaluation",
}


def test_pack_pins_are_in_the_past_of_the_wall_calendar() -> None:
    """The benchmark run failed because wall-clock September saw August fixtures as expired."""
    assert read_pack_today("healthcare") == date(2026, 8, 19)
    assert read_pack_today("legal") == date(2026, 8, 1)
    assert read_pack_today("customer-support") == date(2026, 8, 1)


def test_no_say_goodbye_first() -> None:
    for path in INDUSTRIES.glob("*/tools.json"):
        text = path.read_text()
        assert "goodbye first" not in text.lower(), path
        if '"name": "end_call"' in text:
            assert "after this tool returns" in text, path


def test_expected_healthcare_bookings_are_published_future_slots() -> None:
    today = pack_today("healthcare")
    for path in (INDUSTRIES / "healthcare" / "tasks").glob("*/task.json"):
        task = json.loads(path.read_text())
        for call in task.get("exp_tool_calls") or []:
            name = call.get("name")
            params = call.get("parameters") or {}
            start = params.get("start") or params.get("new_start")
            if not start or name not in BOOKING_TOOLS:
                continue
            start_date = date.fromisoformat(str(start)[:10])
            assert start_date >= today, f"{path.parent.name} {name} {start} is before pack today"
            if name in {"book_appointment", "book_cosmetic_consult", "reschedule_appointment"}:
                assert str(start)[:16] in HEALTHCARE_SLOTS, (
                    f"{path.parent.name} {name} {start} is not a published slot"
                )


def test_relative_date_tasks_name_the_pack_calendar() -> None:
    for key, needle in (
        ("C2-H1", "August 20"),
        ("C2-M1", "August 20"),
        ("C2-H4", "August 20"),
        ("C5-H2", "August 20"),
        ("C2-H3", "August 21"),
    ):
        task = json.loads((INDUSTRIES / "healthcare" / "tasks" / key / "task.json").read_text())
        blob = json.dumps(task)
        assert needle in blob, key
        assert "tomorrow" not in blob.lower(), key


def test_hangup_and_simulator_pins_cannot_skip_work() -> None:
    old_cs_wrap = "after the thing you called about is already done. NOT when still working"
    new_cs_wrap = "has already finished the original request you called about, confirmed that outcome"
    for path in (INDUSTRIES / "customer-support" / "tasks").glob("*/task.json"):
        text = path.read_text()
        assert old_cs_wrap not in text, path.parent.name
        if new_cs_wrap in text:
            assert "NOT during identity" in text, path.parent.name

    c5 = json.loads((INDUSTRIES / "legal" / "tasks" / "C5-H2" / "task.json").read_text())
    assert "Do not mention cancelling" in c5["intent"]
    assert "NOT during screening" in json.dumps(c5["scripted_responses"])

    c4 = json.loads((INDUSTRIES / "legal" / "tasks" / "C4-H3" / "task.json").read_text())
    assert "NOT during screening" in json.dumps(c4["scripted_responses"])
    assert "before those three writes" in json.dumps(c4["scripted_responses"])
