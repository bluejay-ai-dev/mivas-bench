"""Human-transfer session tools POST then hang up; end_call stays local."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from session_tools import ends_session, hangup_tool_names, is_pure_session, still_dispatches  # noqa: E402


def test_end_call_is_pure_session() -> None:
    assert is_pure_session("end_call")
    assert ends_session("end_call", {"session": True})
    assert not still_dispatches("end_call", {"session": True})


def test_human_transfer_dispatches_and_ends() -> None:
    entry = {"name": "escalate_to_human", "session": True}
    assert ends_session("escalate_to_human", entry)
    assert still_dispatches("escalate_to_human", entry)
    assert not is_pure_session("escalate_to_human")


def test_blueprint_hangup_names() -> None:
    expected = {
        "customer-support": {"escalate_to_human"},
        "finance": {"escalate_to_human"},
        "legal": {"escalate_to_human"},
        "travel": {"escalate_to_human"},
        "healthcare": {"transfer_to_human"},
        "control-industry": set(),
    }
    for industry, names in expected.items():
        bp = json.loads((ROOT / "industries" / industry / "agent_blueprint.json").read_text())
        assert hangup_tool_names(bp["agents"]) == names, industry
