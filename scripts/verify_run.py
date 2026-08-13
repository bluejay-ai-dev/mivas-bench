"""Void, don't score, any simulation result whose tool list never landed.

The goal judge is given the transcript, the criteria and the *actual* tool calls —
never `expected_tool_calls`. So when trace→tool extraction produces nothing (an
adapter that died before its `finally` posted trace_ids, a trace whose voice.call
root was never exported), the judge grades on the transcript alone and a criterion
that requires specific tools passes vacuously. Result 719089 passed that way while
its own DB proved the booking happened; 719103 failed the same way.

A result with zero recorded tools is therefore not a pass and not a failure — it is
a measurement error, and it must not enter a score.

    uv run python scripts/verify_run.py 228854            # one run
    uv run python scripts/verify_run.py 228854 228860 ...  # several
    uv run python scripts/verify_run.py --sim 30315        # the sim's latest run

Exit code 0 = every result is scorable. 1 = at least one is void (re-run those).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
# statuses where no tool list is expected, so an empty one is not a measurement error
NO_CONVERSATION = {"NO_ANSWER", "NO_CONNECTION", "CANCELLED", "SYSTEM_ERROR", "ERROR"}
# tools are extracted when the harness POSTs trace_ids, which happens after the call
# ends — so anything short of a final status has an unfinished tool list and must not
# be scored, only waited on. CONVERSATION_ENDED in particular looks done and is not.
NOT_FINAL = {"RUNNING", "IN_PROGRESS", "QUEUED", "PENDING", "EVALUATING", "CONVERSATION_ENDED"}


def _get(path: str) -> dict:
    key = os.environ.get("BLUEJAY_API_KEY")
    if not key:
        raise SystemExit("need BLUEJAY_API_KEY")
    req = urllib.request.Request(f"{API}/{path}", headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"GET {path} → {e.code} {e.read()[:300].decode(errors='replace')}")


def result_ids_for_run(run_id: str) -> list[str]:
    body = _get(f"retrieve-simulation-results/{run_id}")
    results = body.get("simulation_results") or body.get("results") or []
    return [str(r.get("id")) for r in results if r.get("id")]


def latest_run_for_sim(sim_id: str) -> str:
    body = _get(f"get-simulation-runs/{sim_id}")
    runs = body.get("simulation_runs") or body.get("runs") or []
    if not runs:
        raise SystemExit(f"no runs for simulation {sim_id}")
    newest = max(runs, key=lambda r: str(r.get("created_at") or ""))
    return str(newest.get("id") or newest.get("simulation_run_id"))


def classify(result_id: str) -> dict:
    body = _get(f"retrieve-simulation-result/{result_id}")
    d = body.get("simulation_result") or {}
    evals = d.get("evaluations") or []
    ev = evals[0] if isinstance(evals, list) and evals else (evals if isinstance(evals, dict) else {})
    groups = d.get("tool_calls") or []
    fired = [g["name"] for g in groups if g.get("actual")]
    expected = [g["name"] for g in groups if g.get("expected")]
    status = str(d.get("status") or "")

    void_reason = None
    if status in NOT_FINAL:
        return {
            "id": result_id, "status": status, "goal": ev.get("goal_success"),
            "expected": expected, "fired": fired, "hits": [], "missing": [],
            "void_reason": None, "pending": True,
        }
    if status in NO_CONVERSATION:
        void_reason = f"no conversation ({status})"
    elif not d.get("trace_ids"):
        void_reason = "no trace linked — the harness never posted trace_ids"
    elif expected and not fired:
        void_reason = "tool list empty while tools were expected — extraction did not land"

    return {
        "id": result_id,
        "status": status,
        "goal": ev.get("goal_success"),
        "expected": expected,
        "fired": fired,
        # only expected tools that actually fired — `fired` also holds tools the case
        # never asked for, and counting those as hits flatters the result
        "hits": [g["name"] for g in groups if g.get("expected") and g.get("actual")],
        "missing": [g["name"] for g in groups if g.get("expected") and not g.get("actual")],
        "void_reason": void_reason,
        "pending": False,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("runs", nargs="*", help="simulation run ids")
    p.add_argument("--sim", help="use this simulation's latest run")
    args = p.parse_args()

    run_ids = list(args.runs)
    if args.sim:
        run_ids.append(latest_run_for_sim(args.sim))
    if not run_ids:
        p.error("give at least one run id or --sim")

    rows: list[dict] = []
    for run_id in run_ids:
        for rid in result_ids_for_run(run_id):
            rows.append(classify(rid))

    pending = [r for r in rows if r.get("pending")]
    void = [r for r in rows if r["void_reason"]]
    scorable = [r for r in rows if not r["void_reason"] and not r.get("pending")]
    passed = [r for r in scorable if r["goal"] is True]

    for r in rows:
        if r.get("pending"):
            mark = "wait"
        elif r["void_reason"]:
            mark = "VOID"
        else:
            mark = "pass" if r["goal"] is True else "fail"
        print(f"{mark:<5} {r['id']} {r['status']:<18} tools {len(r['hits'])}/{len(r['expected'])}")
        if r.get("pending"):
            print("      ↳ not final yet — tool list still being extracted")
        elif r["void_reason"]:
            print(f"      ↳ {r['void_reason']}")
        elif r["missing"]:
            print(f"      ↳ missing: {', '.join(r['missing'])}")

    total = len(rows)
    print(f"\n{len(scorable)}/{total} scorable · {len(passed)} passed · "
          f"{len(void)} VOID · {len(pending)} still running")
    if pending:
        print("wait for the pending results before scoring this run")
        return 2
    if void:
        print("re-run the void results before reading any score off this run:")
        print("  " + " ".join(r["id"] for r in void))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
