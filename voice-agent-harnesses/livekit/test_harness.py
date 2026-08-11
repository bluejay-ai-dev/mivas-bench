"""Self-check: `python test_harness.py` (needs the harness venv on sys.path).

Covers the two things that silently break a whole run: the job-metadata parse
(wrong => no trace ever links to a simulation result) and the blueprint load.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import combined_instructions, load_blueprint, sim_result_id_from_job_metadata


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
    combined = combined_instructions(bp)
    for agent in bp["agents"].values():
        assert agent["instructions"] in combined


if __name__ == "__main__":
    test_sim_result_id()
    test_blueprint()
    print("ok")
