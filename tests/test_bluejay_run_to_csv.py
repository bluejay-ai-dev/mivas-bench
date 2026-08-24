"""CSV export of one simulation result / conversation — no live Bluejay, no goal-eval columns."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "bluejay_run_to_csv", ROOT / "scripts" / "bluejay_run_to_csv.py"
)
assert _SPEC is not None and _SPEC.loader is not None
exp = importlib.util.module_from_spec(_SPEC)
sys.modules["bluejay_run_to_csv"] = exp
_SPEC.loader.exec_module(exp)


def _scored_result() -> dict:
    return {
        "result_id": "778677",
        "case_key": "C1-E1",
        "digital_human_id": 537891,
        "status": "COMPLETED",
        "pending": False,
        "void_reason": None,
        "mark": "FAIL",
        "conversation_index": 1,
        "state": {"passed": True, "skipped": False, "note": None},
        "call": {
            "passed": False,
            "score": 0.5,
            "missing": ["transfer_to_coverage"],
            "hit": ["check_plan_accepted"],
        },
        "handoff": {
            "passed": False,
            "verdict": "incomplete",
            "score": 0.0,
            "expected": ["transfer_to_coverage"],
            "actual": [],
        },
        "actual_tools": ["check_plan_accepted", "end_call"],
        "actual_tool_calls": [
            {
                "name": "check_plan_accepted",
                "parameters": {"carrier": "Aetna", "start": "T09:00:00"},
                "start_offset_ms": 4120,
            },
            {"name": "end_call", "parameters": {}},
        ],
        "passed": False,
        "agent_chars": 179,
        "task": {
            "task_name": "C1-E1: Aetna coverage at Park Avenue",
            "exp_tool_calls": [
                {"name": "check_plan_accepted", "parameters": {"carrier": "Aetna"}},
                {"name": "transfer_to_coverage"},
            ],
            "exp_handoff_path": ["transfer_to_coverage"],
            "metadata": {
                "category": "C1",
                "difficulty": "easy",
                "audio_condition": "perfect",
            },
            "exp_db_state": {
                "patients": [],
                "appointments": [{"id": 1, "start": "2026-08-20T09:00"}],
                "waitlist": [],
            },
        },
        "actual_state": {
            "patients": [],
            "appointments": [{"id": 1, "start": "2026-08-20T09:00:00"}],
            "waitlist": [],
        },
        "digital_human": {
            "name": "C1-E1",
            "test_name": "C1-E1: Aetna coverage at Park Avenue",
        },
        "detail": {
            "id": 778677,
            "duration": 32,
            "start_time": "2026-08-20T03:10:42.452000Z",
            "end_time": "2026-08-20T03:11:15.337000Z",
            "transcript_url": "https://example.test/transcript.json",
            "recording_url": "https://example.test/call.wav",
            "metrics": [
                {"name": "num_turns", "value": 5},
                {"name": "avg_agent_latency", "value": 410},
                {"name": "agent_interruption_count", "value": 1},
            ],
            "custom_metrics": [
                {
                    "name": "Prompt adherence (1-5)",
                    "response_type": "quantitative",
                    "response_value": "1",
                    "reasoning": "drifted from the Park Avenue script",
                },
                {
                    "name": "Task completion (1-5)",
                    "response_type": "quantitative",
                    "response_value": "5",
                    "reasoning": "answered the office-level coverage question",
                },
                {
                    "name": "Premature call end",
                    "response_type": "yes_no",
                    "response_value": "no",
                    "reasoning": "caller ended after the answer",
                },
            ],
            "evaluations": [
                {
                    "goal_success": False,
                    "goal_reasoning": "must never appear in the csv",
                    "num_turns": 5,
                    "sentiment_label": "neutral",
                    "sentiment_score": 0.1,
                }
            ],
        },
    }


def _row(scored: dict | None = None, **kwargs):
    defaults = {
        "run_id": "242261",
        "simulation_id": "30975",
        "agent_id": "34182",
        "industry": "healthcare",
        "transcript_lines": ["AGENT: Park Avenue accepts Aetna.", "USER: That's all."],
        "harness": "openai/realtime-2.1",
        "fetch_costs": False,
    }
    defaults.update(kwargs)
    return exp.result_row(scored or _scored_result(), **defaults)


def test_result_row_header_and_cells() -> None:
    row = _row()
    text = exp.rows_to_csv([row])
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert list(csv.reader(io.StringIO(text)))[0] == exp.HEADERS
    assert len(parsed) == 1
    got = parsed[0]
    assert got["run_id"] == "242261"
    assert got["simulation_id"] == "30975"
    assert got["agent_id"] == "34182"
    assert got["result_id"] == "778677"
    assert got["digital_human_id"] == "537891"
    assert got["industry"] == "healthcare"
    assert got["case_key"] == "C1-E1"
    assert got["task_path"] == "industries/healthcare/tasks/C1-E1/task.json"
    assert got["task_dir"] == "industries/healthcare/tasks/C1-E1"
    assert got["dh_name"] == "C1-E1"
    assert got["category"] == "C1"
    assert got["difficulty"] == "easy"
    assert got["audio_condition"] == "perfect"
    assert got["conversation_index"] == "1"
    assert got["mark"] == "FAIL"
    assert got["pending"] == "false"
    assert got["combined_pass"] == "false"
    assert got["tools_pass"] == "false"
    assert got["handoff_pass"] == "false"
    assert got["hangup_db_pass"] == "true"
    assert got["tools_score"] == "0.5"
    assert got["handoff_verdict"] == "incomplete"
    assert got["handoff_score"] == "0.0"
    assert got["missing_tools"] == "transfer_to_coverage"
    assert got["hit_tools"] == "check_plan_accepted"
    assert got["extra_tools"] == "end_call"
    assert got["actual_tools"] == "check_plan_accepted;end_call"
    assert got["expected_tools"] == "check_plan_accepted;transfer_to_coverage"
    assert got["prompt_adherence"] == "1"
    assert got["task_completion"] == "5"
    assert got["premature_end"] == "false"
    assert got["prompt_adherence_reasoning"] == "drifted from the Park Avenue script"
    assert got["task_completion_reasoning"] == "answered the office-level coverage question"
    assert got["premature_end_reasoning"] == "caller ended after the answer"
    assert got["duration_s"] == "32"
    assert got["num_turns"] == "5"
    assert got["builtin_num_turns"] == "5"
    assert got["builtin_avg_agent_latency"] == "410"
    assert got["builtin_agent_interruption_count"] == "1"
    assert got["eval_sentiment_label"] == "neutral"
    assert got["recording_url"] == "https://example.test/call.wav"
    assert "Park Avenue accepts Aetna." in got["transcript"]
    assert "That's all." in got["transcript"]


def test_tool_call_json_keeps_args() -> None:
    row = _row()
    expected = json.loads(row["expected_tool_calls_json"])
    actual = json.loads(row["actual_tool_calls_json"])
    assert expected[0]["name"] == "check_plan_accepted"
    assert expected[0]["parameters"]["carrier"] == "Aetna"
    assert actual[0]["parameters"]["start"] == "T09:00:00"
    assert actual[0]["start_offset_ms"] == 4120
    parsed = list(csv.DictReader(io.StringIO(exp.rows_to_csv([row]))))[0]
    assert json.loads(parsed["expected_tool_calls_json"]) == expected
    assert json.loads(parsed["actual_tool_calls_json"]) == actual


def test_hangup_expected_actual_round_trip() -> None:
    scored = _scored_result()
    row = _row(scored)
    expected = json.loads(row["hangup_expected_json"])
    actual = json.loads(row["hangup_actual_json"])
    assert expected["appointments"][0]["start"] == "2026-08-20T09:00"
    assert actual["appointments"][0]["start"] == "2026-08-20T09:00:00"
    assert row["hangup_diff"] == ""
    assert row["hangup_note"] == ""
    parsed = list(csv.DictReader(io.StringIO(exp.rows_to_csv([row]))))[0]
    assert json.loads(parsed["hangup_expected_json"]) == expected
    assert json.loads(parsed["hangup_actual_json"]) == actual

    scored["actual_state"] = {
        "patients": [],
        "appointments": [{"id": 1, "start": "2026-08-20T10:00:00"}],
        "waitlist": [],
    }
    scored["state"] = {"passed": False, "skipped": False, "note": None}
    failed = _row(scored)
    diff = json.loads(failed["hangup_diff"])
    assert "appointments" in diff
    assert diff["appointments"]["expected_count"] == 1
    assert diff["appointments"]["actual_count"] == 1


def test_hangup_note_when_skipped() -> None:
    scored = _scored_result()
    scored["actual_state"] = None
    scored["state"] = {"passed": None, "skipped": True, "note": "no hangup dump to compare"}
    row = _row(scored)
    assert row["hangup_db_pass"] == ""
    assert row["hangup_actual_json"] == ""
    assert row["hangup_diff"] == ""
    assert row["hangup_note"] == "no hangup dump to compare"


def test_excluded_goal_eval_columns_absent() -> None:
    row = _row()
    text = exp.rows_to_csv([row])
    header = list(csv.reader(io.StringIO(text)))[0]
    for name in exp.EXCLUDED_COLUMNS:
        assert name not in header
        assert name not in row
    assert "goal_success" not in text
    assert "goal_reasoning" not in text
    assert "tests_passed" not in text
    assert "must never appear in the csv" not in text


def test_extra_custom_metric_value_and_reasoning() -> None:
    scored = _scored_result()
    scored["detail"]["custom_metrics"].append({
        "name": "Politeness",
        "response_type": "quantitative",
        "response_value": "4",
        "reasoning": "warm close",
    })
    scored["detail"]["custom_metrics"].append({
        "name": "Goal success",
        "response_type": "yes_no",
        "response_value": "no",
        "reasoning": "must never appear in the csv",
    })
    row = _row(scored)
    text = exp.rows_to_csv([row])
    header = list(csv.reader(io.StringIO(text)))[0]
    assert "politeness" in header
    assert "politeness_reasoning" in header
    assert row["politeness"] == "4"
    assert row["politeness_reasoning"] == "warm close"
    assert "goal_success" not in header
    assert "goal_success_reasoning" not in header
    assert "must never appear in the csv" not in text


def test_transcript_newlines_round_trip() -> None:
    scored = _scored_result()
    row = _row(
        scored,
        run_id="1",
        simulation_id="2",
        agent_id="3",
        transcript_lines=["AGENT: line one", "USER: line two"],
    )
    assert "\n" in row["transcript"]
    parsed = list(csv.DictReader(io.StringIO(exp.rows_to_csv([row]))))
    assert parsed[0]["transcript"] == "AGENT: line one\nUSER: line two"


def test_void_result_leaves_combined_pass_empty() -> None:
    scored = _scored_result()
    scored["void_reason"] = "no conversation (NO_ANSWER)"
    scored["status"] = "NO_ANSWER"
    scored["mark"] = "VOID"
    row = _row(scored, run_id="1", simulation_id="2", agent_id="3", transcript_lines=[])
    assert row["combined_pass"] == ""
    assert row["void_reason"] == "no conversation (NO_ANSWER)"
    assert row["mark"] == "VOID"


def test_stashed_transcript_lines_skip_refetch() -> None:
    scored = _scored_result()
    scored["transcript_lines"] = ["AGENT: stashed once"]
    scored["detail"]["transcript_url"] = "https://example.test/would-refetch.json"

    def boom(_result):
        raise AssertionError("should not refetch transcript_url")

    original = exp.verify_task_run.result_transcript_lines
    exp.verify_task_run.result_transcript_lines = boom
    try:
        row = exp.result_row(
            scored,
            run_id="1",
            simulation_id="2",
            agent_id="3",
            industry="healthcare",
        )
    finally:
        exp.verify_task_run.result_transcript_lines = original
    assert row["transcript"] == "AGENT: stashed once"


def test_assign_conversation_indexes_are_one_based_per_case() -> None:
    rows = [
        {"digital_human_id": 1, "case_key": "C1-E1"},
        {"digital_human_id": 1, "case_key": "C1-E1"},
        {"digital_human_id": 2, "case_key": "C1-E1-BG"},
    ]
    exp.assign_conversation_indexes(rows)
    assert [row["conversation_index"] for row in rows] == [1, 2, 1]


def test_cost_columns_estimated_without_trace() -> None:
    row = _row()
    for name in exp.eval_costs.COST_COLUMNS:
        assert name in exp.HEADERS
        assert name in row
    assert row["llm_cost_source"] == "estimated"
    assert float(row["llm_cost_usd"]) > 0
    assert float(row["llm_cost_per_hour_usd"]) > 0
    utterances = json.loads(row["utterance_costs_json"])
    assert utterances[0]["role"] == "agent"
    assert utterances[0]["cost_usd"] is not None
    assert utterances[1]["role"] == "caller"
    assert utterances[1]["cost_usd"] is None


def test_cost_columns_grok_per_minute() -> None:
    row = _row(harness="grok/voice")
    assert row["llm_cost_source"] == "per_minute"
    assert float(row["llm_cost_usd"]) == 0.042667
    assert float(row["llm_cost_per_hour_usd"]) == 4.8


def test_cost_columns_from_token_spans() -> None:
    spans = [
        {
            "name": "model",
            "attributes": {
                "gen_ai.usage.input_audio_tokens": 1000,
                "gen_ai.usage.output_audio_tokens": 500,
                "mivas.transcript": "Park Avenue accepts Aetna.",
            },
        }
    ]
    row = _row(harness="openai/realtime-2.1", cost_spans=spans)
    assert row["llm_cost_source"] == "tokens"
    # 1000 * 32 / 1e6 + 500 * 64 / 1e6
    assert float(row["llm_cost_usd"]) == 0.064
    utterances = json.loads(row["utterance_costs_json"])
    assert utterances[0]["cost_usd"] == 0.064
