"""Rebuild the dashboard's `const D = {...}` blob from a run CSV.

The dashboard is a hand-written page whose entire dataset lives on one line
(`const D = ...`), with the run id in the <title> and the subtitle. Regenerating it
by hand was where the "bad escape \\u" mistake came from, so the swap is scripted:

    uv run python scripts/dashboard_data.py 229542 <dashboard.html>

Rewrites the file in place. Idempotent — run it again with a different run id to repoint.
"""

from __future__ import annotations

import collections
import csv
import json
import re
import sys

AREA_NAMES = {
    # healthcare: the case key carries the area (A1-01 -> A1)
    "A1": "New-patient access",
    "A2": "Appointment management",
    "A3": "Coverage and benefits",
    "A4": "Cosmetic concierge",
    "A5": "Billing and payments",
    "A6": "Clinical and escalation",
    # finance: keys are F01..F60, so the area comes off the dh_tags tag instead
    "area_1_public_information": "Public information and membership",
    "area_2_identity_control": "Identity and authorization",
    "area_3_accounts_and_fees": "Balances, activity and fees",
    "area_4_money_movement": "Money movement",
    "area_5_card_lifecycle": "Card lifecycle",
    "area_6_disputes": "Disputes and claims",
    "area_7_leaves_the_ai": "Calls that must leave the AI",
}


def area_key(row: dict) -> str:
    """Healthcare encodes the area in the case key; finance encodes it in dh_tags."""
    tags = row.get("dh_tags") or ""
    m = re.search(r"area_\d+_[a-z_]+", tags)
    if m:
        return m.group(0)
    return (row.get("case") or "")[:2]


def _f(row: dict, key: str):
    """CSV cells are strings; empty means the metric never landed, which is not 0."""
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _yes(row: dict, key: str) -> bool:
    # the exporter writes booleans as "True"/"False" but `connected` as 1/0
    return str(row.get(key, "")).strip().lower() in ("true", "1")


def _tc(row: dict):
    try:
        return int(float(row["custom_task_completion_1_5"]))
    except (KeyError, TypeError, ValueError):
        return None


def build(csv_path: str) -> dict:
    rows = list(csv.DictReader(open(csv_path)))
    if not rows:
        raise SystemExit(f"{csv_path} has no rows")

    out_rows = []
    for r in rows:
        miss = [t for t in (r["tools_missing"] or "").split(";") if t]
        out_rows.append({
            "case": r["case"], "area": area_key(r), "pass": _yes(r, "goal_success"),
            "status": r["status"], "dur": _f(r, "builtin_duration") or _f(r, "duration"),
            "turns": _f(r, "builtin_num_turns"), "lat": _f(r, "builtin_avg_agent_latency"),
            "clarity": _f(r, "builtin_agent_audio_clarity"),
            "barge": _f(r, "builtin_agent_interruption_count"),
            "adh": _f(r, "tool_call_adherence"), "miss": miss,
            "hoV": r["handoff_adherence_verdict"], "hoS": _f(r, "handoff_adherence_score"),
            "tc": _tc(r), "pe": _yes(r, "custom_premature_call_end"),
            "tcWhy": (r.get("custom_task_completion_1_5_reasoning") or "")[:400],
            "why": (r.get("goal_reasoning") or "")[:400],
        })

    passed = sum(1 for r in out_rows if r["pass"])
    miss_counts = collections.Counter(t for r in out_rows for t in r["miss"])

    areas = []
    for key, name in AREA_NAMES.items():
        sub = [r for r in out_rows if r["area"] == key]
        if not sub:
            continue
        areas.append({
            "key": key, "name": name,
            "pass": sum(1 for r in sub if r["pass"]),
            "fail": sum(1 for r in sub if not r["pass"]),
            "adh": _mean(r["adh"] for r in sub),
            "tc": _mean(r["tc"] for r in sub),
        })

    return {
        "kpi": {
            "n": len(out_rows), "passed": passed,
            "conn": sum(1 for r in rows if _yes(r, "connected")),
            "dur": _mean(r["dur"] for r in out_rows),
            "lat": _mean(r["lat"] for r in out_rows),
            "adh": _mean(r["adh"] for r in out_rows),
            "ho": _mean(r["hoS"] for r in out_rows),
            "hoExact": sum(1 for r in out_rows if r["hoV"] == "exact"),
            "barge": sum(r["barge"] or 0 for r in out_rows),
            "clarity": _mean(r["clarity"] for r in out_rows),
            "turns": _mean(r["turns"] for r in out_rows),
            "tc": _mean(r["tc"] for r in out_rows),
            "pe": sum(1 for r in out_rows if r["pe"]),
            "tcDist": {str(k): v for k, v in sorted(
                collections.Counter(r["tc"] for r in out_rows if r["tc"]).items())},
        },
        "areas": areas,
        "rows": out_rows,
        "miss": miss_counts.most_common(10),
    }


def main() -> int:
    run_id, page = sys.argv[1], sys.argv[2]
    data = build(f"docs/healthcare/run_{run_id}.csv")
    html = open(page).read()
    # a lambda replacement, not a string: a JSON blob is full of backslashes and
    # re.sub reads "\u..." in the *replacement* as a bad escape.
    blob = json.dumps(data, separators=(",", ":"))
    html, n = re.subn(r"^const D = .*$", lambda _: f"const D = {blob};",
                      html, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("could not find the `const D = ...` line")
    html = re.sub(r"run 229\d{3}", f"run {run_id}", html)
    html = re.sub(r"/runs/229\d{3}", f"/runs/{run_id}", html)
    open(page, "w").write(html)
    k = data["kpi"]
    print(f"dashboard -> run {run_id}: {k['passed']}/{k['n']} passed, "
          f"conn {k['conn']}, adh {k['adh']:.0%}, tc {k['tc']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
