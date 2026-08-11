"""Self-check: `python test_harness.py` (needs the harness venv on sys.path).

Covers the three things that silently break a whole run: the job-metadata parse
(wrong => no trace ever links to a simulation result), the blueprint load, and the
two-agent split (wrong => a "handoff" that is really one agent holding both prompts).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from livekit.agents import llm as lk_llm

from harness import Receptionist, Scheduler, load_blueprint, sim_result_id_from_job_metadata


def test_sim_result_id() -> None:
    # what Bluejay actually sends: a JSON string on job.metadata
    assert (
        sim_result_id_from_job_metadata(
            json.dumps({"X-Simulation-Result-Id": "710922", "X-Agent-Id": "30519"})
        )
        == "710922"
    )
    assert sim_result_id_from_job_metadata({"simulation_result_id": 710922}) == "710922"
    for empty in (None, "", "{}", "not json", json.dumps({"X-Agent-Id": "1"}), "[]"):
        assert sim_result_id_from_job_metadata(empty) is None, empty


def test_blueprint() -> None:
    bp = load_blueprint("control-industry")
    assert bp["start"] == "receptionist"
    assert set(bp["agents"]) == {"receptionist", "scheduler"}
    assert set(bp["catalog"]) == {"handoff_to_scheduler", "schedule_appointment", "end_call"}


def _tool_names(agent) -> set[str]:
    return set(lk_llm.ToolContext(agent.tools).function_tools)


def test_real_handoff() -> None:
    """The handoff must yield a *different* agent with the scheduler's own prompt and
    tools — the receptionist must not be able to book, and vice versa."""
    bp = load_blueprint("control-industry")
    hangup = asyncio.Event()

    receptionist_model = object()  # stands in for a per-agent RealtimeModel instance
    scheduler_model = object()
    receptionist = Receptionist(
        bp,
        hangup,
        llm=receptionist_model,
        make_scheduler=lambda: Scheduler(bp, hangup, llm=scheduler_model, opener="hi"),
    )
    assert _tool_names(receptionist) == {"handoff_to_scheduler", "end_call"}
    assert receptionist.instructions == bp["agents"]["receptionist"]["instructions"]

    scheduler = asyncio.run(receptionist.handoff_to_scheduler(None))
    assert isinstance(scheduler, Scheduler)
    assert _tool_names(scheduler) == {"schedule_appointment", "end_call"}
    assert scheduler.instructions == bp["agents"]["scheduler"]["instructions"]
    # each agent carries its own model => LiveKit cannot reuse the realtime session
    assert receptionist.llm is receptionist_model
    assert scheduler.llm is scheduler_model
    assert scheduler.llm is not receptionist.llm


if __name__ == "__main__":
    test_sim_result_id()
    test_blueprint()
    test_real_handoff()
    print("ok")
