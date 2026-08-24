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
        "audio_eval": None,
        "cost": None,
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


def _no_dump(**detail_extra) -> dict:
    scored = _scored_result()
    scored["actual_state"] = None
    scored["state"] = {"passed": None, "skipped": True,
                       "note": "no hangup dump for this result"}
    scored["call"] = {"passed": True, "score": 1.0, "missing": [], "hit": []}
    scored["handoff"] = {"passed": True, "verdict": "exact", "score": 1.0,
                         "expected": [], "actual": []}
    scored["passed"] = True
    scored["detail"]["trace_ids"] = ["trace-1"]
    scored["detail"].update(detail_extra)
    return scored


def test_missing_dump_on_traced_call_fails_hangup_and_combined() -> None:
    scored = _no_dump()
    exp.apply_csv_mark(scored)
    row = _row(scored)
    assert row["hangup_db_pass"] == "false"
    assert row["combined_pass"] == "false"
    assert row["mark"] == "FAIL"
    assert row["hangup_note"] == "no hangup dump for this result"


def test_missing_dump_on_void_call_stays_blank() -> None:
    scored = _no_dump()
    scored["status"] = "NO_ANSWER"
    scored["detail"]["trace_ids"] = []
    exp.apply_csv_mark(scored)
    row = _row(scored)
    assert row["mark"] == "VOID"
    assert row["hangup_db_pass"] == ""
    assert row["combined_pass"] == ""


def test_missing_dump_without_expected_state_stays_skipped() -> None:
    scored = _no_dump()
    scored["task"] = {k: v for k, v in scored["task"].items() if k != "exp_db_state"}
    exp.apply_csv_mark(scored)
    row = _row(scored)
    assert row["hangup_db_pass"] == ""
    assert row["combined_pass"] == "true"


def test_hangup_mismatch_fails_combined_when_tools_and_handoff_pass() -> None:
    scored = _no_dump()
    scored["actual_state"] = {"patients": [], "appointments": [], "waitlist": []}
    scored["state"] = {"passed": False, "skipped": False, "note": None}
    scored["passed"] = False
    exp.apply_csv_mark(scored)
    row = _row(scored)
    assert (row["tools_pass"], row["handoff_pass"]) == ("true", "true")
    assert row["hangup_db_pass"] == "false"
    assert row["combined_pass"] == "false"


def test_resolve_actuals_dir_pulls_when_this_run_has_no_dumps(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(exp, "ROOT", tmp_path)
    monkeypatch.setenv("MIVAS_SNAPSHOT_BUCKET", "mivas-bench-call-dbs")
    root = tmp_path / "actual-final-state"
    (root / "other-pair" / "1" / "2" / "3").mkdir(parents=True)
    calls: list[tuple] = []

    def fake_pull(run_id, slug, out_dir):
        calls.append((run_id, slug, out_dir))
        dest = out_dir / slug / str(run_id) / "dh" / "res"
        dest.mkdir(parents=True)
        (dest / "final.json").write_text("{}")
        return {"run_id": run_id}

    monkeypatch.setattr(exp.verify_task_run, "_pull_actuals", fake_pull)
    got, note = exp._resolve_actuals_dir("248526", "grok-voice-healthcare", None)
    assert note is None
    assert got == root / "grok-voice-healthcare" / "248526"
    assert len(calls) == 1

    # second resolve reuses the dumps on disk instead of re-pulling
    again, note2 = exp._resolve_actuals_dir("248526", "grok-voice-healthcare", None)
    assert (again, note2, len(calls)) == (got, None, 1)


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


def test_metadata_from_case_key() -> None:
    assert exp.metadata_from_case_key("C1-H4") == {
        "category": "C1",
        "difficulty": "hard",
        "audio_condition": "perfect",
    }
    assert exp.metadata_from_case_key("C1-E1-BG") == {
        "category": "C1",
        "difficulty": "easy",
        "audio_condition": "background_noise",
    }
    assert exp.metadata_from_case_key("R-M4") == {
        "category": "R",
        "difficulty": "medium",
        "audio_condition": "perfect",
    }
    assert exp.metadata_from_case_key("T1-H4") == {
        "category": "T1",
        "difficulty": "hard",
        "audio_condition": "perfect",
    }


def test_missing_task_still_fills_metadata_from_case_key() -> None:
    scored = _scored_result()
    scored["case_key"] = "C1-H4"
    scored["task"] = None
    scored["digital_human"] = {
        "name": "Marcus Bell",
        "test_name": "C1-H4: Hours, parking, UnitedHealthcare, then a Brooklyn Heights booking",
    }
    row = _row(scored)
    assert row["category"] == "C1"
    assert row["difficulty"] == "hard"
    assert row["audio_condition"] == "perfect"
    assert row["task_path"] == "industries/healthcare/tasks/C1-H4/task.json"


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


def test_csv_void_reason_ignores_empty_tool_pairing() -> None:
    completed = {
        "status": "COMPLETED",
        "trace_ids": ["abc"],
        "tool_calls": [{"name": "check_plan_accepted", "expected": [{}], "actual": []}],
    }
    assert exp.csv_void_reason(completed) == ""
    assert exp.csv_void_reason({"status": "NO_ANSWER"}) == "no conversation (NO_ANSWER)"
    assert exp.csv_void_reason({"status": "COMPLETED", "trace_ids": []}) == (
        "no trace linked — the harness never posted trace_ids"
    )
    row = {
        "detail": completed,
        "status": "COMPLETED",
        "pending": False,
        "task": {"exp_tool_calls": [{"name": "check_plan_accepted"}]},
        "passed": False,
    }
    exp.apply_csv_mark(row)
    assert row["mark"] == "FAIL"
    assert not row["void_reason"]


def test_headers_include_cost_columns() -> None:
    for name in (
        "cost_usd",
        "cost_model",
        "input_text_tokens",
        "input_audio_tokens",
        "output_text_tokens",
        "output_audio_tokens",
        "cached_tokens",
        "total_tokens",
    ):
        assert name in exp.HEADERS
    row = _row()
    assert row["cost_usd"] == ""
    assert row["cost_model"] == ""


def test_parse_run_spec_plus_and_comma() -> None:
    assert exp.parse_run_spec("247475+247634") == ("247475", ["247634"])
    assert exp.parse_run_spec("10,20,30") == ("10", ["20", "30"])
    assert exp.parse_run_spec("99") == ("99", [])


def test_fill_connection_holes_keeps_void_and_replaces_no_answer() -> None:
    primary = [
        {
            "result_id": "1",
            "case_key": "C1-E1",
            "digital_human_id": 10,
            "status": "COMPLETED",
            "void_reason": "tool list empty",
        },
        {
            "result_id": "2",
            "case_key": "C1-E1",
            "digital_human_id": 11,
            "status": "NO_ANSWER",
        },
    ]
    retries = [[
        {
            "result_id": "8",
            "case_key": "C1-E1",
            "digital_human_id": 10,
            "status": "COMPLETED",
        },
        {
            "result_id": "9",
            "case_key": "C1-E1",
            "digital_human_id": 11,
            "status": "COMPLETED",
            "void_reason": "tool list empty",
        },
    ]]
    out = exp.fill_connection_holes(primary, retries)
    assert [row["result_id"] for row in out] == ["1", "9"]


def test_audio_eval_overrides_bluejay_latency() -> None:
    package = {
        "agent_latency_stats": {"avg_ms": 2185, "p50_ms": 4780, "p90_ms": 4939, "max_ms": 4939},
        "customer_latency_stats": {"avg_ms": 4075, "p50_ms": 4140, "p90_ms": 5100, "max_ms": 5100},
        "interruptions": {"agent_interruption_count": 1, "customer_interruption_count": 0},
        "transcript": [{"speaker": "AGENT", "start": 1.6, "text": "hello"}],
    }
    row = _row(audio_eval=package)
    assert row["builtin_avg_agent_latency"] == "2185"
    assert row["builtin_p50_agent_latency"] == "4780"
    assert row["builtin_p90_agent_latency"] == "4939"
    assert row["builtin_max_agent_latency"] == "4939"
    assert row["builtin_p95_agent_latency"] == ""
    assert row["builtin_p99_agent_latency"] == ""
    assert row["builtin_avg_punctuation_latency"] == ""
    assert row["eval_avg_agent_latency"] == "2185"
    assert row["builtin_avg_customer_latency"] == "4075"
    assert row["builtin_agent_interruption_count"] == "1"
    assert row["builtin_customer_interruption_count"] == "0"
    assert row["builtin_time_to_first_agent_utterance"] == "1600"
    assert row["builtin_num_turns"] == "5"


def test_normalize_model_id_matches_pricing_keys() -> None:
    pricing = {"token_pricing": {
        "gemini-2.5-flash-native-audio": {},
        "gemini-3.1-flash-live-preview": {},
    }}
    assert exp.normalize_model_id(
        "models/gemini-2.5-flash-native-audio-preview-09-2025", pricing
    ) == "gemini-2.5-flash-native-audio"
    assert exp.normalize_model_id(
        "gemini-3.1-flash-live-preview-12-2025", pricing
    ) == "gemini-3.1-flash-live-preview"


def test_usage_from_trace_sums_every_response() -> None:
    exp._PRICING_CACHE = {
        "token_pricing": {
            "gemini-2.5-flash-native-audio": {
                "inputText": 0.5,
                "inputAudio": 3.0,
                "cachedText": None,
                "outputText": 2.0,
                "outputAudio": 12.0,
            }
        }
    }
    try:
        body = {
            "data": {"data": {"results": [{"rows": [
                {"data": {"timestamp": "1", "attributes": {
                    "gen_ai.request.model": "gemini-2.5-flash-native-audio",
                    "gen_ai.usage.input_tokens": 100,
                    "gen_ai.usage.input_text_tokens": 40,
                    "gen_ai.usage.input_audio_tokens": 60,
                    "gen_ai.usage.output_tokens": 10,
                    "gen_ai.usage.output_text_tokens": 4,
                    "gen_ai.usage.output_audio_tokens": 6,
                }}},
                {"data": {"timestamp": "2", "attributes": {
                    "gen_ai.request.model": "gemini-2.5-flash-native-audio",
                    "gen_ai.usage.input_tokens": 150,
                    "gen_ai.usage.input_text_tokens": 50,
                    "gen_ai.usage.input_audio_tokens": 100,
                    "gen_ai.usage.output_tokens": 12,
                    "gen_ai.usage.output_text_tokens": 5,
                    "gen_ai.usage.output_audio_tokens": 7,
                }}},
            ]}]}},
        }
        # Each response re-sends and is billed for the whole conversation, so
        # growing per-response input is context growth, not a running counter.
        usage = exp.usage_from_trace_body(body)
        assert usage["input_text"] == 90
        assert usage["input_audio"] == 160
        assert usage["output_text"] == 9
        assert usage["output_audio"] == 13
        assert usage["norm"] == "gemini-2.5-flash-native-audio"
        assert usage["cost_usd"] == round(
            (90 * 0.5 + 160 * 3.0 + 9 * 2.0 + 13 * 12.0) / 1_000_000, 6
        )
        row = _row(cost=usage)
        assert row["cost_model"] == "gemini-2.5-flash-native-audio"
        assert row["input_text_tokens"] == "90"
        assert row["total_tokens"] == "272"
        assert row["cost_usd"] == row["cost_llm_usd"]
    finally:
        exp._PRICING_CACHE = None


def test_per_minute_pricing_from_root_audio_duration() -> None:
    """Grok: no tokens, minutes on `realtime_session` → USD from per_minute_pricing."""
    exp._PRICING_CACHE = {
        "token_pricing": {},
        "per_minute_pricing": {"grok-voice-latest": 0.08},
    }
    try:
        body = {"data": {"data": {"results": [{"rows": [
            {"data": {"timestamp": "1", "attributes": {
                "gen_ai.request.model": "grok-voice-latest",
                "gen_ai.usage.input_tokens": "0",
                "gen_ai.usage.output_tokens": "0",
                "mivas.audio.duration_s": "53.028",
                "mivas.audio.duration_minutes": "0.883797",
            }}},
        ]}]}}}
        usage = exp.usage_from_trace_body(body)
        assert usage["total"] == 0
        assert usage["audio_duration_s"] == 53.028
        assert usage["cost_usd"] == round(0.883797 * 0.08, 6)
        row = _row(cost=usage)
        assert row["audio_duration_minutes"] == "0.883797"
        assert row["cost_usd"] == "0.070704"
    finally:
        exp._PRICING_CACHE = None


def test_cascaded_text_lanes_and_stt_tts_durations() -> None:
    """livekit/cascaded stamps `_text`-suffixed lanes + stt/tts seconds on `voice.call`."""
    exp._PRICING_CACHE = {
        "token_pricing": {"gpt-4.1": {
            "inputText": 2.0, "inputAudio": None,
            "cachedText": 0.5, "cachedAudio": None,
            "outputText": 8.0, "outputAudio": None,
        }},
        "per_minute_pricing": {},
        "component_pricing": {
            "stt": {"usd_per_minute": 0.0077},
            "tts": {"usd_per_1k_characters": 0.0825},
        },
    }
    try:
        body = {"data": {"data": {"results": [{"rows": [
            {"data": {"timestamp": "1", "attributes": {
                "gen_ai.request.model": "gpt-4.1",
                "gen_ai.usage.input_tokens": "31847",
                "gen_ai.usage.input_tokens_text": "31847",
                "gen_ai.usage.output_tokens": "198",
                "gen_ai.usage.output_tokens_text": "198",
                "gen_ai.usage.cached_tokens": "21632",
                "mivas.audio.duration_s": "84.173",
                "mivas.stt.audio_duration_s": "80.35",
                "mivas.tts.audio_duration_s": "36.833",
                "mivas.tts.characters": "669",
            }}},
        ]}]}}}
        usage = exp.usage_from_trace_body(body)
        assert usage["input_text"] == 31847
        assert usage["output_text"] == 198 and usage["output_audio"] == 0
        # cached REPLACES part of the input lane; it is not billed on top.
        assert usage["cost_llm_usd"] == round(
            ((31847 - 21632) * 2.0 + 21632 * 0.5 + 198 * 8.0) / 1_000_000, 6
        )
        assert usage["cost_stt_usd"] == round(80.35 / 60 * 0.0077, 6)
        assert usage["cost_tts_usd"] == round(669 / 1000 * 0.0825, 6)
        parts = usage["cost_llm_usd"] + usage["cost_stt_usd"] + usage["cost_tts_usd"]
        assert abs(usage["cost_usd"] - parts) < 1e-5
        row = _row(cost=usage)
        assert row["stt_audio_duration_s"] == "80.35"
        assert row["tts_audio_duration_s"] == "36.833"
        assert row["tts_characters"] == "669"
    finally:
        exp._PRICING_CACHE = None


def test_session_root_usage_is_not_double_counted() -> None:
    """openai/qwen/aws stamp usage on the root AND its per-response children."""
    exp._PRICING_CACHE = {
        "token_pricing": {"gpt-realtime-2.1-mini": {
            "inputText": 0.6, "inputAudio": 10.0,
            "cachedText": 0.06, "cachedAudio": 0.3,
            "outputText": 2.4, "outputAudio": 20.0,
        }},
        "per_minute_pricing": {},
    }

    def span(span_id, parent, ts, attrs):
        return {"data": {"span_id": span_id, "parent_span_id": parent,
                         "timestamp": ts, "attributes": {
                             "gen_ai.request.model": "gpt-realtime-2.1-mini", **attrs}}}

    try:
        body = {"data": {"data": {"results": [{"rows": [
            span("root", "", "1", {"gen_ai.usage.input_tokens": 300,
                                   "gen_ai.usage.output_tokens": 30}),
            span("a", "root", "2", {"gen_ai.usage.input_tokens": 100,
                                    "gen_ai.usage.input_text_tokens": 100,
                                    "gen_ai.usage.output_tokens": 10,
                                    "gen_ai.usage.output_audio_tokens": 10}),
            span("b", "root", "3", {"gen_ai.usage.input_tokens": 200,
                                    "gen_ai.usage.input_text_tokens": 200,
                                    "gen_ai.usage.output_tokens": 20,
                                    "gen_ai.usage.output_audio_tokens": 20}),
        ]}]}}}
        usage = exp.usage_from_trace_body(body)
        assert usage["input_text"] == 300
        assert usage["output_audio"] == 30
    finally:
        exp._PRICING_CACHE = None


def test_pricing_file_has_component_pricing_for_the_cascade() -> None:
    pricing = json.loads((ROOT / "voice-agent-harnesses" / "s2s-model-pricing.json").read_text())
    stt = pricing["component_pricing"]["stt"]
    tts = pricing["component_pricing"]["tts"]
    assert (stt["model"], stt["usd_per_minute"]) == ("flux-general-en", 0.0077)
    assert (tts["model"], tts["usd_per_1k_characters"]) == ("eleven_flash_v2_5", 0.0825)


def test_pricing_file_has_gpt_41_row() -> None:
    pricing = json.loads((ROOT / "voice-agent-harnesses" / "s2s-model-pricing.json").read_text())
    row = pricing["token_pricing"]["gpt-4.1"]
    assert (row["inputText"], row["cachedText"], row["outputText"]) == (2.00, 0.50, 8.00)
    assert row["inputAudio"] is None and row["outputAudio"] is None


def test_overlay_tool_actuals_prefers_postgres_when_present() -> None:
    detail = {"tool_calls": []}
    pg = {"tool_calls": [{"name": "check_plan_accepted", "actual": [{"parameters": {"carrier": "Aetna"}}]}]}
    out = exp.overlay_tool_actuals(detail, pg)
    assert out["tool_calls"][0]["name"] == "check_plan_accepted"
    listing = {"tool_calls": [{"name": "end_call", "actual": [{}]}]}
    empty_pg = {"tool_calls": [{"name": "end_call", "actual": []}]}
    kept = exp.overlay_tool_actuals(listing, empty_pg)
    assert kept["tool_calls"][0]["name"] == "end_call"
