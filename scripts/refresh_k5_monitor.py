#!/usr/bin/env python3
"""Re-inline healthcare k=5 poller snapshots into the progress-monitor canvas.

Canvas runtime cannot fetch `.mivas-monitor/*.json`. This script is the Refresh
path: read the latest poller files, rewrite the marked snapshot block in
`healthcare-k5-progress-monitor.canvas.tsx`, and write the official
`.canvas.data.json` sidecar so `useCanvasState("k5Monitor")` stays current.

    uv run python scripts/refresh_k5_monitor.py
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MONITOR_DIR = ROOT / ".mivas-monitor"
DEFAULT_CANVAS = (
    Path.home()
    / ".cursor/projects/Users-farazsiddiqi-Desktop-bluejay-repos-mivas-bench"
    / "canvases"
    / "healthcare-k5-progress-monitor.canvas.tsx"
)

SIM_IDS = (30998, 30999, 31000, 31001, 31002, 31003, 31004)

SHORT_NAMES = {
    "openai/realtime-2.1": "realtime-2.1",
    "openai/realtime-2.1-mini": "realtime-2.1-mini",
    "gemini/flash-live-3.1": "flash-live-3.1",
    "gemini/2.5-flash-native-audio": "native-audio",
    "aws/nova-sonic-2": "nova-sonic-2",
    "grok/voice": "grok-voice",
    "qwen/audio-realtime": "qwen-audio",
}

MARKER_START = "/* k5-monitor-data-start */"
MARKER_END = "/* k5-monitor-data-end */"
STATE_KEY = "k5Monitor"


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _i(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_issues(raw: dict[str, Any]) -> dict[str, int]:
    issues = raw.get("issues") or {}
    return {
        "NO_ANSWER": _i(issues.get("NO_ANSWER")),
        "NO_CONNECTION": _i(issues.get("NO_CONNECTION")),
        "CANCELLED": _i(issues.get("CANCELLED")),
        "ERROR": _i(issues.get("ERROR") or issues.get("system_connection_ERROR")),
        "SYSTEM_ERROR": _i(issues.get("SYSTEM_ERROR")),
        "other": _i(issues.get("other")),
    }


def parse_latency(raw: dict[str, Any]) -> dict[str, Any]:
    lat = raw.get("latency") or {}
    avg: float | None = None
    p50: float | None = None
    p95: float | None = None
    n = 0

    if "avg_agent_latency_mean" in lat:
        avg = _f(lat.get("avg_agent_latency_mean"))
        p50 = _f(lat.get("p50_agent_latency_mean"))
        p95 = _f(lat.get("p95_agent_latency_mean") or lat.get("p90_agent_latency_mean"))
        n = _i(lat.get("n"))
    else:
        avg_o = lat.get("avg_agent_latency") or lat.get("avg_agent_latency_ms")
        if isinstance(avg_o, (int, float)):
            avg = float(avg_o)
            n = _i(lat.get("n"))
        elif isinstance(avg_o, dict):
            avg = _f(avg_o.get("mean"))
            n = _i(avg_o.get("n") or lat.get("n"))
            p50 = _f(avg_o.get("p50"))

        p50_o = lat.get("p50_agent_latency") or lat.get("p50_agent_latency_ms")
        if isinstance(p50_o, dict):
            p50 = _f(p50_o.get("mean"))
            n = n or _i(p50_o.get("n"))
        elif isinstance(p50_o, (int, float)):
            p50 = float(p50_o)

        p95_o = (
            lat.get("p95_agent_latency")
            or lat.get("p90_agent_latency")
            or lat.get("p95_agent_latency_ms")
            or lat.get("p90_agent_latency_ms")
        )
        if isinstance(p95_o, dict):
            p95 = _f(p95_o.get("mean"))
        elif isinstance(p95_o, (int, float)):
            p95 = float(p95_o)

    # grok/voice (and similar) report seconds; canvas latency columns are ms
    if avg is not None and avg < 100:
        avg = round(avg * 1000, 4)
        if p50 is not None:
            p50 = round(p50 * 1000, 4)
        if p95 is not None:
            p95 = round(p95 * 1000, 4)

    return {"avg": avg, "p50": p50, "p95": p95, "n": n}


def _metric_block(cm: dict[str, Any], name: str) -> tuple[float | None, int, Any]:
    obj = cm.get(name)
    if isinstance(obj, dict):
        if name == "premature_end":
            mean = _f(obj.get("rate") if obj.get("rate") is not None else obj.get("mean"))
        else:
            mean = _f(obj.get("mean"))
        return mean, _i(obj.get("n")), obj.get("yes")
    mean_key = "premature_end_rate" if name == "premature_end" else f"{name}_mean"
    return _f(cm.get(mean_key)), _i(cm.get(f"n_{name}")), None


def parse_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    cm = raw.get("custom_metrics") or {}
    pa_mean, pa_n, _ = _metric_block(cm, "prompt_adherence")
    tc_mean, tc_n, _ = _metric_block(cm, "task_completion")
    pe_rate, pe_n, pe_yes = _metric_block(cm, "premature_end")
    if pe_yes is None and pe_rate is not None and pe_n:
        pe_yes = int(round(pe_rate * pe_n))
    else:
        pe_yes = _i(pe_yes)
    return {
        "prompt_adherence_mean": pa_mean,
        "prompt_adherence_n": pa_n,
        "task_completion_mean": tc_mean,
        "task_completion_n": tc_n,
        "premature_end_rate": pe_rate,
        "premature_end_yes": pe_yes,
        "premature_end_n": pe_n,
    }


def to_row(index_entry: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    harness = str(index_entry["harness"])
    counts = raw.get("status_counts") or {}
    status = str(raw.get("status") or raw.get("run_status") or "QUEUED").upper()
    if status not in {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}:
        status = "RUNNING"
    return {
        "harness": harness,
        "short": SHORT_NAMES.get(harness, harness.rsplit("/", 1)[-1]),
        "agent_id": _i(raw.get("agent_id") or index_entry.get("agent_id")),
        "simulation_id": _i(
            raw.get("simulation_id") or raw.get("sim_id") or index_entry.get("sim_id")
        ),
        "run_id": _i(raw.get("run_id") or index_entry.get("run_id")),
        "status": status,
        "completed": _i(raw.get("completed") if raw.get("completed") is not None else counts.get("COMPLETED")),
        "total": _i(raw.get("total") or 360),
        "in_progress": _i(counts.get("RUNNING")),
        "initializing": _i(counts.get("INITIALIZING")),
        "evaluating": _i(counts.get("EVALUATING")),
        "conversation_ended": _i(counts.get("CONVERSATION_ENDED")),
        "queued": _i(counts.get("QUEUED")),
        "concurrency": _i(index_entry.get("concurrency")),
        "issues": parse_issues(raw),
        "latency": parse_latency(raw),
        "metrics": parse_metrics(raw),
        "csv_path": raw.get("csv_path"),
        "updated_at": str(raw.get("updated_at") or ""),
    }


def load_monitor(monitor_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    index = json.loads((monitor_dir / "index.json").read_text())
    by_sim = {int(h["sim_id"]): h for h in index["harnesses"]}
    rows: list[dict[str, Any]] = []
    for sim_id in SIM_IDS:
        path = monitor_dir / f"{sim_id}.json"
        raw = json.loads(path.read_text())
        rows.append(to_row(by_sim[sim_id], raw))
    updated = [r["updated_at"] for r in rows if r["updated_at"]]
    snapshot_at = max(updated) if updated else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return snapshot_at, rows


def _ts_num(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    return json.dumps(value)


def emit_harness_ts(row: dict[str, Any]) -> str:
    issues = row["issues"]
    lat = row["latency"]
    met = row["metrics"]
    csv_path = "null" if row["csv_path"] is None else json.dumps(row["csv_path"])
    return "\n".join(
        [
            "  {",
            f'    harness: {json.dumps(row["harness"])},',
            f'    short: {json.dumps(row["short"])},',
            f'    agent_id: {row["agent_id"]},',
            f'    simulation_id: {row["simulation_id"]},',
            f'    run_id: {row["run_id"]},',
            f'    status: {json.dumps(row["status"])},',
            f'    completed: {row["completed"]},',
            f'    total: {row["total"]},',
            f'    in_progress: {row["in_progress"]},',
            f'    initializing: {row["initializing"]},',
            f'    evaluating: {row["evaluating"]},',
            f'    conversation_ended: {row["conversation_ended"]},',
            f'    queued: {row["queued"]},',
            f'    concurrency: {row["concurrency"]},',
            "    issues: {{ NO_ANSWER: {na}, NO_CONNECTION: {nc}, CANCELLED: {ca}, ERROR: {er}, SYSTEM_ERROR: {se}, other: {ot} }},".format(
                na=issues["NO_ANSWER"],
                nc=issues["NO_CONNECTION"],
                ca=issues["CANCELLED"],
                er=issues["ERROR"],
                se=issues["SYSTEM_ERROR"],
                ot=issues["other"],
            ),
            f'    latency: {{ avg: {_ts_num(lat["avg"])}, p50: {_ts_num(lat["p50"])}, p95: {_ts_num(lat["p95"])}, n: {lat["n"]} }},',
            "    metrics: {",
            f'      prompt_adherence_mean: {_ts_num(met["prompt_adherence_mean"])},',
            f'      prompt_adherence_n: {met["prompt_adherence_n"]},',
            f'      task_completion_mean: {_ts_num(met["task_completion_mean"])},',
            f'      task_completion_n: {met["task_completion_n"]},',
            f'      premature_end_rate: {_ts_num(met["premature_end_rate"])},',
            f'      premature_end_yes: {met["premature_end_yes"]},',
            f'      premature_end_n: {met["premature_end_n"]},',
            "    },",
            f"    csv_path: {csv_path},",
            f'    updated_at: {json.dumps(row["updated_at"])},',
            "  }",
        ]
    )


def src_caption(snapshot_at: str) -> str:
    clock = snapshot_at.replace("T", " ").replace("Z", " UTC")
    return (
        "Source: .mivas-monitor/{sim}.json · healthcare · k=5 · 72 DHs · "
        f"snapshot {clock}"
    )


def emit_data_block(snapshot_at: str, rows: list[dict[str, Any]]) -> str:
    body = ",\n".join(emit_harness_ts(row) for row in rows)
    src = src_caption(snapshot_at).replace("\\", "\\\\").replace('"', '\\"')
    return (
        f"{MARKER_START}\n"
        f'const SNAPSHOT_AT = {json.dumps(snapshot_at)};\n'
        f"const SRC =\n"
        f'  "{src}";\n'
        f"\n"
        f"const HARNESSES: HarnessRow[] = [\n"
        f"{body},\n"
        f"];\n"
        f"{MARKER_END}"
    )


def write_canvas(canvas: Path, snapshot_at: str, rows: list[dict[str, Any]]) -> None:
    text = canvas.read_text()
    if MARKER_START not in text or MARKER_END not in text:
        raise SystemExit(
            f"{canvas} is missing {MARKER_START} / {MARKER_END} markers"
        )
    block = emit_data_block(snapshot_at, rows)
    updated, n = re.subn(
        re.escape(MARKER_START) + r"[\s\S]*?" + re.escape(MARKER_END),
        lambda _: block,
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit(f"failed to replace snapshot block in {canvas}")
    if "const COLORS" not in updated or "type HarnessRow" not in updated:
        raise SystemExit(
            f"refusing to write {canvas}: snapshot replace would drop COLORS / types"
        )
    canvas.write_text(updated)


def write_sidecar(canvas: Path, snapshot_at: str, rows: list[dict[str, Any]]) -> Path:
    sidecar = canvas.with_name(canvas.name.replace(".canvas.tsx", ".canvas.data.json"))
    existing: dict[str, Any] = {}
    if sidecar.is_file():
        try:
            loaded = json.loads(sidecar.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            existing = {}
    existing[STATE_KEY] = {
        "snapshot_at": snapshot_at,
        "source": src_caption(snapshot_at),
        "harnesses": rows,
    }
    sidecar.write_text(json.dumps(existing, indent=2) + "\n")
    return sidecar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor-dir", type=Path, default=MONITOR_DIR)
    parser.add_argument("--canvas", type=Path, default=DEFAULT_CANVAS)
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="print completed/total and skip writing files",
    )
    args = parser.parse_args()

    snapshot_at, rows = load_monitor(args.monitor_dir)
    if args.print_only:
        for row in rows:
            print(f"{row['harness']}: {row['completed']}/{row['total']} {row['status']}")
        done = sum(r["completed"] for r in rows)
        total = sum(r["total"] for r in rows)
        print(f"all: {done}/{total} snapshot {snapshot_at}")
        return 0

    write_canvas(args.canvas, snapshot_at, rows)
    sidecar = write_sidecar(args.canvas, snapshot_at, rows)
    done = sum(r["completed"] for r in rows)
    total = sum(r["total"] for r in rows)
    print(f"wrote {args.canvas}")
    print(f"wrote {sidecar}")
    for row in rows:
        print(f"  {row['harness']}: {row['completed']}/{row['total']} {row['status']}")
    print(f"all: {done}/{total} snapshot {snapshot_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
