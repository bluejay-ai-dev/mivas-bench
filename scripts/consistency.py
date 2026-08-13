"""Per-case pass consistency across k samples — the attribution basis.

Verdicts churn ~38% between single runs of this suite (D11), so a case that failed once
tells you nothing: six cases registered as MODEL defects in run 228930 passed in 228986
with no fix aimed at them. What matters is how often a case fails across k samples:

    3/3 fail  → hard defect, attribute it
    2/3 fail  → real but intermittent, attribute it
    1/3 fail  → noise, do not register a defect
    0/3 fail  → pass

SYSTEM_ERROR samples are void (the platform dropped the call at dispatch — seen when
max_concurrent was raised to 60) and are excluded from both numerator and denominator, so
a case's k is what actually ran, not what was requested.

    uv run python scripts/consistency.py 229001              # one run
    uv run python scripts/consistency.py 229001 228986 228930  # pool several
    uv run python scripts/consistency.py 229001 --attribute  # list cases worth triaging
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import urllib.request

API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
TERMINAL = {"COMPLETED", "FAILED", "NO_ANSWER", "NO_CONNECTION", "CANCELLED",
            "SYSTEM_ERROR", "ERROR"}
VOID_STATUS = {"SYSTEM_ERROR", "NO_CONNECTION", "CANCELLED", "ERROR"}
# how long after end_time an empty tool list stops being "not extracted yet" and starts
# being a real measurement failure. Observed lag: under a minute.
SETTLE_SECONDS = float(os.environ.get("MIVAS_TOOL_SETTLE_SECONDS", "180"))


def _get(path: str) -> dict:
    key = os.environ.get("BLUEJAY_API_KEY")
    if not key:
        raise SystemExit("need BLUEJAY_API_KEY")
    req = urllib.request.Request(f"{API}/{path}", headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("runs", nargs="+")
    p.add_argument("--sim", default="30315")
    p.add_argument("--attribute", action="store_true",
                   help="print only the cases worth triaging (>=2 of k failing)")
    args = p.parse_args()

    dhs = {d["id"]: (d.get("name") or "?")
           for d in _get(f"digital-humans-by-simulation/{args.sim}").get("digital_humans", [])}

    samples: dict[str, list[dict]] = collections.defaultdict(list)
    void = pending = 0
    for run in args.runs:
        # the bulk endpoint under-reports tool_calls and metrics[] for the tail of a large
        # run (D14) — it made 49 of 180 samples look tool-less. Enumerate with it, then read
        # each result individually, which is correct.
        listing = _get(f"retrieve-simulation-results/{run}").get("simulation_results", [])
        for stub in listing:
            rid = stub.get("id")
            r = (_get(f"retrieve-simulation-result/{rid}").get("simulation_result") or stub) if rid else stub
            status = str(r.get("status"))
            if status not in TERMINAL:
                pending += 1
                continue
            if status in VOID_STATUS:
                void += 1
                continue
            groups = r.get("tool_calls") or []
            # A sample whose tool list never landed is a measurement failure, not a
            # verdict: the judge grades the transcript alone and can pass a case that
            # skipped every required tool (run 229001 lost 28 samples to a span-queue
            # overflow at concurrency 60).
            #
            # But an empty tool list ALSO means "read too early" — extraction lands a
            # minute or two after the status flips, so 14 samples that looked lost in
            # 229022 all had 2-9 tools when re-polled 45 s later. Only void it when the
            # result has had time to settle; otherwise treat it as still pending.
            if any(g.get("expected") for g in groups) and not any(g.get("actual") for g in groups):
                end = str(r.get("end_time") or "")
                settled = False
                if end:
                    try:
                        from datetime import datetime, timezone
                        t = datetime.fromisoformat(end.replace("Z", "+00:00"))
                        settled = (datetime.now(timezone.utc) - t).total_seconds() > SETTLE_SECONDS
                    except ValueError:
                        settled = True
                if settled:
                    void += 1
                else:
                    pending += 1
                continue
            evs = r.get("evaluations") or []
            ev = evs[0] if isinstance(evs, list) and evs else {}
            case = (dhs.get(r.get("digital_human_id")) or "?").split()[0]
            samples[case].append({
                "result_id": r["id"],
                "goal": ev.get("goal_success"),
                "missing": [g["name"] for g in groups if g.get("expected") and not g.get("actual")],
                "duration": r.get("duration"),
                "judge": str(ev.get("goal_reasoning") or "")[:200],
            })

    rows = []
    for case, ss in samples.items():
        k = len(ss)
        fails = [s for s in ss if s["goal"] is not True]
        rows.append({"case": case, "k": k, "n_fail": len(fails),
                     "rate": len(fails) / k if k else 0.0,
                     "worst": max(fails, key=lambda s: len(s["missing"]), default=None)})

    hard = [r for r in rows if r["k"] >= 2 and r["n_fail"] == r["k"]]
    flaky = [r for r in rows if r["k"] >= 2 and 0 < r["n_fail"] < r["k"]]
    clean = [r for r in rows if r["n_fail"] == 0]
    thin = [r for r in rows if r["k"] < 2]

    if not args.attribute:
        print(f"cases {len(rows)} · void samples {void} · still running {pending}")
        print(f"  always fail  {len(hard)}")
        print(f"  sometimes    {len(flaky)}")
        print(f"  always pass  {len(clean)}")
        if thin:
            print(f"  too few samples (k<2): {len(thin)} → {' '.join(sorted(r['case'] for r in thin))}")
        total_k = sum(r["k"] for r in rows)
        total_pass = sum(r["k"] - r["n_fail"] for r in rows)
        print(f"\nsample-level pass rate: {total_pass}/{total_k} = {total_pass/total_k*100:.0f}%")
        print(f"pass^k (cases passing every sample): {len(clean)}/{len(rows)} = "
              f"{len(clean)/len(rows)*100:.0f}%")

    print("\nALWAYS FAILS (attribute these first):")
    for r in sorted(hard, key=lambda r: r["case"]):
        miss = ", ".join(r["worst"]["missing"]) if r["worst"] else ""
        print(f"  {r['case']:<6} {r['n_fail']}/{r['k']}  missing: {miss[:70] or '(none — transcript)'}")
    print("\nSOMETIMES FAILS (attribute; expect intermittent model behaviour):")
    for r in sorted(flaky, key=lambda r: (-r["rate"], r["case"])):
        miss = ", ".join(r["worst"]["missing"]) if r["worst"] else ""
        print(f"  {r['case']:<6} {r['n_fail']}/{r['k']}  missing: {miss[:70] or '(none — transcript)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
