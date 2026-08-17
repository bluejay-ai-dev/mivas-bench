"""Session-tool policy shared by harnesses and expected-final-state.

`session: true` on a blueprint tool means the call ends when it fires.
`end_call` is harness-native (no industry POST). Human-transfer session tools
(`escalate_to_human`, `transfer_to_human`, …) still POST so the industry
records the escalation and the execute_tool span has a real result — there is
no human to join, so the harness hangs up immediately after.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# Session tools that never hit POST /tools/{name}.
PURE_SESSION = frozenset({"end_call"})


def is_pure_session(name: str) -> bool:
    return name in PURE_SESSION


def ends_session(name: str, entry: Mapping[str, Any] | None = None) -> bool:
    if name in PURE_SESSION:
        return True
    return bool(entry and entry.get("session"))


def still_dispatches(name: str, entry: Mapping[str, Any] | None = None) -> bool:
    """False for handoffs and end_call; True for industry tools and human-transfer session tools."""
    if name in PURE_SESSION:
        return False
    if entry and entry.get("handoff"):
        return False
    return True


def hangup_tool_names(agents: Iterable[Mapping[str, Any]]) -> set[str]:
    """Named session tools that POST then hang up (everything session except end_call)."""
    names: set[str] = set()
    for agent in agents:
        for tool in agent.get("tools") or []:
            name = tool.get("name")
            if name and tool.get("session") and name not in PURE_SESSION:
                names.add(name)
    return names
