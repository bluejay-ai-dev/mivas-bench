"""Cartesia Line must deploy a per-pack agent, not the /app/industry folder name."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "cartesia"
if str(FAMILY) not in sys.path:
    sys.path.insert(0, str(FAMILY))

from harness import industry_name, load_blueprint  # noqa: E402


def test_industry_name_uses_INDUSTRY_when_mount_is_app_industry(tmp_path, monkeypatch) -> None:
    d = tmp_path / "industry"
    d.mkdir()
    monkeypatch.setenv("INDUSTRY", "healthcare")
    monkeypatch.setenv("INDUSTRY_DIR", str(d))
    assert industry_name(d) == "healthcare"


def test_industry_name_from_pack_path(monkeypatch) -> None:
    monkeypatch.delenv("INDUSTRY", raising=False)
    assert industry_name(ROOT / "industries" / "healthcare") == "healthcare"


def test_healthcare_blueprint_greeting() -> None:
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    assert bp["start"] == "reception"
    assert "Straus Dermatology" in (bp.get("greeting") or "")


def test_line_agent_reads_pack_greeting() -> None:
    text = (FAMILY / "line_agent" / "main.py").read_text()
    assert "BLUEPRINT.get(\"greeting\")" in text
    assert "_introduction" in text
    assert "next_stack" in text
    assert "_continue_as_handoff" in text
    assert "is_background=False" in text
    assert "agent_as_handoff(" not in text


def test_chirp_gates_pcm_on_bluejay_speech_events() -> None:
    text = (FAMILY / "adapters" / "chirp.py").read_text()
    assert "listening = False" in text
    assert "if not listening:" in text
    assert "speech.completed" in text
    assert 'audio["held"]' in text


def test_healthcare_handoff_graph_is_cyclic_but_walkable() -> None:
    """reception↔identity↔scheduling loops; Line get_agent must skip back-edges."""
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    seen: set[str] = set()

    def walk(name: str, stack: frozenset[str]) -> None:
        seen.add(name)
        nxt = stack | {name}
        for t in bp["agents"][name]["tools"]:
            if not t.get("handoff"):
                continue
            target = t["handoff_to"]
            if target in nxt:
                continue
            walk(target, nxt)

    walk(bp["start"], frozenset())
    assert "reception" in seen
    assert "identity" in seen
    assert "scheduling" in seen
    assert "billing" in seen
    assert "cosmetic" in seen
    assert "coverage" in seen
    assert "clinical" in seen
