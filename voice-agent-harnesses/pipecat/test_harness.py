"""Self-check: `python test_harness.py` (no Pipecat runtime import required).

Covers pack greeting, today clock, Daily pinless SIP URI, and
Gemini handoff openers derived from the *target* prompt.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness
from harness import sip_uri, sim_result_id_from_job_metadata, sim_result_id_from_participant


def test_sim_result_id() -> None:
    import json
    from types import SimpleNamespace

    assert (
        sim_result_id_from_job_metadata(
            json.dumps({"X-Simulation-Result-Id": "710922", "X-Agent-Id": "30538"})
        )
        == "710922"
    )
    assert sim_result_id_from_job_metadata({"simulation_result_id": 710922}) == "710922"
    for empty in (None, "", "{}", "not json", json.dumps({"X-Agent-Id": "1"}), "[]"):
        assert sim_result_id_from_job_metadata(empty) is None, empty
    sip = SimpleNamespace(
        attributes={"sip.h.x-simulation-result-id": "736069"},
        metadata="",
    )
    assert sim_result_id_from_participant(sip) == "736069"


def test_control_industry_split() -> None:
    bp = harness.load_blueprint("control-industry")
    assert bp["start"] == "receptionist"
    assert harness.tool_names(bp, "receptionist") == ["handoff_to_scheduler", "end_call"]
    assert harness.tool_names(bp, "scheduler") == ["schedule_appointment", "end_call"]
    assert harness.handoff_target(bp, "receptionist", "handoff_to_scheduler") == "scheduler"


def test_pack_greeting_and_clock() -> None:
    control = harness.load_blueprint("control-industry")
    hc = harness.load_blueprint("healthcare")
    assert harness.pack_greeting(control) == harness.GREETING
    assert harness.pack_greeting(hc).startswith("Thank you for calling Straus")
    assert "Straus Dermatology" in hc["greeting"]

    rec = harness.instructions(control, "receptionist")
    assert control["agents"]["receptionist"]["instructions"] in rec
    assert harness.today_clock() in rec
    assert harness.GREETING in rec

    start = harness.instructions(hc, hc["start"])
    assert harness.pack_greeting(hc) in start
    assert harness.today_clock() in start
    silent = harness.instructions(hc, hc["start"], speak_first=False)
    assert harness.pack_greeting(hc) not in silent
    assert harness.today_clock() in silent


def test_agent_opener() -> None:
    control = harness.load_blueprint("control-industry")
    assert harness.agent_opener(control, "scheduler") == (
        "Hey, when do you want to schedule your repair appointment?"
    )
    hc = harness.load_blueprint("healthcare")
    # healthcare prompts have no step-1 Ask/Say line; never inherit reception's greeting
    assert harness.agent_opener(hc, "scheduling") == harness.GENERIC_OPENER
    assert harness.pack_greeting(hc) not in harness.agent_opener(hc, "scheduling")


def test_generic_industries() -> None:
    for industry in ("healthcare", "legal", "travel"):
        bp = harness.load_blueprint(industry)
        start = harness.tool_names(bp, bp["start"])
        expected = [t["name"] for t in bp["agents"][bp["start"]]["tools"] if t["name"] in bp["catalog"]]
        assert start == expected, (industry, set(start) ^ set(expected))
        assert harness.pack_greeting(bp)


def test_resolve_agent_name() -> None:
    os.environ.pop("LIVEKIT_AGENT_NAME", None)
    os.environ.pop("MIVAS_SLUG", None)
    assert harness.resolve_agent_name("mivas-pipecat-cascaded") == "mivas-pipecat-cascaded"
    os.environ["MIVAS_SLUG"] = "pipecat-cascaded-healthcare"
    assert harness.resolve_agent_name("mivas-pipecat-cascaded") == (
        "mivas-pipecat-cascaded-healthcare"
    )
    os.environ["LIVEKIT_AGENT_NAME"] = "explicit-name"
    assert harness.resolve_agent_name("mivas-pipecat-cascaded") == "explicit-name"
    os.environ.pop("LIVEKIT_AGENT_NAME", None)
    os.environ.pop("MIVAS_SLUG", None)

    prev_sip = os.environ.pop("DAILY_SIP_URI", None)
    try:
        assert sip_uri() is None
        os.environ["DAILY_SIP_URI"] = "sip:abc@daily-pinless.example"
        assert sip_uri() == "sip:abc@daily-pinless.example"
    finally:
        os.environ.pop("DAILY_SIP_URI", None)
        if prev_sip is not None:
            os.environ["DAILY_SIP_URI"] = prev_sip


def test_daily_dialin_not_livekit() -> None:
    bot = (Path(__file__).parent / "bot.py").read_text()
    assert "def serve(" in bot
    assert "DailyTransport" in bot
    assert "LiveKitTransport" not in bot
    # Request must be a module global: `from __future__ import annotations`
    # otherwise FastAPI treats `request` as a required query param (422).
    assert "from fastapi import HTTPException, Request" in bot
    disp = (Path(__file__).parent / "dispatcher.py").read_text()
    assert "PIPECAT_WORKER_URL_TEMPLATE" in disp
    assert "PIPECAT_WORKER_PODS_TEMPLATE" in disp
    assert "_RETRY_CODES" in disp


if __name__ == "__main__":
    test_sim_result_id()
    test_control_industry_split()
    test_pack_greeting_and_clock()
    test_agent_opener()
    test_generic_industries()
    test_resolve_agent_name()
    test_daily_dialin_not_livekit()
    print("ok")
