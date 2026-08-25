"""Write one or more Bluejay simulation runs as a conversation-per-row CSV.

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

Primary + fill-in: `PRIMARY+RETRY` (or comma) replaces unanswered slots
from later runs, matched by digital_human_id then case_key. Connected
VOID rows stay; they are not swapped for a retry.

Latency columns come from `verify-out/audio_eval/{result_id}.json` when
that file exists (agent/customer stats, interruptions, time-to-first
agent utterance). Bluejay `metrics[]` latency is not used in that case.

LLM cost comes from Bluejay traces (`gen_ai.usage.*`) priced with
`voice-agent-harnesses/s2s-model-pricing.json`.

Scoring is our verifier (tools ∧ handoff ∧ hangup DB), not Bluejay's goal
judge. `goal_success`, `goal_reasoning`, and platform `tests_passed` are
never columns.

    uv run python scripts/bluejay_run_to_csv.py RUN --industry healthcare --harness gemini/2.5-flash-native-audio
    uv run python scripts/bluejay_run_to_csv.py PRIMARY+RETRY --industry healthcare --harness gemini/flash-live-3.1
    uv run python scripts/bluejay_run_to_csv.py --sim SIM --industry legal --harness openai/realtime-2.1
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, TextIO

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
VERIFIERS = ROOT / "verifiers"
DEFAULT_AUDIO_EVAL = ROOT / "verify-out" / "audio_eval"
DEFAULT_PRICING = ROOT / "voice-agent-harnesses" / "s2s-model-pricing.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verify_task_run = _load("verify_task_run", VERIFIERS / "verify_task_run.py")
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
    # LLM cost from traces + s2s-model-pricing.json (cost_usd = llm + stt + tts)
    "cost_usd",
    "cost_llm_usd",
    "cost_stt_usd",
    "cost_tts_usd",
    "cost_model",
    "input_text_tokens",
    "input_audio_tokens",
    "output_text_tokens",
    "output_audio_tokens",
    "cached_tokens",
    "total_tokens",
    # audio duration from the harness root span (grok minutes, cascaded stt/tts)
    "audio_duration_s",
    "audio_duration_minutes",
    "stt_audio_duration_s",
    "tts_audio_duration_s",
    "tts_characters",
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
CONNECTION_HOLES = frozenset({
    "NO_ANSWER", "DISPATCHED", "SYSTEM_ERROR", "ERROR",
    "NO_CONNECTION", "CANCELLED", "CONVERSATION_ENDED",
})
# Bluejay metrics[] latency — wiped when an audio_eval package is present.
AUDIO_EVAL_CLEARS = (
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
    "builtin_agent_interruption_count",
    "builtin_customer_interruption_count",
    "eval_avg_agent_latency",
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PRICING_CACHE: dict[str, Any] | None = None


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


_DIFFICULTY = {"E": "easy", "M": "medium", "H": "hard"}
_AUDIO_SUFFIXES = (("-BG", "background_noise"), ("-SIG", "bad_signal"))


def metadata_from_case_key(case_key: str) -> dict[str, str]:
    """C1-H4 / C1-E1-BG / R-M4 / T1-H4 → category, difficulty, audio_condition."""
    raw = str(case_key or "").strip()
    empty = {"category": "", "difficulty": "", "audio_condition": ""}
    if not raw:
        return empty
    audio = "perfect"
    key = raw
    for suffix, condition in _AUDIO_SUFFIXES:
        if key.endswith(suffix):
            audio = condition
            key = key[: -len(suffix)]
            break
    parts = key.split("-")
    if len(parts) < 2 or not parts[1]:
        return empty
    difficulty = _DIFFICULTY.get(parts[1][0], "")
    if not difficulty:
        return empty
    return {
        "category": parts[0],
        "difficulty": difficulty,
        "audio_condition": audio,
    }


def task_metadata(task: dict[str, Any] | None, case_key: str = "") -> dict[str, str]:
    """Prefer task.json metadata; fill blanks from the case key."""
    meta = (task or {}).get("metadata") if isinstance(task, dict) else None
    if not isinstance(meta, dict):
        meta = {}
    derived = metadata_from_case_key(case_key)
    out: dict[str, str] = {}
    for field in ("category", "difficulty", "audio_condition"):
        value = meta.get(field)
        out[field] = "" if value in (None, "") else str(value)
        if not out[field]:
            out[field] = derived[field]
    return out


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


def csv_void_reason(detail: dict[str, Any], status: str | None = None) -> str:
    """VOID only when there was no scorable call.

    `classify_detail` also voids 'expected pairing present, actuals empty' —
    that is a tool FAIL for this CSV, not a void. Completed calls with a
    transcript / traces stay in the score.
    """
    st = str(status or detail.get("status") or "")
    if st in verify_run.NOT_FINAL:
        return ""
    if st in verify_run.NO_CONVERSATION:
        return f"no conversation ({st})"
    if not detail.get("trace_ids"):
        return "no trace linked — the harness never posted trace_ids"
    return ""


def score_missing_hangup_dump(row: dict[str, Any]) -> None:
    """A scorable call with no dump is a hangup FAIL, same rule as empty tools.

    Only when the case actually expects a DB state; a task without
    `exp_db_state` stays skipped and out of `combined_pass`.
    """
    state = row.get("state") or {}
    if not state.get("skipped") or state.get("passed") is not None:
        return
    task = row.get("task")
    if not isinstance(task, dict) or verify_task_run.expected_state(task) is None:
        return
    row["state"] = {**state, "passed": False, "skipped": False}
    row["passed"] = False


def apply_csv_mark(row: dict[str, Any]) -> None:
    detail = row.get("detail") or {}
    pending = bool(row.get("pending"))
    reason = csv_void_reason(detail, row.get("status"))
    row["void_reason"] = reason or None
    if not pending and not reason:
        score_missing_hangup_dump(row)
    if pending:
        row["mark"] = "wait"
    elif reason:
        row["mark"] = "VOID"
    elif not row.get("task"):
        row["mark"] = "MISS"
    elif row.get("passed"):
        row["mark"] = "pass"
    else:
        row["mark"] = "FAIL"


def parse_run_spec(spec: str) -> tuple[str, list[str]]:
    """PRIMARY+RETRY or PRIMARY,RETRY → (primary, [retries])."""
    parts = [p.strip() for p in spec.replace(",", "+").split("+") if p.strip()]
    if not parts:
        raise SystemExit(f"no run ids in {spec!r}")
    return parts[0], parts[1:]


def _fill_holes(
    primary: list[dict[str, Any]],
    retries: list[list[dict[str, Any]]],
    *,
    is_hole: Any,
    usable: Any,
) -> list[dict[str, Any]]:
    """Replace hole slots with same-digital-human retry rows, in run order."""
    pool: dict[str, list[dict[str, Any]]] = {}
    for pack in retries:
        for row in pack:
            if usable(row):
                pool.setdefault(str(row.get("digital_human_id")), []).append(row)
    out: list[dict[str, Any]] = []
    for slot in primary:
        candidates = pool.get(str(slot.get("digital_human_id")))
        if is_hole(slot) and candidates:
            out.append(candidates.pop(0))
        else:
            out.append(slot)
    return out


def fill_connection_holes(
    primary: list[dict[str, Any]],
    retries: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Replace unanswered slots only. Leave connected VOID rows in place.

    A later COMPLETED retry is usable even when its tool list is empty —
    that conversation still replaces NO_ANSWER.
    """
    return _fill_holes(
        primary,
        retries,
        is_hole=lambda s: str(s.get("status") or "") in CONNECTION_HOLES,
        usable=lambda r: str(r.get("status") or "") == "COMPLETED" and not r.get("pending"),
    )


def fill_void_holes(
    primary: list[dict[str, Any]],
    retries: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """--fill-voids: also replace connected-but-VOID rows, with non-void retries."""
    return _fill_holes(
        primary,
        retries,
        is_hole=lambda s: str(s.get("status") or "") in CONNECTION_HOLES
        or bool(s.get("void_reason")),
        usable=lambda r: str(r.get("status") or "") == "COMPLETED"
        and not r.get("pending")
        and not r.get("void_reason"),
    )


def load_audio_eval(result_id: str, directory: Path | None) -> dict[str, Any] | None:
    if directory is None:
        return None
    path = directory / f"{result_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _num_cell(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value)


def apply_audio_eval_latency(row: dict[str, str], package: dict[str, Any] | None) -> None:
    """Overwrite latency / barge-in from the diarization package. No Bluejay metrics."""
    if not package:
        return
    for key in AUDIO_EVAL_CLEARS:
        row[key] = ""
    agent = package.get("agent_latency_stats") if isinstance(package.get("agent_latency_stats"), dict) else {}
    customer = package.get("customer_latency_stats") if isinstance(package.get("customer_latency_stats"), dict) else {}
    inter = package.get("interruptions") if isinstance(package.get("interruptions"), dict) else {}
    row["builtin_avg_agent_latency"] = _num_cell(agent.get("avg_ms"))
    row["builtin_max_agent_latency"] = _num_cell(agent.get("max_ms"))
    row["builtin_p50_agent_latency"] = _num_cell(agent.get("p50_ms"))
    row["builtin_p90_agent_latency"] = _num_cell(agent.get("p90_ms"))
    row["eval_avg_agent_latency"] = _num_cell(agent.get("avg_ms"))
    row["builtin_avg_customer_latency"] = _num_cell(customer.get("avg_ms"))
    row["builtin_p50_customer_latency"] = _num_cell(customer.get("p50_ms"))
    row["builtin_p90_customer_latency"] = _num_cell(customer.get("p90_ms"))
    row["builtin_agent_interruption_count"] = _num_cell(inter.get("agent_interruption_count"))
    row["builtin_customer_interruption_count"] = _num_cell(inter.get("customer_interruption_count"))
    for turn in package.get("transcript") or []:
        if not isinstance(turn, dict):
            continue
        if str(turn.get("speaker") or "").upper() != "AGENT":
            continue
        start = turn.get("start")
        if start is None:
            continue
        try:
            row["builtin_time_to_first_agent_utterance"] = str(int(round(float(start) * 1000)))
        except (TypeError, ValueError):
            pass
        break


def load_pricing(path: Path | None = None) -> dict[str, Any]:
    global _PRICING_CACHE
    if _PRICING_CACHE is not None:
        return _PRICING_CACHE
    dest = path or DEFAULT_PRICING
    if dest.is_file():
        _PRICING_CACHE = json.loads(dest.read_text())
    else:
        _PRICING_CACHE = {}
    return _PRICING_CACHE


def normalize_model_id(model: str, pricing: dict[str, Any] | None = None) -> str:
    """Match s2s-model-pricing.json `normalize_model_id`."""
    s = str(model or "").strip().lower()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    s = re.sub(r"@\d{8}$", "", s)
    s = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", s)
    s = re.sub(r"-preview-\d{2}-\d{4}", "-preview", s)
    s = s.replace("-native-audio-preview", "-native-audio")
    rates = (pricing or load_pricing()).get("token_pricing") or {}
    if s not in rates and s.endswith("-preview") and s[: -len("-preview")] in rates:
        s = s[: -len("-preview")]
    return s


def _usage_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _usage_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Root-span audio attrs: grok stamps mivas.audio.duration_{s,minutes} on
# `realtime_session`, livekit/cascaded the stt/tts lanes on `voice.call`.
DURATION_ATTRS: dict[str, tuple[str, ...]] = {
    "audio_duration_s": ("mivas.audio.duration_s", "mivas.audio_duration_s"),
    "audio_duration_minutes": ("mivas.audio.duration_minutes", "mivas.audio_duration_minutes"),
    "stt_audio_duration_s": ("mivas.stt.audio_duration_s",),
    "tts_audio_duration_s": ("mivas.tts.audio_duration_s",),
    "tts_characters": ("mivas.tts.characters",),
}


def durations_from_trace_body(body: dict[str, Any]) -> dict[str, float]:
    """First span carrying each audio attr wins (the root stamps them)."""
    try:
        rows = body["data"]["data"]["results"][0]["rows"]
    except (KeyError, IndexError, TypeError):
        return {}
    out: dict[str, float] = {}
    for row in rows:
        attrs = (row.get("data") or {}).get("attributes") or {}
        for key, names in DURATION_ATTRS.items():
            if key in out:
                continue
            for name in names:
                value = _usage_float(attrs.get(name))
                if value is not None:
                    out[key] = int(value) if key == "tts_characters" else value
                    break
    return out


def _token_cost(rates: dict[str, Any], usage: dict[str, int]) -> tuple[float, bool]:
    """Cached tokens REPLACE part of the input lane, they are not billed on top.

    Cache hits land on the longest common prefix — the text system prompt and
    history — so cached is charged against text first, the remainder audio.
    A vendor with no cached lane bills those tokens at the full input rate.
    """
    cached_text = min(usage["cached"], usage["input_text"])
    cached_audio = min(usage["cached"] - cached_text, usage["input_audio"])
    lanes = (
        (usage["input_text"] - cached_text, "inputText", "inputText"),
        (cached_text, "cachedText", "inputText"),
        (usage["input_audio"] - cached_audio, "inputAudio", "inputAudio"),
        (cached_audio, "cachedAudio", "inputAudio"),
        (usage["output_text"], "outputText", "outputText"),
        (usage["output_audio"], "outputAudio", "outputAudio"),
    )
    usd = 0.0
    priced = False
    for tokens, key, fallback in lanes:
        rate = rates.get(key)
        if rate is None:
            rate = rates.get(fallback)
        if rate is None:
            continue
        priced = True
        usd += tokens * float(rate) / 1_000_000.0
    return usd, priced


def _component_costs(pricing: dict[str, Any], usage: dict[str, Any]) -> dict[str, float]:
    """STT/TTS legs of a cascaded pair, from the minutes/characters on the root."""
    components = pricing.get("component_pricing") or {}
    out: dict[str, float] = {}
    seconds = usage.get("stt_audio_duration_s")
    stt_rate = (components.get("stt") or {}).get("usd_per_minute")
    if seconds is not None and stt_rate is not None:
        out["cost_stt_usd"] = float(seconds) / 60.0 * float(stt_rate)
    characters = usage.get("tts_characters")
    tts_rate = (components.get("tts") or {}).get("usd_per_1k_characters")
    if characters is not None and tts_rate is not None:
        out["cost_tts_usd"] = float(characters) / 1000.0 * float(tts_rate)
    return out


def _leaf_usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Usage spans with the session-root duplicates removed.

    openai/qwen/aws stamp `gen_ai.usage.*` on BOTH the per-response child spans
    and the session root, where the root repeats their sum. Keeping both doubles
    every token. Drop any usage span that is an ancestor of another usage span.
    """
    parent: dict[str, str] = {}
    usage: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        data = row.get("data") or {}
        attrs = data.get("attributes") or {}
        span_id = str(data.get("span_id") or "")
        parent[span_id] = str(data.get("parent_span_id") or "")
        if "gen_ai.usage.input_tokens" in attrs or "gen_ai.usage.output_tokens" in attrs:
            usage.append((span_id, row))
    ids = {sid for sid, _ in usage if sid}
    ancestors: set[str] = set()
    for sid, _ in usage:
        node = parent.get(sid, "")
        for _ in range(64):
            if not node:
                break
            if node in ids:
                ancestors.add(node)
            node = parent.get(node, "")
    return [row for sid, row in usage if sid not in ancestors]


def usage_from_trace_body(body: dict[str, Any]) -> dict[str, Any]:
    """Summed per-response gen_ai.usage spans (root duplicates dropped).

    Every lane is summed across responses: a realtime turn re-sends the whole
    conversation and is billed for it (OpenAI "the entire conversation is sent
    to the model for each Response"; Vertex "tokens from past turns are
    re-processed and accounted for in each new turn"). Growing per-response
    input is context growth, not a cumulative counter.
    """
    snaps: list[tuple[str, dict[str, int], str]] = []
    try:
        rows = body["data"]["data"]["results"][0]["rows"]
    except (KeyError, IndexError, TypeError):
        rows = []
    for row in _leaf_usage_rows(rows):
        attrs = (row.get("data") or {}).get("attributes") or {}
        ts = str((row.get("data") or {}).get("timestamp") or row.get("timestamp") or "")
        model = str(attrs.get("gen_ai.request.model") or "")
        snap = {
            "input_text": _usage_int(
                attrs.get("gen_ai.usage.input_text_tokens")
                or attrs.get("gen_ai.usage.input_tokens_text")
            ),
            "input_audio": _usage_int(attrs.get("gen_ai.usage.input_audio_tokens")),
            "input_total": _usage_int(attrs.get("gen_ai.usage.input_tokens")),
            "output_text": _usage_int(
                attrs.get("gen_ai.usage.output_text_tokens")
                or attrs.get("gen_ai.usage.output_tokens_text")
            ),
            "output_audio": _usage_int(attrs.get("gen_ai.usage.output_audio_tokens")),
            "output_total": _usage_int(attrs.get("gen_ai.usage.output_tokens")),
            "cached": _usage_int(
                attrs.get("gen_ai.usage.input_cached_tokens")
                or attrs.get("gen_ai.usage.cached_tokens")
            ),
        }
        leftover_in = snap["input_total"] - snap["input_text"] - snap["input_audio"]
        if leftover_in > 0:
            snap["input_text"] += leftover_in
        leftover_out = snap["output_total"] - snap["output_text"] - snap["output_audio"]
        if leftover_out > 0:
            snap["output_audio"] += leftover_out
        snaps.append((ts, snap, model))
    durations = durations_from_trace_body(body)
    if not snaps:
        return dict(durations) if durations else {}
    snaps.sort(key=lambda item: item[0])
    model = next((m for _, _, m in reversed(snaps) if m), "")
    usage = {
        key: sum(s[key] for _, s, _ in snaps)
        for key in ("input_text", "input_audio", "cached", "output_text", "output_audio")
    }
    # `cached` is a SUBSET of the input lanes everywhere we price (OpenAI
    # input_token_details.cached_tokens, Gemini cachedContentTokenCount,
    # LiveKit prompt_cached_tokens) — never add it to the total.
    usage["total"] = (
        usage["input_text"] + usage["input_audio"]
        + usage["output_text"] + usage["output_audio"]
    )
    usage["model"] = model
    usage.update(durations)
    pricing = load_pricing()
    usage["norm"] = normalize_model_id(model, pricing)
    rates = (pricing.get("token_pricing") or {}).get(usage["norm"]) or {}
    usd, priced = _token_cost(rates, usage)
    minute_rate = (pricing.get("per_minute_pricing") or {}).get(usage["norm"])
    if not priced and minute_rate is not None and not usage["total"]:
        minutes = usage.get("audio_duration_minutes")
        if minutes is None and usage.get("audio_duration_s") is not None:
            minutes = usage["audio_duration_s"] / 60.0
        if minutes is not None:
            priced = True
            usd += minutes * float(minute_rate)
    usage["cost_llm_usd"] = round(usd, 6) if priced else None
    total = usd if priced else 0.0
    for key, component in _component_costs(pricing, usage).items():
        usage[key] = round(component, 6)
        total += component
        priced = True
    usage["cost_usd"] = round(total, 6) if priced else None
    return usage


def _bj_post_trace(trace_id: str) -> dict[str, Any] | None:
    key = os.environ.get("BLUEJAY_API_KEY") or ""
    if not key:
        return None
    api = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
    req = urllib.request.Request(
        f"{api}/traces/{trace_id}",
        data=b"{}",
        method="POST",
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except Exception:
        return None


def fetch_costs_for_results(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """trace_id → usage, then result_id → merged usage."""
    tids: list[str] = []
    by_result: dict[str, list[str]] = {}
    for row in results:
        rid = str(row.get("result_id") or "")
        detail = row.get("detail") or {}
        traces = [str(t) for t in (detail.get("trace_ids") or row.get("trace_ids") or []) if t]
        by_result[rid] = traces
        tids.extend(traces)
    unique = list(dict.fromkeys(tids))
    bodies: dict[str, dict[str, Any]] = {}
    if unique:
        workers = max(1, min(20, len(unique)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_bj_post_trace, tid): tid for tid in unique}
            for fut in as_completed(futs):
                body = fut.result()
                if body:
                    bodies[futs[fut]] = body
    out: dict[str, dict[str, Any]] = {}
    for rid, traces in by_result.items():
        merged: dict[str, Any] = {}
        for tid in traces:
            part = usage_from_trace_body(bodies.get(tid) or {})
            if not part:
                continue
            if not merged:
                merged = dict(part)
                continue
            for key in ("input_text", "input_audio", "output_text", "output_audio", "cached", "total"):
                merged[key] = merged.get(key, 0) + part.get(key, 0)
            for key in DURATION_ATTRS:
                if part.get(key) is not None:
                    merged[key] = (merged.get(key) or 0) + part[key]
            for key in ("cost_usd", "cost_llm_usd", "cost_stt_usd", "cost_tts_usd"):
                if part.get(key) is not None:
                    merged[key] = (merged.get(key) or 0) + part[key]
            merged["model"] = part.get("model") or merged.get("model")
            merged["norm"] = part.get("norm") or merged.get("norm")
        if merged:
            out[rid] = merged
    return out


def apply_cost_cells(row: dict[str, str], usage: dict[str, Any] | None) -> None:
    usage = usage or {}
    for key in ("cost_usd", "cost_llm_usd", "cost_stt_usd", "cost_tts_usd"):
        row[key] = "" if usage.get(key) is None else f"{usage[key]:.6f}"
    row["cost_model"] = str(usage.get("norm") or "")
    row["input_text_tokens"] = _num_cell(usage.get("input_text"))
    row["input_audio_tokens"] = _num_cell(usage.get("input_audio"))
    row["output_text_tokens"] = _num_cell(usage.get("output_text"))
    row["output_audio_tokens"] = _num_cell(usage.get("output_audio"))
    row["cached_tokens"] = _num_cell(usage.get("cached"))
    row["total_tokens"] = _num_cell(usage.get("total"))
    for key in DURATION_ATTRS:
        row[key] = _num_cell(usage.get(key))


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
    audio_eval: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
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
    case_key = str(scored.get("case_key") or "")
    meta = task_metadata(task if isinstance(task, dict) else None, case_key)
    path, directory = task_link(industry, case_key, task)
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
        "case_key": case_key,
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
        "cost_usd": "",
        "cost_llm_usd": "",
        "cost_stt_usd": "",
        "cost_tts_usd": "",
        "cost_model": "",
        "input_text_tokens": "",
        "input_audio_tokens": "",
        "output_text_tokens": "",
        "output_audio_tokens": "",
        "cached_tokens": "",
        "total_tokens": "",
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
    apply_audio_eval_latency(row, audio_eval)
    apply_cost_cells(row, cost)
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


def _pg_tool_groups(listed: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not listed:
        return []
    out: list[dict[str, Any]] = []
    for group in listed.get("tool_calls") or []:
        if not isinstance(group, dict):
            continue
        actuals = group.get("actual") or []
        if not actuals:
            continue
        out.append({"name": group.get("name"), "actual": actuals})
    return out


def overlay_tool_actuals(detail: dict[str, Any], pg_row: dict[str, Any] | None) -> dict[str, Any]:
    """Prefer Postgres args; if those are empty, keep listing names."""
    detail = dict(detail)
    listing_calls = verify_task_run.actual_tool_calls(detail)
    pg_groups = _pg_tool_groups(pg_row)
    if pg_groups:
        detail["tool_calls"] = pg_groups
    elif listing_calls:
        pass
    if pg_row and pg_row.get("trace_ids") and not detail.get("trace_ids"):
        detail["trace_ids"] = pg_row["trace_ids"]
    return detail


def _resolve_actuals_dir(
    run_id: str,
    slug: str,
    actuals_dir: Path | None,
) -> tuple[Path | None, str | None]:
    if actuals_dir is not None:
        return actuals_dir, None
    dest = ROOT / "actual-final-state"
    # Scope to this pair+run — the shared root also holds other pairs' dumps,
    # and its mere existence must not suppress the S3 pull for this run.
    run_dir = dest / slug / str(run_id)
    if any(run_dir.glob("*/*/final.json")):
        return run_dir, None
    if os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip():
        try:
            pulled = verify_task_run._pull_actuals(str(run_id), slug, dest)
            return dest / slug / str(pulled.get("run_id") or run_id), None
        except SystemExit as exc:
            return None, f"S3 pull failed: {exc}"
    return None, "MIVAS_SNAPSHOT_BUCKET not set; state compare skipped"


def _prefetch_transcripts(details: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def one(detail: dict[str, Any]) -> tuple[str, list[str]]:
        rid = str(detail.get("id") or "")
        return rid, verify_task_run.result_transcript_lines(detail)

    workers = max(1, min(20, len(details) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rid, lines in pool.map(one, details):
            if rid:
                out[rid] = lines
    return out


def collect_from_listing(
    run_id: str,
    industry: str,
    *,
    actuals_dir: Path | None = None,
    harness: str = "openai/realtime-2.1",
    slug: str | None = None,
    sim_hint: str | None = None,
    pg_listed: list[dict[str, Any]] | None = None,
    fetch_details: bool = False,
) -> dict[str, Any]:
    """Score a run from the listing (one HTTP) plus hangup dumps. Optional PG overlay."""
    if fetch_details:
        scored = verify_task_run.collect_scored_results(
            run_id, industry, actuals_dir=actuals_dir,
            harness=harness, slug=slug, sim_hint=sim_hint,
        )
        for row in scored["results"]:
            row["run_id"] = str(run_id)
            apply_csv_mark(row)
        return scored

    slug = slug or verify_task_run.pull.pair_slug(harness, industry)
    schemas = verify_task_run.load_tool_schemas(industry)
    run_body = verify_task_run._get_with_retry(f"retrieve-simulation-results/{run_id}")
    run = run_body.get("simulation_run") or {}
    listings = run_body.get("simulation_results") or run_body.get("results") or []
    sim_id = str(run.get("simulation_id") or sim_hint or "")
    dh_by_id = verify_task_run._digital_humans_by_sim(sim_id) if sim_id else {}
    actuals_dir, state_skip_note = _resolve_actuals_dir(str(run_id), slug, actuals_dir)
    actual_by_result = verify_task_run._actual_by_result(actuals_dir)
    pg_by_id = {str(row.get("id") or ""): row for row in (pg_listed or [])}

    details: list[dict[str, Any]] = []
    for summary in listings:
        if not isinstance(summary, dict) or summary.get("id") in (None, ""):
            continue
        details.append(overlay_tool_actuals(summary, pg_by_id.get(str(summary["id"]))))

    lines_by_id = _prefetch_transcripts(details)
    rows: list[dict[str, Any]] = []
    for detail in details:
        result_id = str(detail.get("id") or "")
        classified = verify_run.classify_detail(detail, result_id)
        dh = detail.get("digital_human") or dh_by_id.get(str(detail.get("digital_human_id"))) or {}
        case_key = verify_task_run.case_key_from_dh(dh) if dh else None
        if not case_key and pg_by_id.get(result_id, {}).get("case_key"):
            case_key = str(pg_by_id[result_id]["case_key"])
        task = verify_task_run.load_task(industry, case_key) if case_key else None
        actual_state = None
        note = state_skip_note
        dump = actual_by_result.get(result_id)
        if dump and dump.get("path"):
            actual_state = json.loads(Path(dump["path"]).read_text())
            note = None
        elif dump is None and actuals_dir is not None and state_skip_note is None:
            note = "no hangup dump for this result"
        check = verify_task_run.verify_result(
            {**detail, "transcript_url": None},
            task,
            actual_state,
            state_note=note,
            schemas=schemas,
            industry=industry,
        )
        lines = lines_by_id.get(result_id) or []
        check["agent_chars"] = len(verify_task_run.agent_transcript(lines))
        rows.append({
            "run_id": str(run_id),
            "result_id": result_id,
            "case_key": case_key,
            "digital_human_id": detail.get("digital_human_id"),
            "status": classified.get("status") or detail.get("status"),
            "pending": classified.get("pending"),
            "void_reason": None,
            "mark": "",
            "detail": detail,
            "task": task,
            "digital_human": dh,
            "transcript_lines": lines,
            "actual_state": actual_state,
            "actual_tool_calls": verify_task_run.actual_tool_calls(detail),
            **check,
        })
        apply_csv_mark(rows[-1])
    return {
        "run_id": str(run_id),
        "industry": industry,
        "run": run,
        "simulation_id": sim_id,
        "results": rows,
        "actuals_dir": str(actuals_dir) if actuals_dir else None,
        "state_skip_note": state_skip_note,
    }


def collect_filled_results(
    primary: str,
    retries: list[str],
    industry: str,
    *,
    actuals_dir: Path | None = None,
    harness: str = "openai/realtime-2.1",
    slug: str | None = None,
    sim_hint: str | None = None,
    include_void_holes: bool = False,
    fetch_details: bool = False,
) -> dict[str, Any]:
    """Score primary, then replace unanswered slots from later runs."""
    # ponytail: the Postgres bulk-listing fast path left with verify_runs_bulk.py;
    # every run now lists through the Bluejay API.
    pg_by_run: dict[str, list[dict[str, Any]]] = {}
    scored = collect_from_listing(
        primary,
        industry,
        actuals_dir=actuals_dir,
        harness=harness,
        slug=slug,
        sim_hint=sim_hint,
        pg_listed=pg_by_run.get(str(primary)),
        fetch_details=fetch_details,
    )
    retry_packs: list[list[dict[str, Any]]] = []
    for rid in retries:
        extra = collect_from_listing(
            rid,
            industry,
            actuals_dir=actuals_dir,
            harness=harness,
            slug=slug,
            sim_hint=sim_hint,
            pg_listed=pg_by_run.get(str(rid)),
            fetch_details=fetch_details,
        )
        retry_packs.append(extra["results"])
    if retry_packs:
        if include_void_holes:
            scored["results"] = fill_void_holes(scored["results"], retry_packs)
        else:
            scored["results"] = fill_connection_holes(scored["results"], retry_packs)
    return scored


def default_out_path(industry: str, harness: str, primary: str) -> Path:
    return ROOT / "eval_outputs" / f"{industry}-{harness.replace('/', '-')}-{primary}.csv"


def main(argv: list[str] | None = None) -> int:
    verify_task_run.load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run_id",
        nargs="?",
        help="Primary run, or PRIMARY+RETRY[+RETRY...] (comma also ok)",
    )
    parser.add_argument("--sim", help="Use this simulation's latest run")
    parser.add_argument("--industry", required=True)
    parser.add_argument(
        "--out",
        help="UTF-8 CSV path (default: eval_outputs/{industry}-{harness}-{primary}.csv)",
    )
    parser.add_argument("--actuals-dir", type=Path, help="Local hangup dumps (skip S3)")
    parser.add_argument("--harness", default="openai/realtime-2.1")
    parser.add_argument("--slug", help="Override the S3 / dump slug")
    parser.add_argument(
        "--audio-eval-dir",
        type=Path,
        default=DEFAULT_AUDIO_EVAL,
        help="Per-result latency JSON dir (default: verify-out/audio_eval)",
    )
    parser.add_argument(
        "--pricing",
        type=Path,
        default=DEFAULT_PRICING,
        help="s2s-model-pricing.json",
    )
    parser.add_argument("--skip-cost", action="store_true", help="Do not fetch traces")
    parser.add_argument(
        "--fill-voids",
        action="store_true",
        help="Also replace connected VOID rows with later-run matches",
    )
    parser.add_argument(
        "--fetch-details",
        action="store_true",
        help="GET each result instead of using the run listing",
    )
    args = parser.parse_args(argv)

    primary = args.run_id
    retries: list[str] = []
    if args.sim:
        primary = verify_run.latest_run_for_sim(args.sim)
    elif primary:
        primary, retries = parse_run_spec(str(primary))
    if not primary:
        parser.error("give a run id (PRIMARY+RETRY) or --sim")

    global _PRICING_CACHE
    _PRICING_CACHE = None
    if args.pricing.is_file():
        load_pricing(args.pricing)

    scored = collect_filled_results(
        str(primary),
        retries,
        args.industry,
        actuals_dir=args.actuals_dir,
        harness=args.harness,
        slug=args.slug,
        sim_hint=args.sim,
        include_void_holes=args.fill_voids,
        fetch_details=args.fetch_details,
    )
    agent_id = agent_id_of(scored.get("run") or {}, scored["results"], scored.get("simulation_id") or "")
    assign_conversation_indexes(scored["results"])

    audio_dir = args.audio_eval_dir if args.audio_eval_dir.is_dir() else None
    costs: dict[str, dict[str, Any]] = {}
    if not args.skip_cost:
        costs = fetch_costs_for_results(scored["results"])

    rows = [
        result_row(
            row,
            run_id=str(row.get("run_id") or scored["run_id"]),
            simulation_id=scored.get("simulation_id") or "",
            agent_id=agent_id,
            industry=args.industry,
            conversation_index=row.get("conversation_index"),
            transcript_lines=row.get("transcript_lines"),
            harness=args.harness,
            fetch_costs=not args.skip_cost,
            audio_eval=load_audio_eval(str(row.get("result_id") or ""), audio_dir),
            cost=costs.get(str(row.get("result_id") or "")),
        )
        for row in scored["results"]
    ]
    if not rows:
        raise SystemExit(f"no results for run {primary}")

    if args.out:
        out = Path(args.out)
    else:
        out = default_out_path(args.industry, args.harness, str(primary))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        fields = write_csv(rows, handle)
    print(f"wrote {out} ({len(rows)} rows × {len(fields)} columns)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
