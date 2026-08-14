"""AssemblyAI must speak the pack greeting and not feed Bluejay hold noise into VAD."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "assemblyai"
if str(FAMILY) not in sys.path:
    sys.path.insert(0, str(FAMILY))

from harness import load_blueprint, session_config  # noqa: E402


def test_healthcare_blueprint_greeting() -> None:
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    assert bp["start"] == "reception"
    assert "Straus Dermatology" in (bp.get("greeting") or "")


def test_session_config_uses_pack_greeting_when_env_is_empty(monkeypatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_GREETING", "")
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    cfg = session_config(bp)
    assert "Straus Dermatology" in cfg["greeting"]


def test_session_config_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_GREETING", "Hello from env.")
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    cfg = session_config(bp)
    assert cfg["greeting"] == "Hello from env."


def test_chirp_gates_pcm_on_bluejay_speech_events() -> None:
    text = (FAMILY / "adapters" / "chirp.py").read_text()
    assert "listening = False" in text
    assert "if not listening:" in text
    assert "speech.completed" in text
    assert "session_config(bp)" in text


def test_chirp_paces_outbound_pcm() -> None:
    text = (FAMILY / "adapters" / "chirp.py").read_text()
    assert "PcmPacer" in text
    assert "pacer.push" in text
    assert "wait_until_idle" in text
    assert "reset_clock" in text
    pcm = (FAMILY / "pcm.py").read_text()
    assert "CATCHUP_S = 1.0" in pcm


def test_session_config_handoff_update_skips_greeting(monkeypatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_GREETING", "")
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    cfg = session_config(bp, agent="scheduling", greeting="")
    assert cfg["greeting"] == ""
    assert "scheduling" in cfg["system_prompt"].lower() or "schedul" in cfg["system_prompt"].lower()


def test_chirp_handoff_rewires_and_ends_human_transfer() -> None:
    text = (FAMILY / "adapters" / "chirp.py").read_text()
    assert 'session_config(bp, agent=role, greeting="")' in text
    assert '"transfer_to_human"' in text
    assert '{"type": "session.end"}' in text
