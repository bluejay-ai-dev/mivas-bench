"""Download hangup GET /state dumps from S3, keyed by the Bluejay result.

Each simulation result id is the S3 object name and the conversation key.
That result also carries digital_human_id, which joins back to an expected
case. k=3 means three result folders under one digital-human folder.

    uv run python scripts/pull_actual_final_state.py RUN_ID --slug openai-realtime-2-1-customer-support
    uv run python scripts/pull_actual_final_state.py RUN_ID --harness openai/realtime-2.1 --industry customer-support

Writes:

    actual-final-state/{slug}/{run_id}/{digital_human_id}/{result_id}/final.json
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
DEFAULT_OUT = ROOT / "actual-final-state"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def s3_final_key(slug: str, result_id: str) -> str:
    prefix = (os.environ.get("MIVAS_SNAPSHOT_PREFIX", "mivas").strip() or "mivas").strip("/")
    return f"{prefix}/{slug}/{result_id}.final.json"


def pair_slug(harness: str, industry: str) -> str:
    return (
        f"{harness.replace('/', '-')}-{industry}"
        .replace("_", "-")
        .replace(".", "-")
        .lower()
    )


def actual_path(
    root: Path, slug: str, run_id: str, digital_human_id: str, result_id: str
) -> Path:
    return root / slug / str(run_id) / str(digital_human_id) / str(result_id) / "final.json"


def _api_key() -> str:
    key = os.environ.get("BLUEJAY_API_KEY")
    if not key:
        raise SystemExit("need BLUEJAY_API_KEY")
    return key


def _req(path: str) -> dict[str, Any]:
    req = Request(
        f"{API}/{path}",
        headers={"X-API-Key": _api_key(), "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except HTTPError as e:
        raise SystemExit(
            f"GET {path} → {e.code} {e.read()[:400].decode(errors='replace')}"
        ) from e
    except URLError as e:
        raise SystemExit(f"GET {path} → {e}") from e


def list_run_results(run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    body = _req(f"retrieve-simulation-results/{run_id}")
    run = body.get("simulation_run") or {}
    results = body.get("simulation_results") or body.get("results") or []
    if not results:
        raise SystemExit(f"no simulation results for run {run_id}")
    return run, results


def _s3_client():
    bucket = os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip()
    if not bucket:
        raise SystemExit("need MIVAS_SNAPSHOT_BUCKET")
    import boto3

    region = (
        os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "us-west-1"
    )
    return boto3.client("s3", region_name=region), bucket


def fetch_s3_json(key: str) -> dict[str, Any] | None:
    client, bucket = _s3_client()
    try:
        raw = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"} or "NoSuchKey" in type(e).__name__:
            return None
        raise
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"s3://{bucket}/{key} is not JSON: {e}") from e
    if not isinstance(state, dict):
        raise SystemExit(f"s3://{bucket}/{key} is not an object")
    return state


def pull_run(
    run_id: str,
    slug: str,
    out_root: Path,
    *,
    get_json=fetch_s3_json,
    list_results=list_run_results,
) -> dict[str, Any]:
    run, results = list_results(str(run_id))
    run_id = str(run.get("id") or run_id)
    sim_id = run.get("simulation_id")
    rows: list[dict[str, Any]] = []

    def one(result: dict[str, Any]) -> dict[str, Any]:
        result_id = result.get("id")
        dh_id = result.get("digital_human_id")
        if result_id is None or dh_id is None:
            return {
                "result_id": result_id,
                "digital_human_id": dh_id,
                "status": result.get("status"),
                "s3_key": None,
                "path": None,
                "fetched": False,
                "error": "result missing id or digital_human_id",
            }
        key = s3_final_key(slug, str(result_id))
        dest = actual_path(out_root, slug, run_id, str(dh_id), str(result_id))
        state = get_json(key)
        if state is None:
            return {
                "result_id": result_id,
                "digital_human_id": dh_id,
                "status": result.get("status"),
                "s3_key": key,
                "path": None,
                "fetched": False,
                "error": "not in S3",
            }
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        return {
            "result_id": result_id,
            "digital_human_id": dh_id,
            "status": result.get("status"),
            "s3_key": key,
            "path": str(dest.relative_to(out_root / slug / run_id)),
            "fetched": True,
            "error": None,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, result) for result in results]
        for fut in as_completed(futures):
            rows.append(fut.result())
    rows.sort(key=lambda r: (str(r.get("digital_human_id") or ""), str(r.get("result_id") or "")))

    manifest = {
        "slug": slug,
        "run_id": run_id,
        "simulation_id": sim_id,
        "count": len(rows),
        "fetched": sum(1 for r in rows if r["fetched"]),
        "missing": sum(1 for r in rows if not r["fetched"]),
        "results": rows,
    }
    run_dir = out_root / slug / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "index.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    os.environ.setdefault("MIVAS_SNAPSHOT_PREFIX", "mivas")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Bluejay simulation run id")
    parser.add_argument("--slug", help="Harness×industry slug used as the S3 key prefix")
    parser.add_argument("--harness", help="e.g. openai/realtime-2.1 (with --industry, builds --slug)")
    parser.add_argument("--industry", help="e.g. customer-support (with --harness, builds --slug)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    slug = args.slug
    if not slug:
        if not (args.harness and args.industry):
            raise SystemExit("pass --slug, or both --harness and --industry")
        slug = pair_slug(args.harness, args.industry)
    manifest = pull_run(args.run_id, slug, args.out)
    print(
        f"{slug}/{manifest['run_id']}: "
        f"{manifest['fetched']} fetched, {manifest['missing']} missing "
        f"of {manifest['count']}",
        flush=True,
    )
    for row in manifest["results"]:
        mark = "ok" if row["fetched"] else "MISSING"
        print(
            f"  {mark} dh={row['digital_human_id']} result={row['result_id']} "
            f"{row.get('error') or row.get('path')}",
            flush=True,
        )
    return 1 if manifest["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
