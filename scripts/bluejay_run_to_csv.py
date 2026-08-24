"""Write one Bluejay simulation run as a conversation-per-row CSV.

Each CSV row is one Bluejay simulation result (`result_id`) — one
conversation. k repeats of the same digital human stay as k rows; they
are never rolled up. `conversation_index` is 1..k in run order for the
same `digital_human_id` + `case_key` so dashboards can still do
always/mixed/never bands. It is not a rollup of those conversations.

This CSV is the dashboard source of record for a full simulation — task
link, tool/handoff/hangup evals (args + DB diffs), custom-metric values
and reasoning, builtin quality metrics, transcript, and LLM cost
(conversation + per-utterance) — so a later dashboard can visualize the
run without going back to Bluejay.

Scoring is our verifier (tools ∧ handoff ∧ hangup DB), not Bluejay's goal
judge. `goal_success`, `goal_reasoning`, and platform `tests_passed` are
never columns.

    uv run python scripts/bluejay_run_to_csv.py RUN_ID --industry healthcare --out /tmp/run-RUN_ID.csv
    uv run python scripts/bluejay_run_to_csv.py --sim SIM_ID --industry healthcare --out /tmp/run.csv
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, TextIO

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verify_task_run = _load("verify_task_run", SCRIPTS / "verify_task_run.py")
verify_run = verify_task_run.verify_run
eval_costs = _load("eval_costs", SCRIPTS / "eval_costs.py")

# stable header. extra custom metrics (if any) append after these.
HEADERS = [
    # run / conversation identity
    "run_id",
    "simulation_id",
    "agent_id",
    "result_id",
    "digital_human_id",
    "industry",
    "case_key",
    "task_path",
    "task_dir",
    "task_name",
    "dh_name",
    "category",
    "difficulty",
    "audio_condition",
    "conversation_index",
    "status",
    "mark",
    "pending",
    "void_reason",
    "duration_s",
    "llm_cost_usd",
    "llm_cost_source",
    "llm_cost_per_hour_usd",
    "utterance_costs_json",
    "start_time",
    "end_time",
    # combined + component pass (ours, not Bluejay goal)
    "combined_pass",
    "tools_pass",
    "handoff_pass",
    "hangup_db_pass",
    # tool-call eval (names + args)
    "tools_score",
    "missing_tools",
    "hit_tools",
    "extra_tools",
    "actual_tools",
    "expected_tools",
    "expected_tool_calls_json",
    "actual_tool_calls_json",
    # handoff eval
    "handoff_verdict",
    "handoff_score",
    "expected_handoffs",
    "actual_handoffs",
    # hangup DB eval
    "hangup_expected_json",
    "hangup_actual_json",
    "hangup_diff",
    "hangup_note",
    # Bluejay custom metrics (never goal_*)
    "prompt_adherence",
    "prompt_adherence_reasoning",
    "task_completion",
    "task_completion_reasoning",
    "premature_end",
    "premature_end_reasoning",
    # builtin quality / latency / barge-in
    "builtin_duration",
    "builtin_num_turns",
    "builtin_interface",
    "builtin_agent_interruption_count",
    "builtin_customer_interruption_count",
    "builtin_avg_agent_latency",
    "builtin_max_agent_latency",
    "builtin_p50_agent_latency",
    "builtin_p90_agent_latency",
    "builtin_p95_agent_latency",
    "builtin_p99_agent_latency",
    "builtin_avg_customer_latency",
    "builtin_p50_customer_latency",
    "builtin_p90_customer_latency",
    "builtin_avg_punctuation_latency",
    "builtin_p50_punctuation_latency",
    "builtin_p90_punctuation_latency",
    "builtin_time_to_first_agent_utterance",
    "builtin_agent_audio_clarity",
    "builtin_agent_perceived_loudness",
    "builtin_agent_audio_clipping",
    "builtin_agent_pitch_variability",
    "builtin_agent_audio_dropouts",
    "builtin_pronunciation",
    "builtin_agent_turn_duration_avg",
    "builtin_agent_words_per_turn_avg",
    "builtin_customer_words_per_turn_avg",
    "builtin_agent_wpm",
    "builtin_agent_speak_percentage",
    "builtin_success",
    "builtin_redundancy",
    # evaluation quality (never goal_success / goal_reasoning)
    "eval_sentiment_label",
    "eval_sentiment_score",
    "eval_hallucination",
    "eval_redundancy",
    "eval_pronunciation_score",
    "eval_agent_audio_clarity",
    "eval_user_audio_clarity",
    "eval_agent_speak_percentage",
    "eval_avg_agent_latency",
    "eval_call_summary",
    # transcript / audio
    "transcript",
    "transcript_url",
    "recording_url",
    "agent_chars",
    "num_turns",
]

# Bluejay native outcome / goal eval — never emit these, even as extras.
EXCLUDED_COLUMNS = frozenset({
    "goal_success",
    "goal_reasoning",
    "tests_passed",
    "judge_goal",
    "judge_reason",
})

CANONICAL_METRICS = ("prompt_adherence", "task_completion", "premature_end")
CANONICAL_REASONING = tuple(f"{key}_reasoning" for key in CANONICAL_METRICS)
BUILTIN_METRICS = (
    "duration",
    "num_turns",
    "interface",
    "agent_interruption_count",
    "customer_interruption_count",
    "avg_agent_latency",
    "max_agent_latency",
    "p50_agent_latency",
    "p90_agent_latency",
    "p95_agent_latency",
    "p99_agent_latency",
    "avg_customer_latency",
    "p50_customer_latency",
    "p90_customer_latency",
    "avg_punctuation_latency",
    "p50_punctuation_latency",
    "p90_punctuation_latency",
    "time_to_first_agent_utterance",
    "agent_audio_clarity",
    "agent_perceived_loudness",
    "agent_audio_clipping",
    "agent_pitch_variability",
    "agent_audio_dropouts",
    "pronunciation",
    "agent_turn_duration_avg",
    "agent_words_per_turn_avg",
    "customer_words_per_turn_avg",
    "agent_wpm",
    "agent_speak_percentage",
    "success",
    "redundancy",
)
EVAL_QUALITY_KEYS = (
    "sentiment_label",
    "sentiment_score",
    "hallucination",
    "redundancy",
    "pronunciation_score",
    "agent_audio_clarity",
    "user_audio_clarity",
    "agent_speak_percentage",
    "avg_agent_latency",
    "call_summary",
)
RECORDING_KEYS = (
    "recording_url",
    "audio_url",
    "call_recording_url",
    "recording",
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug_metric(name: str) -> str:
    return _SLUG_RE.sub("_", str(name or "").strip().lower()).strip("_")


def canonical_metric_key(name: str) -> str | None:
    slug = slug_metric(name)
    if "prompt" in slug and "adherence" in slug:
        return "prompt_adherence"
    if "task" in slug and "completion" in slug:
        return "task_completion"
    if "premature" in slug:
        return "premature_end"
    return None


def _bool_cell(value: Any) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def _join(names: Iterable[Any]) -> str:
    return ";".join(str(item) for item in names if item not in (None, ""))


def _json_cell(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def _eval0(result: dict[str, Any]) -> dict[str, Any]:
    evals = result.get("evaluations") or []
    if isinstance(evals, list) and evals and isinstance(evals[0], dict):
        return evals[0]
    if isinstance(evals, dict):
        return evals
    return {}


def _metric_raw(entry: dict[str, Any]) -> Any:
    if entry.get("is_not_applicable"):
        return None
    val = entry.get("response_value")
    if val is None:
        for key in (
            "yes_no_response",
            "quantitative_response",
            "pass_fail_response",
            "enum_response",
            "qualitative_response",
            "json_response",
            "int_value",
            "float_value",
            "boolean_value",
            "enum_value",
            "qualitative_value",
            "json_value",
            "value",
        ):
            if entry.get(key) is not None:
                val = entry[key]
                break
    return val


def _metric_cell(entry: dict[str, Any]) -> str:
    val = _metric_raw(entry)
    if val is None or val == "":
        return ""
    kind = str(entry.get("response_type") or "").lower()
    if kind in {"yes_no", "pass_fail"} or (
        isinstance(val, str) and val.strip().lower() in {"yes", "no", "true", "false"}
    ):
        low = str(val).strip().lower()
        if low in {"yes", "true"}:
            return "true"
        if low in {"no", "false"}:
            return "false"
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val).strip()


def _metric_reasoning(entry: dict[str, Any]) -> str:
    return str(entry.get("reasoning") or "").replace("\n", " ").strip()


def iter_custom_metric_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer top-level `custom_metrics`; fall back to evaluation mirrors."""
    top = result.get("custom_metrics")
    if isinstance(top, list) and top:
        return [item for item in top if isinstance(item, dict)]
    ev = _eval0(result)
    nested = ev.get("custom_metrics") or ev.get("custom_evals")
    if isinstance(nested, dict):
        results = nested.get("custom_metrics_results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return [
            dict(value, name=key) if isinstance(value, dict) else {"name": key, "value": value}
            for key, value in nested.items()
        ]
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    return []


def _excluded_metric(key: str) -> bool:
    if key in EXCLUDED_COLUMNS:
        return True
    if key.endswith("_reasoning") and key[: -len("_reasoning")] in EXCLUDED_COLUMNS:
        return True
    return False


def custom_metric_cells(result: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {key: "" for key in CANONICAL_METRICS}
    for key in CANONICAL_METRICS:
        out[f"{key}_reasoning"] = ""
    for entry in iter_custom_metric_entries(result):
        name = str(entry.get("metric_name") or entry.get("name") or "").strip()
        if not name:
            continue
        key = canonical_metric_key(name) or slug_metric(name)
        if not key or _excluded_metric(key):
            continue
        if key in out and out[key]:
            continue
        out[key] = _metric_cell(entry)
        out[f"{key}_reasoning"] = _metric_reasoning(entry)
    return out


def builtin_metric(result: dict[str, Any], name: str) -> Any:
    for item in result.get("metrics") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item.get("value")
    return None


def builtin_metric_cells(result: dict[str, Any]) -> dict[str, str]:
    out = {f"builtin_{name}": "" for name in BUILTIN_METRICS}
    for item in result.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name not in BUILTIN_METRICS:
            continue
        val = item.get("value")
        out[f"builtin_{name}"] = "" if val is None else str(val)
    return out


def eval_quality_cells(result: dict[str, Any]) -> dict[str, str]:
    ev = _eval0(result)
    out: dict[str, str] = {}
    for key in EVAL_QUALITY_KEYS:
        col = f"eval_{key}"
        if _excluded_metric(col) or key in EXCLUDED_COLUMNS:
            continue
        val = ev.get(key)
        if key == "call_summary" and val not in (None, ""):
            out[col] = str(val).replace("\n", " ").strip()
        else:
            out[col] = "" if val is None else str(val)
    return out


def recording_url_of(result: dict[str, Any]) -> str:
    for key in RECORDING_KEYS:
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def duration_s(result: dict[str, Any]) -> str:
    raw = result.get("duration")
    if raw not in (None, ""):
        return str(raw)
    ms = builtin_metric(result, "duration")
    if isinstance(ms, (int, float)):
        return str(ms / 1000 if ms > 1000 else ms)
    return ""


def num_turns(result: dict[str, Any]) -> str:
    turns = builtin_metric(result, "num_turns")
    if turns is None:
        turns = _eval0(result).get("num_turns")
    return "" if turns is None else str(turns)


def agent_id_of(
    run: dict[str, Any],
    results: list[dict[str, Any]],
    sim_id: str,
) -> str:
    for key in ("agent_id",):
        if run.get(key) not in (None, ""):
            return str(run[key])
    if sim_id:
        try:
            body = verify_run._get(f"simulation/{sim_id}")
        except SystemExit:
            body = {}
        sim = body.get("simulation") or body
        if isinstance(sim, dict) and sim.get("agent_id") not in (None, ""):
            return str(sim["agent_id"])
    for row in results:
        detail = row.get("detail") or {}
        for entry in iter_custom_metric_entries(detail):
            if entry.get("agent_id") not in (None, ""):
                return str(entry["agent_id"])
    return ""


def extra_tool_names(expected: list[str], actual: list[str]) -> list[str]:
    want = set(expected)
    seen: set[str] = set()
    extra: list[str] = []
    for name in actual:
        if name in want or name in seen:
            continue
        seen.add(name)
        extra.append(name)
    return extra


def transcript_text(result: dict[str, Any], lines: list[str] | None = None) -> str:
    if lines is None:
        lines = verify_task_run.result_transcript_lines(result)
    return "\n".join(line.rstrip("\n") for line in lines)


def task_link(industry: str, case_key: str, task: Any) -> tuple[str, str]:
    """Repo-relative task.json path. Emit if the file exists or was already loaded."""
    if not industry or not case_key:
        return "", ""
    rel_dir = f"industries/{industry}/tasks/{case_key}"
    rel_file = f"{rel_dir}/task.json"
    if task is not None or (ROOT / rel_file).is_file():
        return rel_file, rel_dir
    return "", ""


def task_metadata(task: dict[str, Any] | None) -> dict[str, str]:
    meta = (task or {}).get("metadata") if isinstance(task, dict) else None
    if not isinstance(meta, dict):
        meta = {}
    return {
        "category": "" if meta.get("category") in (None, "") else str(meta["category"]),
        "difficulty": "" if meta.get("difficulty") in (None, "") else str(meta["difficulty"]),
        "audio_condition": (
            "" if meta.get("audio_condition") in (None, "") else str(meta["audio_condition"])
        ),
    }


def assign_conversation_indexes(results: list[dict[str, Any]]) -> None:
    """1..k in run order for the same digital_human_id + case_key.

    Each result stays its own row. This index is only so dashboards can
    band always/mixed/never across k conversations; it is not a rollup.
    """
    counts: dict[tuple[str, str], int] = {}
    for row in results:
        key = (str(row.get("digital_human_id") or ""), str(row.get("case_key") or ""))
        counts[key] = counts.get(key, 0) + 1
        row["conversation_index"] = counts[key]


def _row_fingerprint(row: Any) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def hangup_table_diff(
    expected: Any,
    actual: Any,
    industry: str | None,
) -> dict[str, Any]:
    """Canonical table-level mismatch (same normalization the scorer uses)."""
    if expected is None or actual is None:
        return {}
    exp = verify_task_run.office_canonical(expected, industry)
    act = verify_task_run.office_canonical(actual, industry)
    out: dict[str, Any] = {}
    tables = list(dict.fromkeys([*exp.keys(), *act.keys()]))
    for table in tables:
        want = exp.get(table) or []
        got = act.get(table) or []
        if want == got:
            continue
        exp_fps = {_row_fingerprint(row) for row in want}
        act_fps = {_row_fingerprint(row) for row in got}
        out[table] = {
            "expected_count": len(want),
            "actual_count": len(got),
            "only_in_expected": [row for row in want if _row_fingerprint(row) not in act_fps],
            "only_in_actual": [row for row in got if _row_fingerprint(row) not in exp_fps],
        }
    return out


def hangup_diff_cell(
    expected: Any,
    actual: Any,
    state: dict[str, Any],
    industry: str | None,
) -> str:
    if state.get("skipped") or state.get("passed") is not False:
        return ""
    diff = hangup_table_diff(expected, actual, industry)
    return _json_cell(diff) if diff else ""


def result_row(
    scored: dict[str, Any],
    *,
    run_id: str,
    simulation_id: str,
    agent_id: str,
    industry: str = "",
    conversation_index: int | str | None = None,
    transcript_lines: list[str] | None = None,
    harness: str = "openai/realtime-2.1",
    fetch_costs: bool = False,
    cost_spans: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """One CSV row for one Bluejay simulation result / conversation."""
    detail = scored.get("detail") or {}
    task = scored.get("task")
    call = scored.get("call") or {}
    handoff = scored.get("handoff") or {}
    state = scored.get("state") or {}
    expected_tools = verify_task_run.expected_tool_names(task)
    actual_tools = list(scored.get("actual_tools") or [])
    void = bool(scored.get("void_reason") or scored.get("pending"))
    hangup = state.get("passed")
    hangup_cell = "" if state.get("skipped") or hangup is None else _bool_cell(hangup)
    combined = "" if void else _bool_cell(scored.get("passed"))
    metrics = custom_metric_cells(detail)
    builtins = builtin_metric_cells(detail)
    evals = eval_quality_cells(detail)
    meta = task_metadata(task if isinstance(task, dict) else None)
    path, directory = task_link(industry, str(scored.get("case_key") or ""), task)
    dh = scored.get("digital_human") or {}
    task_name = ""
    if isinstance(task, dict) and task.get("task_name"):
        task_name = str(task["task_name"])
    else:
        task_name = str(dh.get("test_name") or "")
    if conversation_index is None:
        conversation_index = scored.get("conversation_index")
    if transcript_lines is None:
        transcript_lines = scored.get("transcript_lines")
    expected_calls = list((task or {}).get("exp_tool_calls") or []) if isinstance(task, dict) else []
    actual_calls = scored.get("actual_tool_calls")
    if actual_calls is None:
        actual_calls = verify_task_run.actual_tool_calls(detail)
    expected_state = verify_task_run.expected_state(task) if isinstance(task, dict) else None
    actual_state = scored.get("actual_state")
    row = {
        "run_id": str(run_id),
        "simulation_id": str(simulation_id or ""),
        "agent_id": str(agent_id or ""),
        "result_id": str(scored.get("result_id") or detail.get("id") or ""),
        "digital_human_id": "" if scored.get("digital_human_id") is None else str(scored["digital_human_id"]),
        "industry": industry,
        "case_key": scored.get("case_key") or "",
        "task_path": path,
        "task_dir": directory,
        "task_name": task_name,
        "dh_name": "" if dh.get("name") in (None, "") else str(dh["name"]),
        "category": meta["category"],
        "difficulty": meta["difficulty"],
        "audio_condition": meta["audio_condition"],
        "conversation_index": "" if conversation_index in (None, "") else str(conversation_index),
        "status": scored.get("status") or "",
        "mark": scored.get("mark") or "",
        "pending": _bool_cell(scored.get("pending")),
        "void_reason": scored.get("void_reason") or "",
        "duration_s": duration_s(detail),
        "llm_cost_usd": "",
        "llm_cost_source": "",
        "llm_cost_per_hour_usd": "",
        "utterance_costs_json": "",
        "start_time": str(detail.get("start_time") or ""),
        "end_time": str(detail.get("end_time") or ""),
        "combined_pass": combined,
        "tools_pass": _bool_cell(call.get("passed")),
        "handoff_pass": _bool_cell(handoff.get("passed")),
        "hangup_db_pass": hangup_cell,
        "tools_score": "" if call.get("score") is None else str(call["score"]),
        "missing_tools": _join(call.get("missing") or []),
        "hit_tools": _join(call.get("hit") or []),
        "extra_tools": _join(extra_tool_names(expected_tools, actual_tools)),
        "actual_tools": _join(actual_tools),
        "expected_tools": _join(expected_tools),
        "expected_tool_calls_json": _json_cell(expected_calls) if expected_calls or isinstance(task, dict) else "",
        "actual_tool_calls_json": _json_cell(actual_calls),
        "handoff_verdict": handoff.get("verdict") or "",
        "handoff_score": "" if handoff.get("score") is None else str(handoff["score"]),
        "expected_handoffs": _join(handoff.get("expected") or []),
        "actual_handoffs": _join(handoff.get("actual") or []),
        "hangup_expected_json": _json_cell(expected_state),
        "hangup_actual_json": _json_cell(actual_state),
        "hangup_diff": hangup_diff_cell(expected_state, actual_state, state, industry or None),
        "hangup_note": "" if state.get("note") in (None, "") else str(state["note"]),
        "prompt_adherence": metrics.get("prompt_adherence", ""),
        "prompt_adherence_reasoning": metrics.get("prompt_adherence_reasoning", ""),
        "task_completion": metrics.get("task_completion", ""),
        "task_completion_reasoning": metrics.get("task_completion_reasoning", ""),
        "premature_end": metrics.get("premature_end", ""),
        "premature_end_reasoning": metrics.get("premature_end_reasoning", ""),
        **builtins,
        **evals,
        "transcript": transcript_text(detail, transcript_lines),
        "transcript_url": str(detail.get("transcript_url") or ""),
        "recording_url": recording_url_of(detail),
        "agent_chars": "" if scored.get("agent_chars") is None else str(scored["agent_chars"]),
        "num_turns": num_turns(detail),
    }
    hinted_ids = detail.get("trace_ids") or scored.get("trace_ids") or []
    row.update(
        eval_costs.cost_conversation(
            row,
            harness,
            spans=cost_spans,
            fetch=fetch_costs,
            transcript_lines=transcript_lines,
            trace_ids=[str(item) for item in hinted_ids if item],
        )
    )
    skip_metric_keys = set(CANONICAL_METRICS) | set(CANONICAL_REASONING) | EXCLUDED_COLUMNS
    for key, value in metrics.items():
        if key in skip_metric_keys or _excluded_metric(key) or key in row:
            continue
        row[key] = value
    return {key: "" if value is None else str(value) for key, value in row.items()}


def headers_for(rows: list[dict[str, str]]) -> list[str]:
    extra: list[str] = []
    for row in rows:
        for key in row:
            if key in HEADERS or key in EXCLUDED_COLUMNS or _excluded_metric(key) or key in extra:
                continue
            extra.append(key)
    return list(HEADERS) + extra


def write_csv(rows: list[dict[str, str]], dest: TextIO) -> list[str]:
    fields = headers_for(rows)
    writer = csv.DictWriter(
        dest,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return fields


def rows_to_csv(rows: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    write_csv(rows, buf)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    verify_task_run.load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", help="Bluejay simulation run id")
    parser.add_argument("--sim", help="Use this simulation's latest run")
    parser.add_argument("--industry", required=True)
    parser.add_argument("--out", help="UTF-8 CSV path (default: run_<id>.csv)")
    parser.add_argument("--actuals-dir", type=Path, help="Local hangup dumps (skip S3)")
    parser.add_argument("--harness", default="openai/realtime-2.1")
    parser.add_argument("--slug", help="Override the S3 / dump slug")
    args = parser.parse_args(argv)

    run_id = args.run_id
    if args.sim:
        run_id = verify_run.latest_run_for_sim(args.sim)
    if not run_id:
        parser.error("give a run id or --sim")

    scored = verify_task_run.collect_scored_results(
        str(run_id),
        args.industry,
        actuals_dir=args.actuals_dir,
        harness=args.harness,
        slug=args.slug,
        sim_hint=args.sim,
    )
    agent_id = agent_id_of(scored.get("run") or {}, scored["results"], scored.get("simulation_id") or "")
    assign_conversation_indexes(scored["results"])
    rows = [
        result_row(
            row,
            run_id=scored["run_id"],
            simulation_id=scored.get("simulation_id") or "",
            agent_id=agent_id,
            industry=args.industry,
            conversation_index=row.get("conversation_index"),
            transcript_lines=row.get("transcript_lines"),
            harness=args.harness,
            fetch_costs=True,
        )
        for row in scored["results"]
    ]
    if not rows:
        raise SystemExit(f"no results for run {run_id}")

    out = Path(args.out) if args.out else Path(f"run_{run_id}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        fields = write_csv(rows, handle)
    print(f"wrote {out} ({len(rows)} rows × {len(fields)} columns)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
