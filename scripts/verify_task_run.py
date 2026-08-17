"""Score a finished Bluejay run against MIVAS task.json files.

Three independent checks per result (combined pass is AND):

1. Hangup GET /state vs `exp_db_state` on office tables only
   (`patients`, `appointments`, `waitlist`). Ignores `tool_events`,
   catalog tables, `created_at`, and unconstrained description text.
2. Tool-call adherence — Bluejay `actual` invocations vs `exp_tool_calls`,
   matching constrained args from `tools.json` (enums, ids, facts). Prose
   args must be non-empty when required; empty live args are name-only.
3. Handoff adherence — `exp_handoff_path` as an in-order subsequence of
   actual transfer_* tools in wall-clock order (`start_offset_ms`), not
   Bluejay's name-grouped `tool_calls` array.

Join result → task by the DH `case_key` trait or the `test_name` prefix
(`C1-E1:`). Empty expected tools pass that half.
Missing S3 dumps skip state with a note; `--actuals-dir` compares local
files without S3.

    uv run python scripts/verify_task_run.py RUN_ID --industry healthcare
    uv run python scripts/verify_task_run.py --sim SIM_ID --industry healthcare
    uv run python scripts/verify_task_run.py RUN_ID --industry healthcare --actuals-dir actual-final-state/...
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INDUSTRY_ROOT = ROOT / "industries"
SCRIPTS = ROOT / "scripts"

CASE_KEY_RE = re.compile(r"^[A-Z]+\d*-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
AGENT_ROLES = frozenset({"AGENT", "ASSISTANT"})
USER_ROLES = frozenset({"USER", "CALLER", "CUSTOMER", "HUMAN", "PATIENT"})
HANDOFF_TOOLS = frozenset({
    "transfer_to_identity", "transfer_to_scheduling", "transfer_to_coverage",
    "transfer_to_cosmetic", "transfer_to_billing", "transfer_to_clinical",
    "transfer_to_human",
})

OFFICE_TABLES = ("patients", "appointments", "waitlist")
IGNORE_ROW_KEYS = frozenset({"created_at", "description"})
PHONE_KEY_RE = re.compile(r"phone|_e164$", re.I)

PROSE_ARG_KEYS = frozenset({
    "notes", "patient_safe_message",
})
FACT_ARG_KEYS = frozenset({
    "full_name", "first_name", "last_name", "dob", "zip", "carrier",
    "member_id", "destination", "queue", "location_id", "provider_id",
    "slot_id", "appointment_id", "start", "end", "new_start", "new_end",
    "service_date", "group_number", "channel", "template_id",
    "medication_name", "visit_class", "topic", "service", "order_type",
    "appointment_type_code", "cancellation_reason_code", "fee_line_item_id",
    "line_item_id", "next_intent", "subscriber_relationship",
})
KEEP_ARG_TYPES = frozenset({"number", "integer", "boolean"})
KEEP_ARG_FORMATS = frozenset({"date", "date-time"})

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verify_run = _load("verify_run", SCRIPTS / "verify_run.py")
attribution = _load("attribution_bundle", SCRIPTS / "attribution_bundle.py")
pull = _load("pull_actual_final_state", SCRIPTS / "pull_actual_final_state.py")
efs = _load("expected_final_state", SCRIPTS / "expected_final_state.py")

canonical_state = efs.canonical_state
states_match = efs.states_match
iter_actual_finals = efs.iter_actual_finals
load_dotenv = efs.load_dotenv
transcript_lines = attribution.transcript_lines


def trait_value(dh: dict[str, Any], name: str) -> str | None:
    for item in dh.get("traits") or []:
        if item.get("trait_name") == name:
            value = item.get("value")
            return None if value is None else str(value)
    return None


def case_key_from_dh(dh: dict[str, Any]) -> str | None:
    keyed = trait_value(dh, "case_key")
    if keyed:
        return keyed
    test_name = str(dh.get("test_name") or "")
    if ":" in test_name:
        prefix = test_name.split(":", 1)[0].strip()
        if CASE_KEY_RE.match(prefix):
            return prefix
    return None


def load_task(industry: str, case_key: str) -> dict[str, Any] | None:
    folder = INDUSTRY_ROOT / industry / "tasks" / case_key
    path = folder / "task.json"
    if not path.is_file():
        return None
    task = json.loads(path.read_text())
    if not isinstance(task, dict):
        return None
    if "exp_db_state" not in task:
        sibling = folder / "exp_db_state.json"
        if sibling.is_file():
            task["exp_db_state"] = json.loads(sibling.read_text())
    return task


def expected_state(task: dict[str, Any]) -> Any:
    return task.get("exp_db_state")


def inline_transcript_lines(result: dict[str, Any]) -> list[str]:
    data = result.get("transcript") or result.get("messages")
    if isinstance(data, str):
        return [data]
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        who = (item.get("role") or item.get("speaker") or "?").upper()
        said = item.get("utterance") or item.get("content") or item.get("text") or ""
        out.append(f"{who}: {said}")
    return out


def result_transcript_lines(result: dict[str, Any]) -> list[str]:
    """transcript_url first (attribution_bundle), then inline transcript/messages."""
    lines = transcript_lines(result)
    if lines:
        return lines
    return inline_transcript_lines(result)


def agent_transcript(lines: list[str]) -> str:
    texts: list[str] = []
    for line in lines:
        who, sep, said = line.partition(": ")
        if not sep:
            continue
        role = who.strip().upper()
        if role in AGENT_ROLES:
            texts.append(said)
    return "\n".join(texts)


def _digits_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _is_phone_key(key: str) -> bool:
    return bool(PHONE_KEY_RE.search(key))


def _canon_row(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in IGNORE_ROW_KEYS:
            continue
        if isinstance(value, str) and _is_phone_key(key):
            out[key] = _digits_phone(value)
        else:
            out[key] = value
    return out


def office_canonical(state: Any) -> dict[str, Any]:
    """Office records only: patients / appointments / waitlist."""
    src = state if isinstance(state, dict) else {}
    out: dict[str, Any] = {}
    for table in OFFICE_TABLES:
        rows = src.get(table) or []
        if not isinstance(rows, list):
            rows = []
        canon = [_canon_row(row) for row in rows]
        out[table] = sorted(
            canon,
            key=lambda row: json.dumps(row, sort_keys=True, default=str),
        )
    return out


def office_states_match(expected: Any, actual: Any) -> bool:
    return office_canonical(expected) == office_canonical(actual)


def load_tool_schemas(industry: str) -> dict[str, Any]:
    if industry in _TOOL_SCHEMAS:
        return _TOOL_SCHEMAS[industry]
    path = INDUSTRY_ROOT / industry / "tools.json"
    schemas: dict[str, Any] = {}
    if path.is_file():
        data = json.loads(path.read_text())
        for tool in data.get("tools") or []:
            name = tool.get("name")
            if name:
                schemas[str(name)] = tool
    _TOOL_SCHEMAS[industry] = schemas
    return schemas


def _input_schema(tool: dict[str, Any] | None) -> dict[str, Any]:
    raw = (tool or {}).get("inputSchema") or {}
    return raw if isinstance(raw, dict) else {}


def _prop_schema(tool: dict[str, Any] | None, key: str) -> dict[str, Any]:
    props = _input_schema(tool).get("properties") or {}
    spec = props.get(key) if isinstance(props, dict) else None
    return spec if isinstance(spec, dict) else {}


def _required_keys(tool: dict[str, Any] | None) -> set[str]:
    required = _input_schema(tool).get("required") or []
    return {str(item) for item in required}


def _is_prose_arg(key: str, prop: dict[str, Any]) -> bool:
    if prop.get("enum"):
        return False
    types = prop.get("type")
    type_set = {types} if isinstance(types, str) else set(types or [])
    if type_set & KEEP_ARG_TYPES:
        return False
    if prop.get("format") in KEEP_ARG_FORMATS:
        return False
    if prop.get("pattern"):
        return False
    if key.endswith("_id") or key.endswith("_ids") or key.endswith("_e164"):
        return False
    if key in FACT_ARG_KEYS:
        return False
    if key in PROSE_ARG_KEYS:
        return True
    if key == "reason" and not prop.get("enum"):
        return True
    return key in PROSE_ARG_KEYS


def _params_of(call: dict[str, Any] | None) -> dict[str, Any]:
    raw = (call or {}).get("parameters")
    if not isinstance(raw, dict):
        raw = (call or {}).get("arguments")
    if not isinstance(raw, dict):
        raw = (call or {}).get("args")
    return dict(raw) if isinstance(raw, dict) else {}


def _params_empty(params: dict[str, Any]) -> bool:
    if not params:
        return True
    return all(value in (None, "", [], {}) for value in params.values())


def _output_fields(call: dict[str, Any] | None) -> dict[str, Any]:
    call = call or {}
    output = call.get("output") if isinstance(call.get("output"), dict) else {}
    fields: dict[str, Any] = {}
    if "ok" in call:
        fields["ok"] = call.get("ok")
    elif "ok" in output:
        fields["ok"] = output.get("ok")
    if "error_code" in call:
        fields["error_code"] = call.get("error_code")
    elif "error_code" in output:
        fields["error_code"] = output.get("error_code")
    return fields


def _as_calls(items: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"name": item, "parameters": {}})
        elif isinstance(item, dict) and item.get("name"):
            out.append(item)
    return out


# Office ids/names the healthcare tool server already aliases.
_LOCATION_IDS = {
    "loc_park_ave": "park avenue",
    "loc_brooklyn_heights": "brooklyn heights",
    "loc_windermere": "windermere",
}
HARNESS_ONLY_KEYS = frozenset({"to", "from"})


def _canon_location(value: Any) -> str:
    said = str(value or "").strip().lower()
    if not said:
        return ""
    for loc_id, name in _LOCATION_IDS.items():
        if said == loc_id or said == name:
            return loc_id
    return said


def _same_location(expected: Any, actual: Any) -> bool:
    left, right = _canon_location(expected), _canon_location(actual)
    return bool(left) and left == right


_CARRIER_SLUGS = {
    "aetna": "aetna",
    "unitedhealthcare": "unitedhealthcare",
    "cigna": "cigna",
    "bcbs": "bcbs",
    "bluecross": "bcbs",
    "bluecrossblueshield": "bcbs",
    "medicare": "medicare",
    "medicaid": "medicaid",
    "oscarhealth": "oscar_health",
    "oscar": "oscar_health",
    "other": "other",
}


def _canon_carrier(value: Any) -> str:
    compact = "".join(ch for ch in str(value or "").lower() if ch.isalnum())
    return _CARRIER_SLUGS.get(compact, compact)


def _schema_keys(tool: dict[str, Any] | None) -> set[str]:
    props = _input_schema(tool).get("properties") or {}
    return {str(key) for key in props} if isinstance(props, dict) else set()


def _industry_params(params: dict[str, Any], tool: dict[str, Any] | None) -> dict[str, Any]:
    """Keep keys the industry schema actually declares. Drop harness routing."""
    keys = _schema_keys(tool)
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in HARNESS_ONLY_KEYS:
            continue
        if keys and key not in keys:
            continue
        if _present_nonempty(value):
            out[key] = value
    return out


def _values_equal(key: str, expected: Any, actual: Any) -> bool:
    if expected == actual:
        return True
    if expected is None or actual is None:
        return False
    if key == "location_id" or key.endswith("location_id"):
        return _same_location(expected, actual)
    if key == "carrier":
        left, right = _canon_carrier(expected), _canon_carrier(actual)
        return bool(left) and left == right
    if isinstance(expected, list) and isinstance(actual, list):
        if key == "location_ids":
            exp_ids = {_canon_location(item) for item in expected}
            act_ids = {_canon_location(item) for item in actual}
            return bool(exp_ids) and exp_ids == act_ids
        if len(expected) != len(actual):
            return False
        return all(
            _values_equal(key, left, right)
            for left, right in zip(expected, actual)
        )
    if _is_phone_key(key):
        return _digits_phone(str(expected)) == _digits_phone(str(actual))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) or isinstance(actual, (int, float)):
        try:
            return float(expected) == float(actual)
        except (TypeError, ValueError):
            return str(expected) == str(actual)
    return str(expected) == str(actual)


def _present_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _calls_match(
    expected: dict[str, Any],
    actual: dict[str, Any],
    schemas: dict[str, Any] | None,
) -> bool:
    if str(expected.get("name") or "") != str(actual.get("name") or ""):
        return False
    tool = (schemas or {}).get(str(expected.get("name") or ""))
    exp_params = {
        key: value for key, value in _params_of(expected).items()
        if _present_nonempty(value)
    }
    act_params = _industry_params(_params_of(actual), tool)
    # no expected args, or live call has no industry args (harness {to,from},
    # empty Bluejay parameters) → name-only hit. do not invent schema demands.
    if not exp_params or not act_params:
        return True
    for key, exp_value in exp_params.items():
        prop = _prop_schema(tool, key)
        if _is_prose_arg(key, prop):
            if key in act_params and not _present_nonempty(act_params.get(key)):
                return False
            continue
        if key not in act_params:
            return False
        if not _values_equal(key, exp_value, act_params.get(key)):
            return False
    exp_out = _output_fields(expected)
    act_out = _output_fields(actual)
    for field in ("ok", "error_code"):
        if field in exp_out and field in act_out and exp_out[field] != act_out[field]:
            return False
    return True


def expected_tool_names(task: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    for call in (task or {}).get("exp_tool_calls") or []:
        name = (call or {}).get("name")
        if name and name not in names:
            names.append(str(name))
    return names


def actual_tool_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per Bluejay `actual` invocation, in wall-clock order.

    Bluejay groups all actuals of one name into one `{name, actual[]}` object.
    Group array order is not chronological — sort by `start_offset_ms` when
    present so handoff subsequence scoring sees identity → specialist, not
    whichever transfer name happened to be grouped first.
    """
    calls: list[dict[str, Any]] = []
    for group in result.get("tool_calls") or []:
        if not isinstance(group, dict):
            continue
        name = group.get("name")
        actuals = group.get("actual")
        if not name or not actuals:
            continue
        if not isinstance(actuals, list):
            actuals = [actuals]
        for item in actuals:
            if not isinstance(item, dict):
                calls.append({"name": str(name), "parameters": {}})
                continue
            params = _params_of(item)
            if _params_empty(params):
                params = _params_of(group)
            call = {"name": str(name), "parameters": params}
            output = item.get("output") if isinstance(item.get("output"), dict) else {}
            if "ok" in item:
                call["ok"] = item.get("ok")
            elif "ok" in output:
                call["ok"] = output.get("ok")
            if "error_code" in item:
                call["error_code"] = item.get("error_code")
            elif "error_code" in output:
                call["error_code"] = output.get("error_code")
            if item.get("start_offset_ms") is not None:
                call["start_offset_ms"] = item.get("start_offset_ms")
            calls.append(call)
    if any(call.get("start_offset_ms") is not None for call in calls):
        calls.sort(
            key=lambda call: (
                call.get("start_offset_ms") is None,
                call.get("start_offset_ms") if call.get("start_offset_ms") is not None else 0,
            )
        )
    return calls


def actual_tool_names(result: dict[str, Any]) -> list[str]:
    return [call["name"] for call in actual_tool_calls(result)]


def tool_call_adherence(
    expected: list[Any],
    actual: list[Any],
    schemas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every expected call must match some actual call of the same name.

    Constrained args (enums, ids, facts) must equal when live args exist.
    Prose args are ignored except required fields must be non-empty.
    Empty live parameters are a name-only hit. Extra actuals are fine.
    """
    exp_calls = _as_calls(expected)
    act_calls = _as_calls(actual)
    if not exp_calls:
        return {"passed": True, "score": 1.0, "missing": [], "hit": []}
    used: set[int] = set()
    hit: list[str] = []
    missing: list[str] = []
    for exp in exp_calls:
        found = False
        for index, act in enumerate(act_calls):
            if index in used:
                continue
            if _calls_match(exp, act, schemas):
                used.add(index)
                found = True
                break
        name = str(exp.get("name") or "")
        if found:
            hit.append(name)
        else:
            missing.append(name)
    return {
        "passed": not missing,
        "score": len(hit) / len(exp_calls),
        "missing": missing,
        "hit": hit,
    }


def expected_handoff_path(task: dict[str, Any] | None) -> list[str]:
    path = (task or {}).get("exp_handoff_path") or []
    return [str(item) for item in path]


def actual_handoffs(actual_tools: list[str]) -> list[str]:
    return [name for name in actual_tools if name in HANDOFF_TOOLS]


def handoff_adherence(expected: list[str], actual: list[str]) -> dict[str, Any]:
    """Expected hops must appear in order. Empty expected fails if any transfer fired."""
    if not expected:
        exact = not actual
        return {
            "passed": exact,
            "verdict": "exact" if exact else "unexpected_handoffs",
            "score": 1.0 if exact else 0.0,
            "expected": expected,
            "actual": actual,
        }
    i = 0
    for hop in actual:
        if i < len(expected) and hop == expected[i]:
            i += 1
    matched = i / len(expected)
    if actual == expected:
        verdict = "exact"
    elif i == len(expected):
        verdict = "in_order_with_extras"
    else:
        verdict = "incomplete"
    return {
        "passed": verdict in {"exact", "in_order_with_extras"},
        "verdict": verdict,
        "score": matched,
        "expected": expected,
        "actual": actual,
    }


def _fetch_result(result_id: str) -> dict[str, Any]:
    body = verify_run._get(f"retrieve-simulation-result/{result_id}")
    return body.get("simulation_result") or body


def _digital_humans_by_sim(sim_id: str) -> dict[str, dict[str, Any]]:
    body = verify_run._get(f"digital-humans-by-simulation/{sim_id}")
    out: dict[str, dict[str, Any]] = {}
    for dh in body.get("digital_humans") or []:
        if dh.get("id") is not None:
            out[str(dh["id"])] = dh
    return out


def _actual_by_result(actuals_dir: Path | None) -> dict[str, dict[str, Any]]:
    if actuals_dir is None or not actuals_dir.exists():
        return {}
    found: dict[str, dict[str, Any]] = {}
    for item in iter_actual_finals(actuals_dir):
        found[str(item["result_id"])] = item
    return found


def _pull_actuals(run_id: str, slug: str, out_dir: Path) -> dict[str, Any]:
    return pull.pull_run(run_id, slug, out_dir)


def verify_result(
    result: dict[str, Any],
    task: dict[str, Any] | None,
    actual_state: Any | None,
    *,
    state_note: str | None = None,
    industry: str | None = None,
    schemas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if schemas is None and industry:
        schemas = load_tool_schemas(industry)
    lines = result_transcript_lines(result)
    agent_text = agent_transcript(lines)
    actual_calls = actual_tool_calls(result)
    expected_calls = list((task or {}).get("exp_tool_calls") or [])
    call = tool_call_adherence(expected_calls, actual_calls, schemas=schemas)
    actual_tools = [item["name"] for item in actual_calls]
    handoff = handoff_adherence(expected_handoff_path(task), actual_handoffs(actual_tools))

    state: dict[str, Any]
    if task is None:
        state = {"passed": False, "skipped": True, "note": "no matching task.json"}
    elif actual_state is None:
        state = {
            "passed": None,
            "skipped": True,
            "note": state_note or "no hangup dump to compare",
        }
    else:
        expected = expected_state(task)
        if expected is None:
            state = {"passed": None, "skipped": True, "note": "task has no exp_db_state"}
        else:
            matched = office_states_match(expected, actual_state)
            state = {"passed": matched, "skipped": False, "note": None}

    passed = (
        state.get("passed") is not False
        and call["passed"]
        and handoff["passed"]
    )
    return {
        "state": state,
        "call": call,
        "handoff": handoff,
        "actual_tools": actual_tools,
        "passed": passed,
        "agent_chars": len(agent_text),
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", help="Bluejay simulation run id")
    parser.add_argument("--sim", help="Use this simulation's latest run")
    parser.add_argument("--industry", required=True)
    parser.add_argument("--actuals-dir", type=Path,
                        help="Local hangup dumps (skip S3)")
    parser.add_argument("--harness", default="openai/realtime-2.1",
                        help="Used with --industry to build the S3 slug")
    parser.add_argument("--slug", help="Override the S3 / dump slug")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    run_id = args.run_id
    if args.sim:
        run_id = verify_run.latest_run_for_sim(args.sim)
    if not run_id:
        parser.error("give a run id or --sim")

    slug = args.slug or pull.pair_slug(args.harness, args.industry)
    schemas = load_tool_schemas(args.industry)
    run_body = verify_run._get(f"retrieve-simulation-results/{run_id}")
    run = run_body.get("simulation_run") or {}
    results = run_body.get("simulation_results") or run_body.get("results") or []
    sim_id = str(run.get("simulation_id") or args.sim or "")
    dh_by_id = _digital_humans_by_sim(sim_id) if sim_id else {}

    actuals_dir = args.actuals_dir
    state_skip_note = None
    pulled: dict[str, Any] | None = None
    if actuals_dir is None:
        if os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip():
            dest = ROOT / "actual-final-state"
            try:
                pulled = _pull_actuals(str(run_id), slug, dest)
                actuals_dir = dest / slug / str(pulled.get("run_id") or run_id)
            except SystemExit as e:
                state_skip_note = f"S3 pull failed: {e}"
        else:
            state_skip_note = "MIVAS_SNAPSHOT_BUCKET not set; state compare skipped"
    actual_by_result = _actual_by_result(actuals_dir)

    rows: list[dict[str, Any]] = []
    for summary in results:
        result_id = str(summary.get("id") or "")
        if not result_id:
            continue
        classified = verify_run.classify(result_id)
        detail = _fetch_result(result_id)
        dh = detail.get("digital_human") or dh_by_id.get(str(detail.get("digital_human_id"))) or {}
        case_key = case_key_from_dh(dh)
        task = load_task(args.industry, case_key) if case_key else None

        actual_state = None
        note = state_skip_note
        dump = actual_by_result.get(result_id)
        if dump and dump.get("path"):
            actual_state = json.loads(Path(dump["path"]).read_text())
            note = None
        elif dump is None and actuals_dir is not None and state_skip_note is None:
            note = "no hangup dump for this result"

        check = verify_result(
            detail, task, actual_state, state_note=note, schemas=schemas,
        )
        if classified.get("pending"):
            mark = "wait"
        elif classified.get("void_reason"):
            mark = "VOID"
        elif not task:
            mark = "MISS"
        elif check["passed"]:
            mark = "pass"
        else:
            mark = "FAIL"

        row = {
            "result_id": result_id,
            "case_key": case_key,
            "digital_human_id": detail.get("digital_human_id"),
            "status": classified.get("status"),
            "pending": classified.get("pending"),
            "void_reason": classified.get("void_reason"),
            "mark": mark,
            **check,
        }
        rows.append(row)

        extra = ""
        if row["void_reason"]:
            extra = f"  ↳ {row['void_reason']}"
        elif not task:
            extra = "  ↳ no task.json for this digital human"
        elif not check["call"]["passed"]:
            extra = f"  ↳ missing tools: {check['call']['missing']}"
        elif not check["handoff"]["passed"]:
            extra = (
                f"  ↳ handoff {check['handoff']['verdict']}: "
                f"want {check['handoff']['expected']} got {check['handoff']['actual']}"
            )
        elif check["state"].get("skipped"):
            extra = f"  ↳ state skipped: {check['state'].get('note')}"
        elif check["state"].get("passed") is False:
            extra = "  ↳ exp_db_state mismatch"
        print(f"{mark:<5} {result_id} {case_key or '?':<12} {classified.get('status') or ''}")
        if extra:
            print(extra)

    pending = [r for r in rows if r.get("pending")]
    void = [r for r in rows if r.get("void_reason")]
    failed = [r for r in rows if r["mark"] == "FAIL"]
    passed = [r for r in rows if r["mark"] == "pass"]
    skipped_state = [r for r in rows if r.get("state", {}).get("skipped")]
    print(
        f"\n{len(passed)}/{len(rows)} passed · {len(failed)} failed · "
        f"{len(void)} VOID · {len(pending)} pending · "
        f"{len(skipped_state)} state-skipped"
    )
    if args.json:
        json.dump({"run_id": run_id, "industry": args.industry, "results": rows}, sys.stdout, indent=2)
        sys.stdout.write("\n")

    if pending:
        return 2
    if failed or void:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
