"""Replay every task's solution/oracle.json on a fresh seed and judge it.

A task passes when every call without `expect_error` returns ok true, every
`expect_error` call returns exactly that code, and the replayed GET /state satisfies
`exp_db_state` under the production matcher (verify_task_run.office_states_match).
Transfers and end_call are harness-native and are skipped, as in the real run.

    uv run python verifiers/oracle_check.py                 # all industries, exit 1 on any failure
    uv run python verifiers/oracle_check.py --industry legal --task C5-E2 --verbose
    uv run python verifiers/oracle_check.py --json out.json

Each failure is classified: "recipe" when a call did not behave as the oracle says,
"expected state" when every call behaved and the state still does not match.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

VERIFIERS = Path(__file__).resolve().parent
ROOT = VERIFIERS.parent
INDUSTRY_ROOT = ROOT / "industries"
INDUSTRIES = ("customer-support", "healthcare", "legal")


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, VERIFIERS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


efs = _load("expected_final_state")
vtr = _load("verify_task_run")


def task_ids(industry: str) -> list[str]:
    return sorted(p.parent.name for p in (INDUSTRY_ROOT / industry / "tasks").glob("*/task.json"))


def replay(client: TestClient, industry: str, task_id: str, calls: list[dict[str, Any]],
           flags: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    headers = {"X-Mivas-Call-Id": f"oracle-{industry}-{task_id}"}
    log: list[dict[str, Any]] = []
    prior: dict[str, Any] | None = None
    for call in calls:
        name = call["name"]
        if efs.is_harness_native(name, flags):
            continue
        args = efs._carry(dict(call.get("arguments") or {}), prior)
        resp = client.post(f"/tools/{name}", json={"arguments": args}, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = {"ok": False, "error_code": f"HTTP_{resp.status_code}", "raw": resp.text}
        if not isinstance(body, dict):
            body = {"ok": False, "error_code": "NON_OBJECT_RESPONSE"}
        log.append({
            "name": name,
            "arguments": args,
            "ok": bool(body.get("ok")),
            "error_code": body.get("error_code"),
            "expect_error": call.get("expect_error"),
            "message": body.get("caller_safe_message"),
        })
        prior = body if resp.status_code == 200 else None
    state = client.get("/state", headers=headers).json()
    return log, state


def judge(task: dict[str, Any], industry: str, log: list[dict[str, Any]],
          state: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    for i, entry in enumerate(log, 1):
        want = entry["expect_error"]
        if want and entry["error_code"] != want:
            got = entry["error_code"] or "ok"
            reasons.append(f"recipe: call {i} {entry['name']} expected {want}, got {got}")
        elif not want and not entry["ok"]:
            reasons.append(f"recipe: call {i} {entry['name']} returned "
                           f"{entry['error_code']} ({entry.get('message') or ''})".rstrip(" ()"))
    exp = task["exp_db_state"]
    state_ok = vtr.office_states_match(exp, state, industry)
    diff: dict[str, Any] = {}
    if not state_ok:
        e = vtr.office_canonical(exp, industry)
        a = vtr.office_canonical(state, industry)
        diff = {t: {"expected": e[t], "replayed": a[t]} for t in e if e[t] != a[t]}
        kind = "expected state" if not reasons else "recipe"
        reasons.append(f"{kind}: tables differ {sorted(diff)}")
    return {"ok": not reasons, "reasons": reasons, "diff": diff}


def check_industry(industry: str, only: str | None = None) -> list[dict[str, Any]]:
    flags = efs.tool_flags(industry)
    results = []
    with efs.load_tool_server(industry) as module, TestClient(module.app) as client:
        for task_id in task_ids(industry):
            if only and task_id != only:
                continue
            task = json.loads((INDUSTRY_ROOT / industry / "tasks" / task_id / "task.json").read_text())
            path = INDUSTRY_ROOT / industry / "tasks" / task_id / "solution" / "oracle.json"
            if not path.is_file():
                results.append({"industry": industry, "task": task_id, "ok": False,
                                "reasons": ["recipe: solution/oracle.json missing"], "log": []})
                continue
            calls = json.loads(path.read_text())["calls"]
            with contextlib.redirect_stdout(io.StringIO()):
                log, state = replay(client, industry, task_id, calls, flags)
            verdict = judge(task, industry, log, state)
            results.append({"industry": industry, "task": task_id, "log": log, **verdict})
    return results


def print_report(results: list[dict[str, Any]], verbose: bool) -> None:
    print(f"{'industry':18} {'pass':>5} {'fail':>5} {'total':>5}")
    for industry in INDUSTRIES:
        rows = [r for r in results if r["industry"] == industry]
        if not rows:
            continue
        passed = sum(r["ok"] for r in rows)
        print(f"{industry:18} {passed:5} {len(rows) - passed:5} {len(rows):5}")
    passed = sum(r["ok"] for r in results)
    print(f"{'all':18} {passed:5} {len(results) - passed:5} {len(results):5}")
    for r in results:
        if r["ok"]:
            continue
        print(f"\nFAIL {r['industry']}/{r['task']}")
        for reason in r["reasons"]:
            print(f"  {reason}")
        if verbose:
            for table, d in (r.get("diff") or {}).items():
                print(f"  {table}\n    expected {json.dumps(d['expected'])[:400]}"
                      f"\n    replayed {json.dumps(d['replayed'])[:400]}")
            for i, entry in enumerate(r["log"], 1):
                flag = "ok " if entry["ok"] else f"{entry['error_code']} "
                print(f"  {i:2} {flag}{entry['name']} {json.dumps(entry['arguments'])[:160]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--industry", action="append", dest="industries", choices=INDUSTRIES)
    parser.add_argument("--task")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--json", type=Path, help="Write per-task results here")
    args = parser.parse_args(argv)
    results: list[dict[str, Any]] = []
    for industry in args.industries or INDUSTRIES:
        results.extend(check_industry(industry, args.task))
    print_report(results, args.verbose)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1) + "\n")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
