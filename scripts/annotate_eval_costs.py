#!/usr/bin/env python3
"""Backfill conversation and utterance LLM costs onto existing eval CSVs.

New exports get these columns from bluejay_run_to_csv via eval_costs.
This script stamps the same fields onto already-written healthcare CSVs.

    uv run python scripts/annotate_eval_costs.py
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
EVAL_OUTPUTS = ROOT / "eval_outputs"

HEALTHCARE_CSVS = {
    "openai-realtime-2.1": "healthcare-openai-realtime-2.1-253934.csv",
    "openai-realtime-2.1-mini": "healthcare-openai-realtime-2.1-mini-248155.csv",
    "grok-voice": "healthcare-grok-voice-248526.csv",
    "aws-nova-sonic-2": "healthcare-aws-nova-sonic-2-248460.csv",
    "gemini-flash-live-3.1": "healthcare-gemini-flash-live-3.1-247348.csv",
    "gemini-2.5-flash-native-audio": "healthcare-gemini-2.5-flash-native-audio-247475.csv",
    "qwen-audio-realtime": "healthcare-qwen-audio-realtime-248135.csv",
    "livekit-cascaded": "healthcare-livekit-cascaded-248703.csv",
}

LEGAL_CSVS = {
    "openai-realtime-2.1": "legal-openai-realtime-2.1-254157.csv",
    "openai-realtime-2.1-mini": "legal-openai-realtime-2.1-mini-254160.csv",
    "grok-voice": "legal-grok-voice-254141.csv",
    "aws-nova-sonic-2": "legal-aws-nova-sonic-2-254158.csv",
    "gemini-flash-live-3.1": "legal-gemini-flash-live-3.1-254119.csv",
    "gemini-2.5-flash-native-audio": "legal-gemini-2.5-flash-native-audio-254163.csv",
    "qwen-audio-realtime": "legal-qwen-audio-realtime-254129.csv",
    "livekit-cascaded": "legal-livekit-cascaded-254130.csv",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


eval_costs = _load("eval_costs", SCRIPTS / "eval_costs.py")


def annotate_row(row: dict, slug: str, pricing: dict) -> dict:
    costs = eval_costs.cost_conversation(row, slug, pricing, fetch=True)
    updated = dict(row)
    updated.update(costs)
    return updated


def rewrite_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=path.parent,
        prefix=path.stem,
        suffix=".tmp",
        newline="",
        encoding="utf-8",
    )
    with handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    Path(handle.name).replace(path)


def annotate_csv(slug: str, filename: str, pricing: dict, workers: int) -> None:
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
    print(f"{slug}: {len(rows)} rows from {path.name}", flush=True)
    updated = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(annotate_row, row, slug, pricing): index for index, row in enumerate(rows)}
        done = 0
        for future in as_completed(futures):
            index = futures[future]
            updated[index] = future.result()
            done += 1
            if done % 40 == 0 or done == len(rows):
                print(f"  {slug} {done}/{len(rows)}", flush=True)
    rewrite_csv(path, updated, fieldnames)
    costs = [
        eval_costs.as_float(row.get("llm_cost_usd"))
        for row in updated
        if eval_costs.as_float(row.get("llm_cost_usd")) is not None
    ]
    durs = [
        eval_costs.as_float(row.get("duration_s"))
        for row in updated
        if eval_costs.as_float(row.get("duration_s"))
    ]
    hourly = (sum(costs) / sum(durs) * 3600.0) if costs and durs and sum(durs) else None
    print(
        f"  wrote {path.name}: mean ${ (sum(costs)/len(costs)) :.4f}/call"
        + (f", ${hourly:.2f}/hr" if hourly is not None else ""),
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", help="annotate only this harness slug")
    parser.add_argument("--industry", choices=("healthcare", "legal"), help="annotate only this pack")
    args = parser.parse_args()
    pricing = eval_costs.load_pricing()
    workers = min(12, os.cpu_count() or 8)
    eval_costs.CACHE.mkdir(parents=True, exist_ok=True)
    jobs = []
    if args.industry != "legal":
        jobs.extend(HEALTHCARE_CSVS.items())
    if args.industry != "healthcare":
        jobs.extend(LEGAL_CSVS.items())
    if args.slug:
        jobs = [(slug, filename) for slug, filename in jobs if slug == args.slug]
        if not jobs:
            raise SystemExit(f"unknown slug {args.slug}")
    for slug, filename in jobs:
        annotate_csv(slug, filename, pricing, workers)


if __name__ == "__main__":
    main()
