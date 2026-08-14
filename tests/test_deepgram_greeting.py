"""Deepgram must speak the pack greeting and not feed Bluejay hold noise into VAD."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "deepgram"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_family(name: str, path: Path):
    sys.path.insert(0, str(FAMILY))
    for cached in ("harness", "pcm", "report", "adapters", "adapters.chirp"):
        sys.modules.pop(cached, None)
    return _load(name, path)


harness = _load_family("deepgram_harness", FAMILY / "harness.py")
load_blueprint = harness.load_blueprint
settings_payload = harness.settings_payload


def test_healthcare_blueprint_greeting() -> None:
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    assert bp["start"] == "reception"
    assert "Straus Dermatology" in (bp.get("greeting") or "")


def test_settings_payload_uses_pack_greeting_when_env_is_empty(monkeypatch) -> None:
    monkeypatch.setenv("DEEPGRAM_GREETING", "")
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    payload = settings_payload(bp)
    assert "Straus Dermatology" in payload["agent"]["greeting"]


def test_settings_payload_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("DEEPGRAM_GREETING", "Hello from env.")
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    payload = settings_payload(bp)
    assert payload["agent"]["greeting"] == "Hello from env."


def test_chirp_gates_pcm_on_bluejay_speech_events() -> None:
    text = (FAMILY / "adapters" / "chirp.py").read_text()
    assert "listening = False" in text
    assert "_forward_user_pcm" in text
    assert "speech.completed" in text
    assert "SettingsApplied" in text
    assert "chirp greeting=" in text
    assert "first_audio_ms" in text
    assert "CLIENT_MESSAGE_TIMEOUT" in text
    assert "PcmPacer" in text
    assert "_zeros_for" in text
    assert "KeepAlive" not in text
    assert "ECHO_SUPPRESS_S" in text
    assert "rms_open" in text
    assert "speech.started ignored echo" in text
    assert "PREROLL" in text


def test_forward_user_pcm_rms_and_trail() -> None:
    chirp = _load_family("deepgram_chirp", FAMILY / "adapters" / "chirp.py")
    _echo_window = chirp._echo_window
    _forward_user_pcm = chirp._forward_user_pcm

    assert _forward_user_pcm(ready=False, listening=True) is False
    assert _forward_user_pcm(ready=True, listening=False) is False
    assert _forward_user_pcm(ready=True, listening=True) is True
    assert _forward_user_pcm(ready=True, listening=False, trail=True) is True
    assert _forward_user_pcm(ready=True, listening=False, rms=400) is True
    assert _forward_user_pcm(ready=True, listening=False, rms=10) is False
    assert _echo_window(agent_open=True, agent_ended_at=0.0, now=10.0) is True
    assert _echo_window(agent_open=False, agent_ended_at=0.0, now=10.0) is False
    assert _echo_window(agent_open=False, agent_ended_at=9.0, now=9.5) is True
    assert _echo_window(agent_open=False, agent_ended_at=9.0, now=11.0) is False
