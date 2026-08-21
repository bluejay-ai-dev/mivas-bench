"""Self-check for the Gemini LiveKit SIP worker."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("INDUSTRY", "control-industry")

import harness  # noqa: E402


def test_job_count_load() -> None:
    load = harness.job_count_load(1)
    assert load(SimpleNamespace(active_jobs=[])) == 0.0
    assert load(SimpleNamespace(active_jobs=[object()])) == 1.0
    assert load(SimpleNamespace(active_jobs=[object(), object()])) == 1.0
    third = harness.job_count_load(3)
    assert abs(third(SimpleNamespace(active_jobs=[object(), object()])) - 2 / 3) < 1e-9


def test_blueprint_and_greeting() -> None:
    bp = harness.load_blueprint("control-industry")
    assert bp["start"] in bp["agents"]
    assert bp["catalog"]
    health = harness.load_blueprint("healthcare")
    assert "Straus" in harness.greeting(health)


def test_agent_name() -> None:
    os.environ.pop("LIVEKIT_AGENT_NAME", None)
    os.environ.pop("MIVAS_SLUG", None)
    assert harness.agent_name("mivas-gemini-flash-live") == "mivas-gemini-flash-live"
    os.environ["MIVAS_SLUG"] = "gemini-flash-live-3-1-legal"
    assert harness.agent_name("mivas-gemini-flash-live") == "mivas-gemini-flash-live-3-1-legal"
    os.environ["LIVEKIT_AGENT_NAME"] = "explicit"
    assert harness.agent_name("mivas-gemini-flash-live") == "explicit"
    os.environ.pop("LIVEKIT_AGENT_NAME", None)
    os.environ.pop("MIVAS_SLUG", None)


def test_kick_sends_realtime_text() -> None:
    sent: list[object] = []

    class Rt:
        def _send_client_event(self, event: object) -> None:
            sent.append(event)

    session = SimpleNamespace(_activity=SimpleNamespace(_rt_session=Rt()))
    harness.kick(session, "Hello.")
    assert sent
    event = sent[0]
    # LiveClientRealtimeInput(text=...) is the only client event 3.1 generates from
    assert "Hello." in event.text
    assert getattr(event, "turns", None) is None


if __name__ == "__main__":
    test_job_count_load()
    test_blueprint_and_greeting()
    test_agent_name()
    test_kick_sends_realtime_text()
    print("ok")
