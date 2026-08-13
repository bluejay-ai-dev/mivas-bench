"""Twilio harness must load healthcare (and other packs), not only control-industry."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "twilio"
if str(FAMILY) not in sys.path:
    sys.path.insert(0, str(FAMILY))

from adapters.conversationrelay import build_app  # noqa: E402
from harness import demo, load_blueprint, sim_id_from_mapping, twilio_sip_uri, welcome_greeting  # noqa: E402


def test_demo_healthcare() -> None:
    demo(ROOT / "industries" / "healthcare")
    bp = load_blueprint(ROOT / "industries" / "healthcare")
    assert bp["start"] == "reception"
    assert "identity" in bp["agents"]


def test_welcome_healthcare(monkeypatch) -> None:
    monkeypatch.setenv("INDUSTRY", "healthcare")
    monkeypatch.setenv("INDUSTRY_DIR", str(ROOT / "industries" / "healthcare"))
    monkeypatch.setenv("TWILIO_WELCOME_GREETING", "Welcome to Bluejay's Repair Services!")
    assert welcome_greeting() == "Thank you for calling Straus Dermatology."


def test_sip_uri_is_one_domain_per_industry() -> None:
    assert twilio_sip_uri("healthcare") == (
        "sip:mivas@mivas-twilio-healthcare.sip.twilio.com"
    )
    assert twilio_sip_uri("control-industry") == (
        "sip:mivas@mivas-twilio-control-industry.sip.twilio.com"
    )


def test_sim_id_from_twilio_sip_form() -> None:
    assert sim_id_from_mapping({"SipHeader_X-Simulation-Result-Id": "720488"}) == "720488"
    assert sim_id_from_mapping({"SipHeader_X_Simulation_Result_Id": "720488"}) == "720488"
    assert sim_id_from_mapping({"simulation_result_id": "720488"}) == "720488"
    assert (
        sim_id_from_mapping(
            {"SipHeader_X-Custom-Headers": json.dumps({"X-Simulation-Result-Id": "720488"})}
        )
        == "720488"
    )
    assert sim_id_from_mapping({"CallSid": "CA123", "To": "sip:mivas@example.sip.twilio.com"}) == ""


def test_twiml_stamps_bluejay_sim_id(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_URL", "https://twilio-test.example")
    monkeypatch.setenv("INDUSTRY", "control-industry")
    from fastapi.testclient import TestClient

    client = TestClient(build_app("control-industry"))
    r = client.post(
        "/",
        data={"CallSid": "CA1", "SipHeader_X-Simulation-Result-Id": "720488"},
    )
    assert r.status_code == 200
    assert "ConversationRelay" in r.text
    assert "simulation_result_id=720488" in r.text
    assert 'name="simulation_result_id" value="720488"' in r.text

    got = client.get(
        "/",
        params={"CallSid": "CA2", "SipHeader_X-Simulation-Result-Id": "720489"},
    )
    assert got.status_code == 200
    assert "simulation_result_id=720489" in got.text
