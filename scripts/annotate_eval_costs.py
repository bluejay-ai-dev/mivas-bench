#!/usr/bin/env python3
"""Re-price the LLM cost columns of existing eval CSVs with eval_costs.

New exports get these columns from bluejay_run_to_csv via eval_costs; this stamps the
same fields onto CSVs that were exported earlier, so a pricing-table or costing change
reaches every leaderboard row. Traces are read from .cache/eval_costs (fetched on demand).

    uv run python scripts/annotate_eval_costs.py                       # every CSV below
    uv run python scripts/annotate_eval_costs.py --industry legal --slug qwen-audio-realtime
    uv run python scripts/annotate_eval_costs.py --summary /tmp/costs.json --dry-run

Prints the before/after cost per hour of every CSV; --summary also writes them as JSON.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
EVAL_OUTPUTS = ROOT / "eval_outputs"

# harness slug -> CSV, per industry pack; mirrors bluejay-labs/scripts/ingest.py
INDUSTRY_CSVS: dict[str, dict[str, str]] = {
    "healthcare": {
        "openai-gpt-live-1@astra-medium": "healthcare-openai-gpt-live-1-astra-medium-354859.csv",
        "openai-gpt-live-1@sol-low": "healthcare-openai-gpt-live-1-sol-low-354856.csv",
        "openai-realtime-2.1": "healthcare-openai-realtime-2.1-253934.csv",
        "openai-realtime-2.1-mini": "healthcare-openai-realtime-2.1-mini-248155.csv",
        "grok-voice": "healthcare-grok-voice-248526.csv",
        "aws-nova-sonic-2": "healthcare-aws-nova-sonic-2-248460.csv",
        "gemini-3.8-live@extended": "healthcare-gemini-3.8-live-extended-354876.csv",
        "gemini-3.8-live": "healthcare-gemini-3.8-live-354868.csv",
        "gemini-flash-live-3.1": "healthcare-gemini-flash-live-3.1-247348.csv",
        "gemini-2.5-flash-native-audio": "healthcare-gemini-2.5-flash-native-audio-247475.csv",
        "qwen-audio-realtime": "healthcare-qwen-audio-realtime-248135.csv",
        "livekit-cascaded": "healthcare-livekit-cascaded-248703.csv",
    },
    "legal": {
        "openai-gpt-live-1@astra-medium": "legal-openai-gpt-live-1-astra-medium-354871.csv",
        "openai-gpt-live-1@sol-low": "legal-openai-gpt-live-1-sol-low-354865.csv",
        "openai-realtime-2.1": "legal-openai-realtime-2.1-254157.csv",
        "openai-realtime-2.1-mini": "legal-openai-realtime-2.1-mini-254160.csv",
        "grok-voice": "legal-grok-voice-254141.csv",
        "aws-nova-sonic-2": "legal-aws-nova-sonic-2-254158.csv",
        "gemini-3.8-live@extended": "legal-gemini-3.8-live-extended-354815.csv",
        "gemini-3.8-live": "legal-gemini-3.8-live-356269.csv",
        "gemini-flash-live-3.1": "legal-gemini-flash-live-3.1-254119.csv",
        "gemini-2.5-flash-native-audio": "legal-gemini-2.5-flash-native-audio-254163.csv",
        "qwen-audio-realtime": "legal-qwen-audio-realtime-254129.csv",
        "livekit-cascaded": "legal-livekit-cascaded-254130.csv",
    },
    "customer-support": {
        "openai-gpt-live-1@astra-medium": "customer-support-openai-gpt-live-1-astra-medium-354884.csv",
        "openai-gpt-live-1@sol-low": "customer-support-openai-gpt-live-1-sol-low-354880.csv",
        "openai-realtime-2.1": "customer-support-openai-realtime-2.1-257175.csv",
        "openai-realtime-2.1-mini": "customer-support-openai-realtime-2.1-mini-257177.csv",
        "grok-voice": "customer-support-grok-voice-257188.csv",
        "aws-nova-sonic-2": "customer-support-aws-nova-sonic-2-257179.csv",
        "gemini-3.8-live@extended": "customer-support-gemini-3.8-live-extended-354877.csv",
        "gemini-3.8-live": "customer-support-gemini-3.8-live-354866.csv",
        "gemini-flash-live-3.1": "customer-support-gemini-flash-live-3.1-257174.csv",
        "gemini-2.5-flash-native-audio": "customer-support-gemini-2.5-flash-native-audio-257187.csv",
        "qwen-audio-realtime": "customer-support-qwen-audio-realtime-257215.csv",
        "livekit-cascaded": "customer-support-livekit-cascaded-257156.csv",
    },
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


eval_costs = _load("eval_costs", SCRIPTS / "eval_costs.py")
csv.field_size_limit(sys.maxsize)


def cost_per_hour(rows: list[dict]) -> float | None:
    """sum(cost) / sum(duration) in USD per hour, the leaderboard's definition."""
    costs = [eval_costs.as_float(row.get("llm_cost_usd")) for row in rows]
    durations = [eval_costs.as_float(row.get("duration_s")) for row in rows]
    paired = [(c, d) for c, d in zip(costs, durations) if c is not None and d]
    if not paired:
        return None
    total_duration = sum(d for _, d in paired)
    return sum(c for c, _ in paired) / total_duration * 3600.0 if total_duration else None


def annotate_row(row: dict, harness: str, industry: str, pricing: dict) -> dict:
    updated = dict(row)
    updated.setdefault("industry", industry)
    updated.update(eval_costs.cost_conversation(updated, harness, pricing, fetch=True))
    if "industry" not in row:
        updated.pop("industry", None)
    return updated


def rewrite_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w", delete=False, dir=path.parent, prefix=path.stem, suffix=".tmp", newline="", encoding="utf-8"
    )
    with handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    Path(handle.name).replace(path)


def annotate_csv(industry: str, harness: str, filename: str, pricing: dict, workers: int, dry_run: bool) -> dict:
    path = EVAL_OUTPUTS / filename
    if not path.exists():
        raise SystemExit(f"missing {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    for column in eval_costs.COST_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)
    updated: list[dict | None] = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(annotate_row, row, harness, industry, pricing): i for i, row in enumerate(rows)}
        for future in as_completed(futures):
            updated[futures[future]] = future.result()
    finished = [row for row in updated if row is not None]
    if not dry_run:
        rewrite_csv(path, finished, fieldnames)
    summary = {
        "industry": industry,
        "harness": harness,
        "file": filename,
        "rows": len(rows),
        "before_usd_per_hour": cost_per_hour(rows),
        "after_usd_per_hour": cost_per_hour(finished),
        "turns": sum(eval_costs.as_int(row.get("llm_cost_turns")) for row in finished),
        "backfilled_turns": sum(eval_costs.as_int(row.get("llm_cost_backfilled_turns")) for row in finished),
        "sources": {},
    }
    for row in finished:
        key = row.get("llm_cost_source") or "none"
        summary["sources"][key] = summary["sources"].get(key, 0) + 1
    before = summary["before_usd_per_hour"]
    after = summary["after_usd_per_hour"]
    print(
        f"{industry:16s} {harness:32s} "
        f"${before if before is not None else 0:6.2f}/hr -> ${after if after is not None else 0:6.2f}/hr  "
        f"turns {summary['turns']} backfilled {summary['backfilled_turns']} {summary['sources']}",
        flush=True,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", help="annotate only this harness slug (e.g. gemini-3.8-live@extended)")
    parser.add_argument("--industry", choices=tuple(INDUSTRY_CSVS), help="annotate only this pack")
    parser.add_argument("--summary", type=Path, help="write the before/after summary JSON here")
    parser.add_argument("--dry-run", action="store_true", help="compute and print, leave the CSVs untouched")
    args = parser.parse_args()
    pricing = eval_costs.load_pricing()
    workers = min(12, os.cpu_count() or 8)
    eval_costs.CACHE.mkdir(parents=True, exist_ok=True)
    packs = [args.industry] if args.industry else list(INDUSTRY_CSVS)
    jobs = [(pack, slug, name) for pack in packs for slug, name in INDUSTRY_CSVS[pack].items()]
    if args.slug:
        jobs = [job for job in jobs if job[1] == args.slug]
        if not jobs:
            raise SystemExit(f"unknown slug {args.slug}")
    summaries = [annotate_csv(pack, slug, name, pricing, workers, args.dry_run) for pack, slug, name in jobs]
    if args.summary:
        args.summary.write_text(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
