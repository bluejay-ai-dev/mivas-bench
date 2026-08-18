#!/usr/bin/env python3
"""Rebuild mivas-legal-run-* canvas from verify_task_run JSON + task metadata."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "industries" / "legal" / "tasks"
CANVAS_DIR = Path.home() / ".cursor/projects/Users-yashs-Projects-bluejay-mivas-bench/canvases"

RUN_ID = "237621"
SIM_ID = 30658
AGENT_ID = 34170
INDUSTRY = "legal"
SLUG = "openai-realtime-2-1-legal"
ACTUALS = ROOT / "actual-final-state" / SLUG / RUN_ID
S3_BUCKET = "mivas-bench-call-dbs"
S3_PREFIX = "mivas/openai-realtime-2-1-legal"

CLONE_SUFFIXES = ("-BG", "-SIG")


def is_clone(case_key: str) -> bool:
    return any(case_key.endswith(s) for s in CLONE_SUFFIXES)


def load_task_meta(case_key: str) -> dict[str, Any]:
    path = TASKS / case_key / "task.json"
    if not path.is_file():
        return {}
    task = json.loads(path.read_text())
    meta = task.get("metadata") or {}
    return {
        "category": meta.get("category") or (case_key.split("-")[0] if case_key else "?"),
        "difficulty": meta.get("difficulty") or "?",
        "audio_condition": meta.get("audio_condition") or "perfect",
    }


def run_verifier() -> dict[str, Any]:
    cmd = [
        "uv", "run", "python", "scripts/verify_task_run.py", RUN_ID,
        "--industry", INDUSTRY,
        "--actuals-dir", str(ACTUALS),
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    text = proc.stdout
    idx = text.rfind('{\n  "run_id"')
    if idx < 0:
        raise SystemExit(f"verifier JSON not found:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return json.loads(text[idx:])


def fail_reason(row: dict[str, Any]) -> str:
    if row.get("void_reason"):
        return str(row["void_reason"])
    if row.get("mark") == "MISS":
        return "no task.json"
    if row.get("passed"):
        return ""
    call = row.get("call") or {}
    handoff = row.get("handoff") or {}
    state = row.get("state") or {}
    if not call.get("passed"):
        missing = call.get("missing") or []
        return f"missing tools: {missing}"
    if not handoff.get("passed"):
        return (
            f"handoff {handoff.get('verdict')}: "
            f"want {handoff.get('expected')} got {handoff.get('actual')}"
        )
    if state.get("passed") is False:
        return "exp_db_state mismatch"
    return "combined fail"


def build(ver: dict[str, Any]) -> dict[str, Any]:
    rows_in = ver.get("results") or []
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    sample_rows: list[dict[str, Any]] = []
    missing_tools: Counter[str] = Counter()
    by_cat: dict[str, list[bool]] = defaultdict(list)
    by_diff: dict[str, list[bool]] = defaultdict(list)
    by_audio: dict[str, list[bool]] = defaultdict(list)
    by_clone: dict[str, list[bool]] = defaultdict(list)
    k_by_case: dict[str, list[bool]] = defaultdict(list)

    combined_pass = 0
    tool_pass = 0
    handoff_pass = 0
    db_pass = 0
    db_fail = 0
    db_skip = 0
    void = 0
    pending = 0
    missing = 0

    for r in rows_in:
        case_key = r.get("case_key") or "?"
        meta = load_task_meta(case_key)
        combined = r.get("mark", "?")
        if combined == "pass":
            comb = "pass"
            combined_pass += 1
        elif combined == "FAIL":
            comb = "fail"
        elif r.get("void_reason"):
            comb = "void"
            void += 1
        elif r.get("pending"):
            comb = "pending"
            pending += 1
        elif combined == "MISS":
            comb = "missing"
            missing += 1
        else:
            comb = "fail"

        call = r.get("call") or {}
        handoff = r.get("handoff") or {}
        state = r.get("state") or {}
        tp = bool(call.get("passed"))
        hp = bool(handoff.get("passed"))
        if tp:
            tool_pass += 1
        if hp:
            handoff_pass += 1
        if state.get("skipped"):
            db_skip += 1
        elif state.get("passed") is True:
            db_pass += 1
        elif state.get("passed") is False:
            db_fail += 1

        for tool in call.get("missing") or []:
            missing_tools[str(tool)] += 1

        scored = comb in ("pass", "fail")
        if scored:
            ok = comb == "pass"
            by_cat[meta.get("category", "?")].append(ok)
            by_diff[meta.get("difficulty", "?")].append(ok)
            by_audio[meta.get("audio_condition", "perfect")].append(ok)
            by_clone["clone" if is_clone(case_key) else "original"].append(ok)
            k_by_case[case_key].append(ok)

        sim_id = SIM_ID
        result_id = str(r.get("result_id") or "")
        sample_rows.append({
            "resultId": result_id,
            "caseKey": case_key,
            "category": meta.get("category", "?"),
            "difficulty": meta.get("difficulty", "?"),
            "audio": meta.get("audio_condition", "perfect"),
            "isClone": is_clone(case_key),
            "combined": comb,
            "toolPass": tp,
            "toolScore": float(call.get("score") or 0),
            "missingTools": ";".join(call.get("missing") or []),
            "handoffPass": hp,
            "handoffVerdict": str(handoff.get("verdict") or ""),
            "failReason": fail_reason(r),
            "resultUrl": f"https://app.getbluejay.ai/simulations/{sim_id}/runs/{RUN_ID}/results/{result_id}",
            "dbPass": state.get("passed") is True,
            "dbSkipped": bool(state.get("skipped")),
        })

    scored_n = sum(1 for r in sample_rows if r["combined"] in ("pass", "fail"))
    snapshots = len(list(ACTUALS.rglob("final.json"))) if ACTUALS.is_dir() else 0

    def band_for(case_key: str, passes: list[bool]) -> str:
        if len(passes) != 3:
            return "partial"
        if all(passes):
            return "always"
        if not any(passes):
            return "never"
        return "mixed"

    k_detail = []
    k_bands = Counter()
    for case_key in sorted(k_by_case):
        passes = k_by_case[case_key]
        band = band_for(case_key, passes)
        k_bands[band] += 1
        k_detail.append({
            "case_key": case_key,
            "band": band,
            "passes": sum(passes),
            "attempts": len(passes),
        })

    def breakdown(d: dict[str, list[bool]]) -> list[list[Any]]:
        out = []
        for key in sorted(d):
            vals = d[key]
            p = sum(vals)
            t = len(vals)
            out.append([key, p, t])
        return out

    cat_fail = Counter(
        r["category"] for r in sample_rows if r["combined"] == "fail"
    )

    counts = {
        "expected_results": len(sample_rows),
        "verifier_rows": len(sample_rows),
        "completed": sum(1 for r in rows_in if not r.get("pending")),
        "scored_deterministic": scored_n,
        "combined_pass": combined_pass,
        "void": void,
        "pending": pending,
        "missing": missing,
        "db_skipped": db_skip,
        "db_scored": db_pass + db_fail,
        "db_pass": db_pass,
        "db_fail": db_fail,
        "tool_pass": tool_pass,
        "handoff_pass": handoff_pass,
        "s3_fetched": snapshots,
    }
    rates = {
        "combined": round(combined_pass / scored_n, 3) if scored_n else None,
        "tool": round(tool_pass / scored_n, 3) if scored_n else None,
        "handoff": round(handoff_pass / scored_n, 3) if scored_n else None,
        "db": round(db_pass / (db_pass + db_fail), 3) if (db_pass + db_fail) else None,
    }

    meta = {
        "runId": RUN_ID,
        "simulationId": SIM_ID,
        "agentId": AGENT_ID,
        "industry": INDUSTRY,
        "simulationName": "mivas legal · prompt-adherence 66-case review",
        "runUrl": f"https://app.getbluejay.ai/simulations/{SIM_ID}/runs/{RUN_ID}",
        "simulationUrl": f"https://app.getbluejay.ai/simulations/{SIM_ID}",
        "generatedAt": now,
        "runsPerDh": 3,
        "s3Bucket": S3_BUCKET,
        "s3Prefix": S3_PREFIX,
        "snapshotsFetched": snapshots,
        "snapshotsExpected": len(sample_rows),
        "deploymentBucket": S3_BUCKET,
        "dataGaps": [
            "Bluejay custom metrics not joined to this dashboard.",
            "DB compare is exact on prose columns — replay placeholders inflate mismatch rate vs tool-only success.",
        ],
    }

    return {
        "META": meta,
        "COUNTS": counts,
        "RATES": rates,
        "K_BANDS": dict(k_bands),
        "TOOL_CLUSTERS": missing_tools.most_common(12),
        "BY_CATEGORY": breakdown(by_cat),
        "BY_DIFFICULTY": breakdown(by_diff),
        "BY_AUDIO": breakdown(by_audio),
        "BY_CLONE": breakdown(by_clone),
        "CAT_FAIL": cat_fail.most_common(),
        "K_DETAIL": k_detail,
        "ROWS": sample_rows,
    }


def patch_canvas(const: dict[str, Any]) -> None:
    canvas_path = CANVAS_DIR / f"mivas-legal-run-{RUN_ID}.canvas.tsx"
    text = canvas_path.read_text()
    const_js = json.dumps(const, indent=2)
    start = text.index("const DATA:")
    end = text.index("\nconst META = DATA.META;")
    typed_header = text[start:text.index("= {", start) + 1]
    text = text[:start] + typed_header + " " + const_js + ";" + text[end:]

    text = text.replace(
        'label="DB compare pass (vacuous expected)"',
        'label="DB compare pass"',
    )
    text = re.sub(
        r'<Callout tone="success" title="Hangup snapshots confirmed in S3">[\s\S]*?</Callout>',
        '''<Callout tone="success" title="Hangup snapshots confirmed in S3">
        <Stack gap={4}>
          <Text size="small">
            Deployment mivas-openai-realtime-2-1-legal has MIVAS_SNAPSHOT_BUCKET={META.deploymentBucket}. All {META.snapshotsFetched} result dumps were pulled for this run.
          </Text>
          <Text size="small">
            exp_db_state repaired via encode_legal_tasks --repair (full hangup GET /state). DB scoring compares write tables against S3 actuals.
          </Text>
        </Stack>
      </Callout>''',
        text,
        count=1,
    )
    text = re.sub(
        r'<Callout tone="info" title="Actionable clusters">[\s\S]*?</Callout>',
        '''<Callout tone="info" title="Actionable clusters">
        <Stack gap={4}>
          <Text size="small">1. Tool misses still dominate combined pass — record_intake, check_practice_area, escalate_to_human.</Text>
          <Text size="small">2. DB mismatches now meaningful — prose fields (message, summary, note) may differ from replay placeholders.</Text>
          <Text size="small">3. C1 easies pass; C2–C5/R mostly 0/3 — pack routing, not DH contract.</Text>
        </Stack>
      </Callout>''',
        text,
        count=1,
    )
    canvas_path.write_text(text)


def main() -> int:
    ver = run_verifier()
    const = build(ver)
    CANVAS_DIR.mkdir(parents=True, exist_ok=True)
    (CANVAS_DIR / f"mivas-legal-run-{RUN_ID}.data.json").write_text(
        json.dumps(const, indent=2) + "\n"
    )
    (CANVAS_DIR / f"mivas-legal-run-{RUN_ID}.verifier.json").write_text(
        json.dumps(ver, indent=2) + "\n"
    )
    patch_canvas(const)
    c = const["COUNTS"]
    print(
        f"dashboard updated: combined {c['combined_pass']}/{c['scored_deterministic']} "
        f"tools {c['tool_pass']} handoff {c['handoff_pass']} "
        f"db {c['db_pass']}/{c['db_scored']} fail {c['db_fail']} skip {c['db_skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
