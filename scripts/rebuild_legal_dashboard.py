#!/usr/bin/env python3
"""Rebuild mivas-legal-run-* canvas from verify_task_run JSON + task metadata."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "industries" / "legal" / "tasks"
CANVAS_DIR = Path.home() / ".cursor/projects/Users-yashs-Projects-bluejay-mivas-bench/canvases"

DEFAULT_RUN_ID = "237621"
SIM_ID = 30658
AGENT_ID = 34170
INDUSTRY = "legal"
SLUG = "openai-realtime-2-1-legal"
S3_BUCKET = "mivas-bench-call-dbs"
S3_PREFIX = "mivas/openai-realtime-2-1-legal"
LATEST_CANVAS = "mivas-legal-latest"

CLONE_SUFFIXES = ("-BG", "-SIG")
DIFFICULTY_ORDER = ("easy", "medium", "hard")
BAND_ORDER = ("always", "mixed", "never", "partial")


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


def parse_verifier_payload(text: str) -> dict[str, Any]:
    idx = text.rfind('{\n  "run_id"')
    if idx >= 0:
        return json.loads(text[idx:])
    data = json.loads(text)
    if isinstance(data, dict) and "results" in data:
        return data
    raise ValueError("verifier JSON not found")


def _run_verifier_once(run_id: str, actuals: Path) -> dict[str, Any]:
    cmd = [
        "uv", "run", "python", "scripts/verify_task_run.py", run_id,
        "--industry", INDUSTRY,
        "--actuals-dir", str(actuals),
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    try:
        return parse_verifier_payload(proc.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"verifier JSON not found for {run_id}: {exc}\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        ) from exc


def run_verifier(
    run_id: str,
    actuals: Path,
    *,
    extra_run_ids: list[str] | None = None,
    extra_actuals: list[Path] | None = None,
) -> dict[str, Any]:
    ver = _run_verifier_once(run_id, actuals)
    extras = list(extra_run_ids or [])
    extra_dirs = list(extra_actuals or [])
    if len(extras) != len(extra_dirs):
        raise SystemExit("extra_run_ids and extra_actuals must match length")
    for extra_id, extra_dir in zip(extras, extra_dirs, strict=True):
        extra = _run_verifier_once(extra_id, extra_dir)
        ver["results"] = (ver.get("results") or []) + (extra.get("results") or [])
    if extras:
        ver["run_id"] = run_id
        ver["supplemental_run_ids"] = extras
    return ver


def actuals_dir(run_id: str) -> Path:
    return ROOT / "actual-final-state" / SLUG / run_id


def discover_run_ids() -> list[str]:
    root = ROOT / "actual-final-state" / SLUG
    if not root.is_dir():
        return []
    ids = [p.name for p in root.iterdir() if p.is_dir() and p.name.isdigit()]
    return sorted(ids, key=int)


def cache_path(run_id: str) -> Path:
    return CANVAS_DIR / f"mivas-legal-run-{run_id}.verifier.json"


def raw_tmp_path(run_id: str) -> Path:
    return Path(f"/tmp/legal-{run_id}-verify.raw.json")


def _try_load_verifier_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size <= 80:
        return None
    try:
        payload = parse_verifier_payload(path.read_text())
    except (ValueError, json.JSONDecodeError):
        return None
    if not payload.get("results"):
        return None
    return payload


def load_or_verify_run(run_id: str, *, refresh: bool) -> dict[str, Any]:
    cached = cache_path(run_id)
    raw = raw_tmp_path(run_id)
    if not refresh:
        candidates = [p for p in (cached, raw) if p.is_file()]
        candidates.sort(key=lambda p: (p != cached, -p.stat().st_mtime))
        for path in candidates:
            payload = _try_load_verifier_file(path)
            if payload:
                payload["run_id"] = run_id
                return payload
    actuals = actuals_dir(run_id)
    if not actuals.is_dir():
        raise SystemExit(f"missing hangup dumps: {actuals}")
    payload = _run_verifier_once(run_id, actuals)
    payload["run_id"] = run_id
    CANVAS_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def supersede_by_case(runs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Keep every attempt for a case from the newest run that includes that case."""
    by_case: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for run_id, ver in runs:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in ver.get("results") or []:
            case_key = str(row.get("case_key") or "?")
            stamped = dict(row)
            stamped["source_run_id"] = run_id
            grouped[case_key].append(stamped)
        for case_key, rows in grouped.items():
            by_case[case_key] = (run_id, rows)

    merged: list[dict[str, Any]] = []
    cases_by_run: Counter[str] = Counter()
    for case_key in sorted(by_case):
        run_id, rows = by_case[case_key]
        cases_by_run[run_id] += 1
        merged.extend(rows)

    contributing = [rid for rid, _ in runs if cases_by_run[rid]]
    return {
        "run_id": "latest",
        "industry": INDUSTRY,
        "results": merged,
        "contributing_run_ids": contributing,
        "latest_run_id": contributing[-1] if contributing else "",
        "cases_by_run": dict(cases_by_run),
    }


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


def build(
    ver: dict[str, Any],
    *,
    run_id: str,
    actuals: Path,
    extra_actuals: list[Path] | None = None,
    aggregate: bool = False,
) -> dict[str, Any]:
    rows_in = ver.get("results") or []
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    sample_rows: list[dict[str, Any]] = []
    missing_tools: Counter[str] = Counter()
    by_cat: dict[str, list[bool]] = defaultdict(list)
    by_diff: dict[str, list[bool]] = defaultdict(list)
    by_audio: dict[str, list[bool]] = defaultdict(list)
    by_clone: dict[str, list[bool]] = defaultdict(list)
    k_by_case: dict[str, list[bool]] = defaultdict(list)
    case_source: dict[str, str] = {}

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
        src_run = str(r.get("source_run_id") or run_id)
        case_source[case_key] = src_run
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

        result_id = str(r.get("result_id") or "")
        sample_rows.append({
            "resultId": result_id,
            "sourceRunId": src_run,
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
            "resultUrl": (
                f"https://app.getbluejay.ai/simulations/{SIM_ID}/runs/{src_run}?result={result_id}"
            ),
            "dbPass": state.get("passed") is True,
            "dbSkipped": bool(state.get("skipped")),
        })

    scored_n = sum(1 for r in sample_rows if r["combined"] in ("pass", "fail"))
    result_ids = {r["resultId"] for r in sample_rows if r["resultId"]}
    snapshot_dirs = [actuals, *(extra_actuals or [])]
    if aggregate:
        used_runs = sorted({r["sourceRunId"] for r in sample_rows}, key=int)
        snapshot_dirs = [actuals_dir(rid) for rid in used_runs]
    snapshots = 0
    for dump_dir in snapshot_dirs:
        if not dump_dir.is_dir():
            continue
        for final in dump_dir.rglob("final.json"):
            if final.parent.name in result_ids:
                snapshots += 1

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
            "source_run_id": case_source.get(case_key, run_id),
        })

    def breakdown(d: dict[str, list[bool]]) -> list[list[Any]]:
        out = []
        for key in sorted(d):
            vals = d[key]
            p = sum(vals)
            t = len(vals)
            out.append([key, p, t])
        return out

    def breakdown_detail(d: dict[str, list[bool]]) -> list[dict[str, Any]]:
        order = {name: i for i, name in enumerate(DIFFICULTY_ORDER)}
        out: list[dict[str, Any]] = []
        for key in sorted(d, key=lambda k: (order.get(k, 99), k)):
            vals = d[key]
            passed = sum(vals)
            total = len(vals)
            failed = total - passed
            out.append({
                "key": key,
                "pass": passed,
                "fail": failed,
                "total": total,
                "pass_rate": round(passed / total, 3) if total else None,
                "fail_rate": round(failed / total, 3) if total else None,
            })
        return out

    k_bands_by_diff: dict[str, Counter[str]] = defaultdict(Counter)
    difficulty_by_band: dict[str, Counter[str]] = defaultdict(Counter)
    for row in k_detail:
        diff = load_task_meta(row["case_key"]).get("difficulty", "?")
        k_bands_by_diff[str(diff)][row["band"]] += 1
        difficulty_by_band[row["band"]][str(diff)] += 1

    cat_fail = Counter(
        r["category"] for r in sample_rows if r["combined"] == "fail"
    )

    run_stats: dict[str, dict[str, int]] = defaultdict(lambda: {
        "cases": 0, "attempts": 0, "pass": 0, "scored": 0,
    })
    for case_key, src in case_source.items():
        run_stats[src]["cases"] += 1
    for r in sample_rows:
        src = r["sourceRunId"]
        run_stats[src]["attempts"] += 1
        if r["combined"] in ("pass", "fail"):
            run_stats[src]["scored"] += 1
            if r["combined"] == "pass":
                run_stats[src]["pass"] += 1
    cases_by_run = [
        {
            "runId": rid,
            "cases": run_stats[rid]["cases"],
            "attempts": run_stats[rid]["attempts"],
            "pass": run_stats[rid]["pass"],
            "scored": run_stats[rid]["scored"],
        }
        for rid in sorted(run_stats, key=int)
    ]
    contributing = [row["runId"] for row in cases_by_run]
    latest_run_id = contributing[-1] if contributing else run_id

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
        "unique_cases": len(case_source),
    }
    rates = {
        "combined": round(combined_pass / scored_n, 3) if scored_n else None,
        "tool": round(tool_pass / scored_n, 3) if scored_n else None,
        "handoff": round(handoff_pass / scored_n, 3) if scored_n else None,
        "db": round(db_pass / (db_pass + db_fail), 3) if (db_pass + db_fail) else None,
    }

    if aggregate:
        sim_name = "mivas legal · latest aggregate (newer run supersedes per case)"
        run_url = f"https://app.getbluejay.ai/simulations/{SIM_ID}/runs/{latest_run_id}"
        data_gaps = [
            "For each case, k=3 attempts come from the newest run that includes that case.",
            "Bluejay custom metrics not joined to this dashboard.",
            "DB compare is exact on prose columns — replay placeholders inflate mismatch rate vs tool-only success.",
        ]
    else:
        sim_name = "mivas legal · prompt-adherence 66-case review"
        run_url = f"https://app.getbluejay.ai/simulations/{SIM_ID}/runs/{run_id}"
        data_gaps = [
            "Bluejay custom metrics not joined to this dashboard.",
            "DB compare is exact on prose columns — replay placeholders inflate mismatch rate vs tool-only success.",
        ]

    meta = {
        "runId": latest_run_id if aggregate else run_id,
        "simulationId": SIM_ID,
        "agentId": AGENT_ID,
        "industry": INDUSTRY,
        "simulationName": sim_name,
        "runUrl": run_url,
        "simulationUrl": f"https://app.getbluejay.ai/simulations/{SIM_ID}",
        "generatedAt": now,
        "runsPerDh": 3,
        "s3Bucket": S3_BUCKET,
        "s3Prefix": S3_PREFIX,
        "snapshotsFetched": snapshots,
        "snapshotsExpected": len(sample_rows),
        "deploymentBucket": S3_BUCKET,
        "dataGaps": data_gaps,
        "contributingRuns": contributing,
        "latestRunId": latest_run_id,
        "aggregate": aggregate,
    }

    return {
        "META": meta,
        "COUNTS": counts,
        "RATES": rates,
        "K_BANDS": dict(k_bands),
        "TOOL_CLUSTERS": missing_tools.most_common(12),
        "BY_CATEGORY": breakdown(by_cat),
        "BY_DIFFICULTY": breakdown(by_diff),
        "BY_DIFFICULTY_DETAIL": breakdown_detail(by_diff),
        "K_BANDS_BY_DIFFICULTY": {
            diff: dict(sorted(bands.items()))
            for diff, bands in sorted(
                k_bands_by_diff.items(),
                key=lambda item: (
                    DIFFICULTY_ORDER.index(item[0])
                    if item[0] in DIFFICULTY_ORDER
                    else 99,
                    item[0],
                ),
            )
        },
        "DIFFICULTY_BY_BAND": {
            band: {
                diff: difficulty_by_band[band][diff]
                for diff in DIFFICULTY_ORDER
                if difficulty_by_band[band][diff]
            }
            for band in BAND_ORDER
            if difficulty_by_band[band]
        },
        "BY_AUDIO": breakdown(by_audio),
        "BY_CLONE": breakdown(by_clone),
        "CAT_FAIL": cat_fail.most_common(),
        "K_DETAIL": k_detail,
        "CASES_BY_RUN": cases_by_run,
        "ROWS": sample_rows,
    }


def patch_canvas(const: dict[str, Any], *, run_id: str, template_run_id: str) -> None:
    canvas_path = CANVAS_DIR / f"mivas-legal-run-{run_id}.canvas.tsx"
    template_path = CANVAS_DIR / f"mivas-legal-run-{template_run_id}.canvas.tsx"
    if not canvas_path.is_file():
        if not template_path.is_file():
            raise SystemExit(f"canvas template missing: {template_path}")
        canvas_path.write_text(template_path.read_text())
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


def write_latest_canvas(const: dict[str, Any]) -> Path:
    canvas_path = CANVAS_DIR / f"{LATEST_CANVAS}.canvas.tsx"
    data_js = json.dumps(const, indent=2)
    canvas_path.write_text(LATEST_CANVAS_SOURCE.replace("<<DATA>>", data_js, 1))
    return canvas_path


LATEST_CANVAS_SOURCE = r'''import {
  BarChart,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Grid,
  H1,
  H2,
  Link,
  PieChart,
  Pill,
  Row,
  Select,
  Stack,
  Stat,
  Table,
  Text,
  TextInput,
  useCanvasState,
} from "cursor/canvas";

type Combined = "pass" | "fail" | "void" | "pending" | "missing";
type FilterResult = "all" | Combined;
type FilterCategory = "all" | string;

type SampleRow = {
  resultId: string;
  sourceRunId: string;
  caseKey: string;
  category: string;
  difficulty: string;
  audio: string;
  isClone: boolean;
  combined: Combined;
  toolPass: boolean;
  toolScore: number;
  missingTools: string;
  handoffPass: boolean;
  handoffVerdict: string;
  failReason: string;
  resultUrl: string;
  dbPass?: boolean;
  dbSkipped?: boolean;
};

const DATA: {
  META: {
    runId: string;
    simulationId: number;
    agentId: number;
    industry: string;
    simulationName: string;
    runUrl: string;
    simulationUrl: string;
    generatedAt: string;
    runsPerDh: number;
    s3Bucket: string;
    s3Prefix: string;
    snapshotsFetched: number;
    snapshotsExpected: number;
    deploymentBucket: string;
    dataGaps: string[];
    contributingRuns: string[];
    latestRunId: string;
    aggregate: boolean;
  };
  COUNTS: Record<string, number>;
  RATES: Record<string, number | null>;
  K_BANDS: Record<string, number>;
  TOOL_CLUSTERS: Array<[string, number]>;
  BY_CATEGORY: Array<[string, number, number]>;
  BY_DIFFICULTY: Array<[string, number, number]>;
  BY_DIFFICULTY_DETAIL: Array<{
    key: string;
    pass: number;
    fail: number;
    total: number;
    pass_rate: number | null;
    fail_rate: number | null;
  }>;
  K_BANDS_BY_DIFFICULTY: Record<string, Record<string, number>>;
  DIFFICULTY_BY_BAND: Record<string, Record<string, number>>;
  BY_AUDIO: Array<[string, number, number]>;
  BY_CLONE: Array<[string, number, number]>;
  CAT_FAIL: Array<[string, number]>;
  K_DETAIL: Array<{ case_key: string; band: string; passes: number; attempts: number; source_run_id?: string }>;
  CASES_BY_RUN: Array<{ runId: string; cases: number; attempts: number; pass: number; scored: number }>;
  ROWS: SampleRow[];
} = <<DATA>>;
const META = DATA.META;
const COUNTS = DATA.COUNTS;
const RATES = DATA.RATES;
const K_BANDS = DATA.K_BANDS;
const TOOL_CLUSTERS = DATA.TOOL_CLUSTERS;
const BY_CATEGORY = DATA.BY_CATEGORY;
const BY_DIFFICULTY = DATA.BY_DIFFICULTY;
const BY_DIFFICULTY_DETAIL = DATA.BY_DIFFICULTY_DETAIL;
const K_BANDS_BY_DIFFICULTY = DATA.K_BANDS_BY_DIFFICULTY;
const DIFFICULTY_BY_BAND = DATA.DIFFICULTY_BY_BAND;
const BY_AUDIO = DATA.BY_AUDIO;
const BY_CLONE = DATA.BY_CLONE;
const CAT_FAIL = DATA.CAT_FAIL;
const K_DETAIL = DATA.K_DETAIL;
const CASES_BY_RUN = DATA.CASES_BY_RUN;
const ROWS = DATA.ROWS;

const pillTone = (c: Combined): "success" | "warning" | "info" | "neutral" | "deleted" => {
  if (c === "pass") return "success";
  if (c === "fail") return "deleted";
  if (c === "void") return "warning";
  if (c === "pending") return "info";
  return "neutral";
};

const rowTone = (c: Combined): "success" | "danger" | "warning" | "neutral" => {
  if (c === "pass") return "success";
  if (c === "fail") return "danger";
  if (c === "void") return "warning";
  return "neutral";
};

const bandPill = (band: string): "success" | "warning" | "deleted" | "neutral" => {
  if (band === "always") return "success";
  if (band === "mixed") return "warning";
  if (band === "never") return "deleted";
  return "neutral";
};

const bandRow = (band: string): "success" | "warning" | "danger" | "neutral" => {
  if (band === "always") return "success";
  if (band === "mixed") return "warning";
  if (band === "never") return "danger";
  return "neutral";
};

function pct(n: number, d: number): string {
  if (!d) return "—";
  return `${Math.round((n / d) * 100)}%`;
}

function rateTone(rate: number | null): "success" | "warning" | "danger" | "neutral" {
  if (rate == null) return "neutral";
  if (rate >= 0.5) return "success";
  if (rate >= 0.25) return "warning";
  return "danger";
}

const DIFF_ORDER = ["easy", "medium", "hard"] as const;
const K_BAND_ORDER = ["always", "mixed", "never", "partial"] as const;

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function bandTotal(band: string): number {
  const counts = DIFFICULTY_BY_BAND[band] ?? {};
  return DIFF_ORDER.reduce((sum, diff) => sum + (counts[diff] ?? 0), 0);
}

function diffTotal(diff: string): number {
  const bands = K_BANDS_BY_DIFFICULTY[diff] ?? {};
  return K_BAND_ORDER.reduce((sum, band) => sum + (bands[band] ?? 0), 0);
}

const diffTone = (diff: string): "info" | "warning" | "danger" | "neutral" => {
  if (diff === "easy") return "info";
  if (diff === "medium") return "warning";
  if (diff === "hard") return "danger";
  return "neutral";
};

const bandTone = (band: string): "success" | "warning" | "danger" | "neutral" => {
  if (band === "always") return "success";
  if (band === "mixed") return "warning";
  if (band === "never") return "danger";
  return "neutral";
};

export default function MivasLegalLatest() {
  const [query, setQuery] = useCanvasState("search", "");
  const [resultFilter, setResultFilter] = useCanvasState<FilterResult>("resultFilter", "all");
  const [categoryFilter, setCategoryFilter] = useCanvasState<FilterCategory>("categoryFilter", "all");
  const [runFilter, setRunFilter] = useCanvasState<string>("runFilter", "all");

  const categories = Array.from(new Set(ROWS.map((r) => r.category))).sort();
  const filtered = ROWS.filter((r) => {
    if (resultFilter !== "all" && r.combined !== resultFilter) return false;
    if (categoryFilter !== "all" && r.category !== categoryFilter) return false;
    if (runFilter !== "all" && r.sourceRunId !== runFilter) return false;
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    return (
      r.caseKey.toLowerCase().includes(q) ||
      r.resultId.includes(q) ||
      r.sourceRunId.includes(q) ||
      r.missingTools.toLowerCase().includes(q) ||
      r.failReason.toLowerCase().includes(q)
    );
  });

  const catChart = BY_CATEGORY.map(([cat, pass, total]) => ({
    cat,
    rate: total ? Math.round((pass / total) * 100) : 0,
  }));
  const diffChart = BY_DIFFICULTY.map(([d, pass, total]) => ({
    d,
    rate: total ? Math.round((pass / total) * 100) : 0,
  }));
  const bandsWithCases = K_BAND_ORDER.filter((band) => bandTotal(band) > 0);
  const diffsWithCases = DIFF_ORDER.filter((diff) => diffTotal(diff) > 0);
  const diffInBandCategories = bandsWithCases.map((band) => `${titleCase(band)} (${bandTotal(band)})`);
  const diffInBandSeries = DIFF_ORDER.map((diff) => ({
    name: titleCase(diff),
    tone: diffTone(diff),
    data: bandsWithCases.map((band) => (DIFFICULTY_BY_BAND[band] ?? {})[diff] ?? 0),
  }));
  const bandInDiffCategories = diffsWithCases.map((diff) => `${titleCase(diff)} (${diffTotal(diff)})`);
  const bandInDiffSeries = K_BAND_ORDER.filter((band) =>
    diffsWithCases.some((diff) => (K_BANDS_BY_DIFFICULTY[diff] ?? {})[band]),
  ).map((band) => ({
    name: titleCase(band),
    tone: bandTone(band),
    data: diffsWithCases.map((diff) => (K_BANDS_BY_DIFFICULTY[diff] ?? {})[band] ?? 0),
  }));
  const explorer = filtered;
  const topMissing = TOOL_CLUSTERS.slice(0, 4).map(([tool]) => tool).join(", ");
  const sourceNote = `Source: hangup dumps + verifier · ${META.generatedAt}`;

  return (
    <Stack gap={24} style={{ maxWidth: 1200, margin: "0 auto", padding: 24 }}>
      <Stack gap={8}>
        <H1>{META.aggregate ? "Legal MIVAS — latest aggregate" : `Legal MIVAS — run ${META.runId}`}</H1>
        <Text tone="secondary">
          {META.aggregate
            ? `Same scoring as a single-run dashboard. For each case, the newest run that includes it supersedes older k=3 attempts. Generated ${META.generatedAt}.`
            : `Full k=3 deterministic scoring from a single simulation run. Generated ${META.generatedAt}.`}
        </Text>
        <Row gap={16} wrap>
          <Link href={META.simulationUrl}>Simulation {META.simulationId}</Link>
          <Link href={META.runUrl}>Run {META.runId}</Link>
          <Text size="small" tone="tertiary">
            s3://{META.s3Bucket}/{META.s3Prefix}/{'{result_id}'}.final.json · {META.snapshotsFetched}/{META.snapshotsExpected} hangup dumps
          </Text>
        </Row>
      </Stack>

      {META.aggregate ? (
        <Callout tone="info" title="Newer run wins per case">
          <Text size="small">
            {COUNTS.unique_cases} cases · {COUNTS.scored_deterministic} scored attempts from runs {META.contributingRuns.join(", ")}. Re-run a subset and rebuild this canvas — those cases update, the rest stay.
          </Text>
        </Callout>
      ) : (
        <Callout tone="info" title="Single run snapshot">
          <Text size="small">
            {COUNTS.unique_cases} cases · {COUNTS.scored_deterministic} scored attempts · all results from run {META.runId}.
          </Text>
        </Callout>
      )}

      <Grid columns={4} gap={16}>
        <Stat value={`${COUNTS.combined_pass} / ${COUNTS.scored_deterministic}`} label="Combined deterministic pass" tone={rateTone(RATES.combined)} />
        <Stat value={pct(COUNTS.tool_pass, COUNTS.scored_deterministic)} label="Tool completion pass" tone={rateTone(RATES.tool)} />
        <Stat value={pct(COUNTS.handoff_pass, COUNTS.scored_deterministic)} label="Handoff pass" tone={rateTone(RATES.handoff)} />
        <Stat value={`${COUNTS.unique_cases} cases`} label="Pack coverage" tone="success" />
      </Grid>

      <Grid columns={4} gap={16}>
        <Stat value={pct(COUNTS.db_pass, COUNTS.db_scored)} label="DB compare pass" tone={rateTone(RATES.db)} />
        <Stat value={String(COUNTS.db_skipped)} label="DB skipped" tone="neutral" />
        <Stat value={String(COUNTS.db_fail)} label="DB mismatches" tone="danger" />
        <Stat value={String(COUNTS.void)} label="Void results" tone="neutral" />
      </Grid>

      <Card>
        <CardHeader trailing={sourceNote}>Coverage by source run</CardHeader>
        <CardBody>
          <Table
            headers={["Run (newest wins)", "Cases", "Attempts", "Combined pass", "Rate"]}
            rows={CASES_BY_RUN.map((row) => [
              row.runId === META.latestRunId ? `${row.runId} (latest)` : row.runId,
              String(row.cases),
              String(row.attempts),
              `${row.pass} / ${row.scored}`,
              pct(row.pass, row.scored),
            ])}
            striped
          />
        </CardBody>
      </Card>

      <Grid columns={2} gap={24}>
        <Card>
          <CardHeader>Pass rate by category (%)</CardHeader>
          <CardBody>
            <BarChart height={220} yMax={100} valueSuffix="%" categories={catChart.map((x) => x.cat)} series={[{ name: "Pass rate %", data: catChart.map((x) => x.rate), tone: "info" }]} />
            <Text size="small" tone="tertiary">{sourceNote}</Text>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Pass rate by difficulty (%)</CardHeader>
          <CardBody>
            <BarChart height={220} yMax={100} valueSuffix="%" categories={diffChart.map((x) => x.d)} series={[{ name: "Pass rate %", data: diffChart.map((x) => x.rate), tone: "info" }]} />
            <Text size="small" tone="tertiary">{sourceNote}</Text>
          </CardBody>
        </Card>
      </Grid>

      <Grid columns={2} gap={24}>
        <Card>
          <CardHeader>Share of Easy / Medium / Hard within each band (%)</CardHeader>
          <CardBody>
            <BarChart
              height={260}
              yMax={100}
              valueSuffix="%"
              normalized
              stacked
              categories={diffInBandCategories}
              series={diffInBandSeries}
            />
            <Text size="small" tone="tertiary">{sourceNote} · k=3 case bands</Text>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Share of each difficulty in always / mixed / never (%)</CardHeader>
          <CardBody>
            <BarChart
              height={260}
              yMax={100}
              valueSuffix="%"
              normalized
              stacked
              categories={bandInDiffCategories}
              series={bandInDiffSeries}
            />
            <Text size="small" tone="tertiary">{sourceNote} · failure / instability by difficulty</Text>
          </CardBody>
        </Card>
      </Grid>

      <Grid columns={2} gap={24}>
        <Card>
          <CardHeader>k=3 consistency bands</CardHeader>
          <CardBody>
            <PieChart donut size={200} data={[
              { label: "always (3/3)", value: K_BANDS.always ?? 0, tone: "success" },
              { label: "mixed", value: K_BANDS.mixed ?? 0, tone: "warning" },
              { label: "never (0/3)", value: K_BANDS.never ?? 0, tone: "danger" },
            ]} />
            <Text size="small" tone="tertiary">{sourceNote}</Text>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Top missing-tool clusters</CardHeader>
          <CardBody>
            <BarChart height={220} horizontal categories={TOOL_CLUSTERS.map(([tool]) => tool)} series={[{ name: "Fail count", data: TOOL_CLUSTERS.map(([, n]) => n), tone: "danger" }]} />
            <Text size="small" tone="tertiary">{sourceNote}</Text>
          </CardBody>
        </Card>
      </Grid>

      <Grid columns={2} gap={24}>
        <Card>
          <CardHeader>Audio condition</CardHeader>
          <CardBody>
            <Table headers={["Audio", "Pass", "Total", "Rate"]} rows={BY_AUDIO.map(([a, p, t]) => [a, String(p), String(t), pct(p, t)])} />
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Original vs clone</CardHeader>
          <CardBody>
            <Table headers={["Type", "Pass", "Total", "Rate"]} rows={BY_CLONE.map(([a, p, t]) => [a, String(p), String(t), pct(p, t)])} />
          </CardBody>
        </Card>
      </Grid>

      <Stack gap={8}>
        <H2>Failure count by category</H2>
        <BarChart height={180} categories={CAT_FAIL.map(([cat]) => cat)} series={[{ name: "Combined fails", data: CAT_FAIL.map(([, n]) => n), tone: "danger" }]} />
        <Text size="small" tone="tertiary">{sourceNote}</Text>
      </Stack>

      <Card>
        <CardHeader trailing={`${K_DETAIL.length} cases`}>Case k=3 bands</CardHeader>
        <CardBody>
          <Table
            headers={["Case", "Band", "Pass", "Source run"]}
            rows={K_DETAIL.map((row) => [
              row.case_key,
              <Pill tone={bandPill(row.band)}>{row.band}</Pill>,
              `${row.passes}/${row.attempts}`,
              row.source_run_id || "—",
            ])}
            rowTone={K_DETAIL.map((row) => bandRow(row.band))}
            striped
          />
        </CardBody>
      </Card>

      <Card>
        <CardHeader trailing={`${filtered.length} / ${ROWS.length}`}>Per-result explorer</CardHeader>
        <CardBody>
          <Stack gap={12}>
            <Row gap={12} wrap align="center">
              <TextInput aria-label="Search" placeholder="Search…" value={query} onChange={setQuery} style={{ minWidth: 220, flex: 1 }} />
              <Select aria-label="Combined result filter" value={resultFilter} onChange={(v) => setResultFilter(v as FilterResult)} options={[
                { label: "All results", value: "all" },
                { label: "Pass", value: "pass" },
                { label: "Fail", value: "fail" },
                { label: "Void", value: "void" },
              ]} />
              <Select aria-label="Category filter" value={categoryFilter} onChange={(v) => setCategoryFilter(v as FilterCategory)} options={[
                { label: "All categories", value: "all" },
                ...categories.map((c) => ({ label: c, value: c })),
              ]} />
              <Select aria-label="Source run filter" value={runFilter} onChange={setRunFilter} options={[
                { label: "All source runs", value: "all" },
                ...META.contributingRuns.map((rid) => ({ label: `Run ${rid}`, value: rid })),
              ]} />
            </Row>
            <Table
              headers={["Case", "Run", "Cat", "Diff", "Combined", "Tools", "Missing", "Handoff", "Reason", "Link"]}
              rows={explorer.map((r) => [
                r.caseKey, r.sourceRunId, r.category, r.difficulty,
                <Pill tone={pillTone(r.combined)}>{r.combined}</Pill>,
                r.toolPass ? "pass" : `fail (${r.toolScore})`,
                r.missingTools || "—",
                r.handoffPass ? "pass" : r.handoffVerdict || "fail",
                r.failReason || "—",
                <Link href={r.resultUrl}>#{r.resultId}</Link>,
              ])}
              rowTone={explorer.map((r) => rowTone(r.combined))}
              striped
            />
          </Stack>
        </CardBody>
      </Card>

      <Callout tone="info" title="Actionable clusters">
        <Stack gap={4}>
          <Text size="small">1. Combined pass {COUNTS.combined_pass}/{COUNTS.scored_deterministic} — always {K_BANDS.always ?? 0} / mixed {K_BANDS.mixed ?? 0} / never {K_BANDS.never ?? 0} at k=3.</Text>
          <Text size="small">2. Tool misses: {topMissing || "none"}.</Text>
          <Text size="small">3. DB mismatches {COUNTS.db_fail}/{COUNTS.db_scored} — prose fields may differ from replay placeholders even when tools fire.</Text>
        </Stack>
      </Callout>
    </Stack>
  );
}
'''


def write_sidecars(const: dict[str, Any], ver: dict[str, Any], *, stem: str) -> None:
    CANVAS_DIR.mkdir(parents=True, exist_ok=True)
    (CANVAS_DIR / f"{stem}.data.json").write_text(json.dumps(const, indent=2) + "\n")
    (CANVAS_DIR / f"{stem}.verifier.json").write_text(json.dumps(ver, indent=2) + "\n")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default=DEFAULT_RUN_ID)
    ap.add_argument(
        "--template-run-id",
        default=DEFAULT_RUN_ID,
        help="Existing canvas to copy when target canvas is missing",
    )
    ap.add_argument(
        "--supplement-run-id",
        action="append",
        default=[],
        help="Additional run IDs to merge (same subset, split queue)",
    )
    ap.add_argument(
        "--aggregate",
        action="store_true",
        help="One dashboard: newest run supersedes older results per case",
    )
    ap.add_argument(
        "--write-latest",
        action="store_true",
        help="Also refresh mivas-legal-latest.canvas.tsx from this run (single-run snapshot)",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Re-run the verifier even if a cached JSON exists",
    )
    args = ap.parse_args()

    if args.aggregate:
        run_ids = discover_run_ids()
        if not run_ids:
            raise SystemExit(f"no run dumps under actual-final-state/{SLUG}")
        loaded: list[tuple[str, dict[str, Any]]] = []
        for rid in run_ids:
            print(f"loading {rid}...", flush=True)
            loaded.append((rid, load_or_verify_run(rid, refresh=args.refresh)))
            cache_path(rid).write_text(json.dumps(loaded[-1][1], indent=2) + "\n")
        ver = supersede_by_case(loaded)
        const = build(
            ver,
            run_id="latest",
            actuals=actuals_dir(run_ids[-1]),
            extra_actuals=[actuals_dir(rid) for rid in run_ids[:-1]],
            aggregate=True,
        )
        write_sidecars(const, ver, stem=LATEST_CANVAS)
        path = write_latest_canvas(const)
        c = const["COUNTS"]
        print(
            f"latest dashboard {path.name}: combined {c['combined_pass']}/{c['scored_deterministic']} "
            f"cases {c['unique_cases']} tools {c['tool_pass']} handoff {c['handoff_pass']} "
            f"db {c['db_pass']}/{c['db_scored']} "
            f"source {const['META']['contributingRuns']}"
        )
        return 0

    run_id = args.run_id
    actuals = actuals_dir(run_id)
    extra_ids = args.supplement_run_id
    extra_dirs = [actuals_dir(rid) for rid in extra_ids]

    ver = run_verifier(
        run_id,
        actuals,
        extra_run_ids=extra_ids or None,
        extra_actuals=extra_dirs or None,
    )
    const = build(
        ver,
        run_id=run_id,
        actuals=actuals,
        extra_actuals=extra_dirs or None,
    )
    write_sidecars(const, ver, stem=f"mivas-legal-run-{run_id}")
    if args.write_latest:
        write_sidecars(const, ver, stem=LATEST_CANVAS)
        latest_path = write_latest_canvas(const)
        c = const["COUNTS"]
        print(
            f"latest dashboard {latest_path.name}: combined {c['combined_pass']}/{c['scored_deterministic']} "
            f"cases {c['unique_cases']} tools {c['tool_pass']} handoff {c['handoff_pass']} "
            f"db {c['db_pass']}/{c['db_scored']} run {run_id}"
        )
    patch_canvas(const, run_id=run_id, template_run_id=args.template_run_id)
    c = const["COUNTS"]
    print(
        f"dashboard updated: combined {c['combined_pass']}/{c['scored_deterministic']} "
        f"tools {c['tool_pass']} handoff {c['handoff_pass']} "
        f"db {c['db_pass']}/{c['db_scored']} fail {c['db_fail']} skip {c['db_skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
