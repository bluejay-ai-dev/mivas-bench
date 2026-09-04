"""pack_clock helper in isolation — no harness imported."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from pack_clock import (  # noqa: E402
    pack_today,
    read_pack_today,
    today_clock_line,
    with_pack_clock,
)


def test_reads_legal_and_healthcare_pins() -> None:
    assert read_pack_today("legal") == date(2026, 8, 1)
    assert read_pack_today("customer-support") == date(2026, 8, 1)
    assert read_pack_today("healthcare") == date(2026, 8, 19)
    assert read_pack_today("control-industry") is None


def test_pack_today_uses_pin_not_wall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIVAS_TODAY", raising=False)
    assert pack_today("legal") == date(2026, 8, 1)
    assert "August 1, 2026" in today_clock_line("legal")
    assert today_clock_line("legal").startswith("Today is Saturday")
    assert "August 19, 2026" in today_clock_line("healthcare")
    assert today_clock_line("healthcare").startswith("Today is Wednesday")


def test_control_industry_falls_back_to_wall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIVAS_TODAY", raising=False)
    monkeypatch.setattr("pack_clock._wall_today", lambda: date(2026, 9, 3))
    assert pack_today("control-industry") == date(2026, 9, 3)


def test_mivas_today_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIVAS_TODAY", "2026-07-15")
    assert pack_today("legal") == date(2026, 7, 15)
    monkeypatch.setenv("MIVAS_TODAY", "wall")
    monkeypatch.setattr("pack_clock._wall_today", lambda: date(2026, 9, 3))
    assert pack_today("legal") == date(2026, 9, 3)


def test_with_pack_clock_appends_once() -> None:
    line = today_clock_line("legal")
    first = with_pack_clock("Be helpful.", "legal")
    assert first.endswith(line)
    assert with_pack_clock(first, "legal") == first
