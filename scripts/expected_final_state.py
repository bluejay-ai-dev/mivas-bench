"""Replay each digital human's expected tool calls onto a fresh seeded DB.

Hangup freezes GET /state to S3 as `{id}.final.json`. This script produces the
matching expected dump: copy schema+seed, POST /tools/{name} in declared order
(skipping harness-native handoff tools and `end_call`), then write GET /state.
Human-transfer session tools still POST so escalations land in the dump.

    uv run python scripts/expected_final_state.py                  # all v2 packs
    uv run python scripts/expected_final_state.py --industry customer-support
    uv run python scripts/expected_final_state.py --from-spec --industry finance
    uv run python scripts/expected_final_state.py --from-json path.json --industry legal
    uv run python scripts/expected_final_state.py --compare expected.json actual.json
    uv run python scripts/expected_final_state.py --compare-dir expected-final-state/customer-support ./actuals
    uv run python scripts/expected_final_state.py --compare-actuals actual-final-state/openai-realtime-2-1-customer-support/RUN_ID --industry customer-support
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
INDUSTRY_ROOT = ROOT / "industries"
DEFAULT_OUT = ROOT / "expected-final-state"
API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")

# Live v2 communities (one 66-case pack per scored industry).
V2_COMMUNITIES = {
    "healthcare": "6932c5b5-baba-4ce9-885c-2594aa8a98c8",
    "finance": "aec64c79-5da2-403a-b33e-6ba00134d62b",
    "legal": "e18f7c9e-d51c-4f3b-be88-b032edcb7d17",
    "customer-support": "4d6de8c5-5b3e-4a36-a031-df2f73bb1c33",
    "travel": "8fbf3bc4-2d2a-4428-af7d-45f3da4b3504",
}

LOCAL_SPECS = {
    "healthcare": ROOT / "scripts" / "healthcare_digital_humans.py",
    "finance": ROOT / "scripts" / "finance_digital_humans.py",
    "legal": ROOT / "scripts" / "legal_digital_humans.py",
}

# sqlite DEFAULT (datetime('now')) and equivalent write-time clocks.
VOLATILE_KEYS = frozenset({"created_at"})

# Fill these from the previous industry-tool response when the next call omits them.
CARRY_FORWARD = frozenset({
    "confirmation_token", "rma_number", "token", "hold_id",
})

_CALL_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CASE_KEY = re.compile(r"^[A-Z]+\d*-[A-Z0-9]+(?:-[A-Z0-9]+)*$")


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


def unwrap_dh(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("digital_human"), dict):
        return payload["digital_human"]
    return payload


def trait(dh: dict[str, Any], name: str) -> str | None:
    for item in dh.get("traits") or []:
        if item.get("trait_name") == name:
            value = item.get("value")
            return None if value is None else str(value)
    return None


def case_key(dh: dict[str, Any]) -> str:
    keyed = trait(dh, "case_key")
    if keyed:
        return keyed
    test_name = str(dh.get("test_name") or "")
    if ":" in test_name:
        prefix = test_name.split(":", 1)[0].strip()
        if _CASE_KEY.match(prefix):
            return prefix
    first = str(dh.get("name") or "").split()[0] if dh.get("name") else ""
    if _CASE_KEY.match(first):
        return first
    dh_id = dh.get("id")
    if dh_id is not None:
        return f"dh-{dh_id}"
    raise ValueError("digital human has no case_key, test_name, name, or id")


def call_id_for(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", key)[:60].strip("-") or "case"
    cid = f"exp-{safe}"
    if not _CALL_ID_OK.fullmatch(cid):
        cid = f"exp-{abs(hash(key)) % 10**12}"
    return cid


def tool_flags(industry: str) -> dict[str, dict[str, Any]]:
    bp = json.loads((INDUSTRY_ROOT / industry / "agent_blueprint.json").read_text())
    flags: dict[str, dict[str, Any]] = {}
    for agent in bp["agents"]:
        for tool in agent["tools"]:
            flags.setdefault(tool["name"], tool)
    return flags


def is_harness_native(name: str, flags: dict[str, dict[str, Any]]) -> bool:
    """Handoffs and `end_call` never hit the industry server.

    Human-transfer session tools (`escalate_to_human`, `transfer_to_human`)
    still POST — they write escalations — then the harness hangs up.
    """
    spec = flags.get(name) or {}
    if spec.get("handoff"):
        return True
    return name == "end_call"


def canonical_state(value: Any) -> Any:
    """Drop write-time clocks so expected dumps compare to S3 snapshots."""
    if isinstance(value, dict):
        return {k: canonical_state(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [canonical_state(item) for item in value]
    return value


def states_match(expected: Any, actual: Any) -> bool:
    return canonical_state(expected) == canonical_state(actual)


def _args(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("parameters")
    return dict(raw) if isinstance(raw, dict) else {}


def _carry(args: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, Any]:
    if not prior:
        return args
    data = prior.get("data")
    if not isinstance(data, dict):
        return args
    filled = dict(args)
    for key in CARRY_FORWARD:
        if filled.get(key) in (None, "") and data.get(key) not in (None, ""):
            filled[key] = data[key]
    return filled


@contextmanager
def load_tool_server(industry: str) -> Iterator[Any]:
    """Import industries/<industry>/tool_server.py against a throwaway data dir."""
    original = os.environ.get("MIVAS_DB_PATH")
    original_shared = os.environ.get("MIVAS_DB_SHARED")
    with tempfile.TemporaryDirectory(prefix=f"mivas-expected-{industry}-") as tmp:
        os.environ["MIVAS_DB_PATH"] = str(Path(tmp) / "runtime.db")
        os.environ.pop("MIVAS_DB_SHARED", None)
        name = f"expected_tool_server_{industry.replace('-', '_')}"
        sys.modules.pop(name, None)
        try:
            spec = importlib.util.spec_from_file_location(
                name, INDUSTRY_ROOT / industry / "tool_server.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            yield module
        finally:
            sys.modules.pop(name, None)
            if original is None:
                os.environ.pop("MIVAS_DB_PATH", None)
            else:
                os.environ["MIVAS_DB_PATH"] = original
            if original_shared is None:
                os.environ.pop("MIVAS_DB_SHARED", None)
            else:
                os.environ["MIVAS_DB_SHARED"] = original_shared


def replay_case(
    client: TestClient,
    dh: dict[str, Any],
    flags: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    key = case_key(dh)
    cid = call_id_for(key)
    headers = {"X-Mivas-Call-Id": cid}
    replayed: list[dict[str, Any]] = []
    skipped: list[str] = []
    prior: dict[str, Any] | None = None

    for call in dh.get("expected_tool_calls") or []:
        name = str(call.get("name") or "").strip()
        if not name:
            continue
        if is_harness_native(name, flags):
            skipped.append(name)
            continue
        args = _carry(_args(call), prior)
        resp = client.post(f"/tools/{name}", json={"arguments": args}, headers=headers)
        body: dict[str, Any]
        try:
            parsed = resp.json()
            body = parsed if isinstance(parsed, dict) else {"raw": parsed}
        except ValueError:
            body = {"raw": resp.text}
        replayed.append({
            "name": name,
            "arguments": args,
            "status_code": resp.status_code,
            "ok": body.get("ok", body.get("success")),
            "error_code": body.get("error_code"),
        })
        prior = body if resp.status_code == 200 else None

    state_resp = client.get("/state", headers=headers)
    if state_resp.status_code != 200:
        raise RuntimeError(f"{key}: GET /state → {state_resp.status_code} {state_resp.text}")
    state = state_resp.json()
    if not isinstance(state, dict):
        raise RuntimeError(f"{key}: GET /state was not an object")
    return {
        "case_key": key,
        "call_id": cid,
        "digital_human_id": dh.get("id"),
        "name": dh.get("name"),
        "test_name": dh.get("test_name"),
        "replayed": replayed,
        "skipped": skipped,
        "state": canonical_state(state),
    }


def load_from_spec(industry: str) -> list[dict[str, Any]]:
    path = LOCAL_SPECS.get(industry)
    if path is None or not path.is_file():
        raise SystemExit(f"no local spec for {industry}")
    name = f"expected_spec_{industry.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return [unwrap_dh(item) for item in module.build()]


def load_from_json(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict) and isinstance(raw.get("digital_humans"), list):
        raw = raw["digital_humans"]
    if not isinstance(raw, list):
        raise SystemExit(f"{path} must be a list of digital humans")
    return [unwrap_dh(item) for item in raw]


def load_from_community(community_id: str) -> list[dict[str, Any]]:
    body = _req(f"community/{community_id}")
    ids = body.get("digital_human_ids") or (body.get("data") or {}).get("digital_human_ids") or []
    if not ids:
        raise SystemExit(f"community {community_id} has no digital humans")

    def fetch(dh_id: Any) -> dict[str, Any]:
        return unwrap_dh(_req(f"digital-human/{dh_id}"))

    out: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch, dh_id): dh_id for dh_id in ids}
        for fut in as_completed(futures):
            out.append(fut.result())
    out.sort(key=lambda dh: (case_key(dh), str(dh.get("id") or "")))
    return out


def write_industry(
    industry: str,
    humans: list[dict[str, Any]],
    out_dir: Path,
) -> dict[str, Any]:
    flags = tool_flags(industry)
    dest = out_dir / industry
    dest.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    with load_tool_server(industry) as module, TestClient(module.app) as client:
        for dh in humans:
            result = replay_case(client, dh, flags)
            path = dest / f"{result['case_key']}.final.json"
            path.write_text(json.dumps(result["state"], indent=2, sort_keys=True) + "\n")
            index.append({
                "case_key": result["case_key"],
                "digital_human_id": result["digital_human_id"],
                "name": result["name"],
                "test_name": result["test_name"],
                "path": path.name,
                "replayed": result["replayed"],
                "skipped": result["skipped"],
            })
            print(f"{industry}/{result['case_key']}: "
                  f"{len(result['replayed'])} tools, {len(result['skipped'])} skipped",
                  flush=True)
    manifest = {
        "industry": industry,
        "count": len(index),
        "cases": index,
    }
    (dest / "index.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def compare_files(expected_path: Path, actual_path: Path) -> int:
    expected = json.loads(expected_path.read_text())
    actual = json.loads(actual_path.read_text())
    if states_match(expected, actual):
        print(f"match {expected_path.name}")
        return 0
    print(f"MISMATCH {expected_path} vs {actual_path}")
    return 1


INDUSTRY_NAMES = (
    "control-industry",
    "customer-support",
    "healthcare",
    "finance",
    "legal",
    "travel",
)


def industry_from_slug(slug: str) -> str | None:
    for name in sorted(INDUSTRY_NAMES, key=len, reverse=True):
        if slug == name or slug.endswith(f"-{name}"):
            return name
    return None


def load_expected_index(expected_dir: Path) -> dict[str, dict[str, Any]]:
    """digital_human_id (str) → case row, including resolved expected path."""
    index_path = expected_dir / "index.json"
    if not index_path.is_file():
        raise SystemExit(f"no {index_path}")
    body = json.loads(index_path.read_text())
    by_dh: dict[str, dict[str, Any]] = {}
    for case in body.get("cases") or []:
        dh_id = case.get("digital_human_id")
        if dh_id is None:
            continue
        row = dict(case)
        row["expected_path"] = expected_dir / case["path"]
        by_dh[str(dh_id)] = row
    return by_dh


def iter_actual_finals(actuals_dir: Path) -> list[dict[str, Any]]:
    """Walk slug/run/dh/result/final.json (or a suffix of that tree)."""
    found: list[dict[str, Any]] = []
    for path in sorted(actuals_dir.rglob("final.json")):
        result_id = path.parent.name
        dh_id = path.parent.parent.name
        found.append({
            "path": path,
            "result_id": result_id,
            "digital_human_id": dh_id,
        })
    return found


def compare_actuals(
    expected_dir: Path,
    actuals_dir: Path,
    *,
    report_path: Path | None = None,
) -> dict[str, Any]:
    by_dh = load_expected_index(expected_dir)
    actuals = iter_actual_finals(actuals_dir)
    if not actuals:
        raise SystemExit(f"no final.json under {actuals_dir}")

    cases: dict[str, dict[str, Any]] = {}
    unknown = 0
    for item in actuals:
        dh_id = item["digital_human_id"]
        spec = by_dh.get(dh_id)
        if spec is None:
            unknown += 1
            print(f"UNKNOWN dh={dh_id} result={item['result_id']} (not in expected index)")
            continue
        case_id = spec["case_key"]
        bucket = cases.setdefault(case_id, {
            "case_key": case_id,
            "digital_human_id": spec["digital_human_id"],
            "name": spec.get("name"),
            "test_name": spec.get("test_name"),
            "expected": spec["path"],
            "results": [],
            "matched": 0,
            "mismatched": 0,
        })
        expected = json.loads(spec["expected_path"].read_text())
        actual = json.loads(item["path"].read_text())
        matched = states_match(expected, actual)
        bucket["results"].append({
            "result_id": item["result_id"],
            "path": str(item["path"]),
            "match": matched,
        })
        if matched:
            bucket["matched"] += 1
            print(f"match   {case_id} dh={dh_id} result={item['result_id']}")
        else:
            bucket["mismatched"] += 1
            print(f"MISMATCH {case_id} dh={dh_id} result={item['result_id']}")

    actual_dhs = {a["digital_human_id"] for a in actuals}
    missing_expected = [
        spec["case_key"] for dh_id, spec in by_dh.items() if dh_id not in actual_dhs
    ]

    case_rows = [cases[k] for k in sorted(cases)]
    report = {
        "expected_dir": str(expected_dir),
        "actuals_dir": str(actuals_dir),
        "cases": case_rows,
        "case_count": len(case_rows),
        "result_count": sum(c["matched"] + c["mismatched"] for c in case_rows),
        "matched": sum(c["matched"] for c in case_rows),
        "mismatched": sum(c["mismatched"] for c in case_rows),
        "unknown_digital_humans": unknown,
        "cases_with_no_actuals": missing_expected,
        "cases_all_match": sum(1 for c in case_rows if c["mismatched"] == 0 and c["matched"]),
        "cases_any_mismatch": sum(1 for c in case_rows if c["mismatched"]),
    }
    dest = report_path or (actuals_dir / "report.json")
    dest.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"{report['matched']} matched, {report['mismatched']} mismatched "
        f"across {report['result_count']} results / {report['case_count']} cases "
        f"({report['cases_all_match']} cases all-match, "
        f"{report['cases_any_mismatch']} cases with a fail)"
    )
    if missing_expected:
        print(f"{len(missing_expected)} expected cases had no actual dump")
    print(f"report {dest}")
    return report


def compare_dirs(expected_dir: Path, actual_dir: Path) -> int:
    expected_files = sorted(p for p in expected_dir.glob("*.final.json"))
    if not expected_files:
        raise SystemExit(f"no *.final.json in {expected_dir}")
    failed = 0
    missing = 0
    for path in expected_files:
        actual = actual_dir / path.name
        if not actual.is_file():
            print(f"MISSING {actual}")
            missing += 1
            continue
        failed += compare_files(path, actual)
    print(f"{len(expected_files) - failed - missing} matched, "
          f"{failed} mismatched, {missing} missing")
    return 1 if failed or missing else 0


def _humans_for(industry: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.from_json:
        return load_from_json(Path(args.from_json))
    if args.from_spec:
        return load_from_spec(industry)
    community = args.community or V2_COMMUNITIES.get(industry)
    if not community:
        raise SystemExit(
            f"{industry} has no default community; pass --community, --from-spec, or --from-json"
        )
    return load_from_community(community)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--industry", action="append", dest="industries",
                        help="Industry slug. Repeatable. Default: every v2 pack.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--from-spec", action="store_true",
                        help="Load expected_tool_calls from scripts/*_digital_humans.py")
    parser.add_argument("--from-json", type=Path,
                        help="Load digital humans from a local JSON list")
    parser.add_argument("--community", help="Override the v2 community id")
    parser.add_argument("--compare", nargs=2, metavar=("EXPECTED", "ACTUAL"))
    parser.add_argument("--compare-dir", nargs=2, metavar=("EXPECTED_DIR", "ACTUAL_DIR"))
    parser.add_argument("--compare-actuals", type=Path,
                        help="Run folder from pull_actual_final_state.py "
                             "(…/{slug}/{run_id})")
    parser.add_argument("--report", type=Path,
                        help="Where to write the --compare-actuals report.json")
    args = parser.parse_args(argv)

    if args.compare:
        return compare_files(Path(args.compare[0]), Path(args.compare[1]))
    if args.compare_dir:
        return compare_dirs(Path(args.compare_dir[0]), Path(args.compare_dir[1]))
    if args.compare_actuals:
        actuals = args.compare_actuals
        industry = None
        if args.industries:
            if len(args.industries) != 1:
                raise SystemExit("--compare-actuals takes exactly one --industry")
            industry = args.industries[0]
        else:
            for part in reversed(actuals.parts):
                industry = industry_from_slug(part)
                if industry:
                    break
        if not industry:
            raise SystemExit("pass --industry or include the pair slug in the path")
        expected_dir = Path(args.out) / industry
        report = compare_actuals(expected_dir, actuals, report_path=args.report)
        return 1 if report["mismatched"] or report["unknown_digital_humans"] else 0

    industries = args.industries or list(V2_COMMUNITIES)
    if args.from_json and len(industries) != 1:
        raise SystemExit("--from-json requires exactly one --industry")
    if args.community and len(industries) != 1:
        raise SystemExit("--community requires exactly one --industry")

    for industry in industries:
        if not (INDUSTRY_ROOT / industry / "tool_server.py").is_file():
            raise SystemExit(f"no tool_server.py for {industry}")
        humans = _humans_for(industry, args)
        if not humans:
            raise SystemExit(f"no digital humans for {industry}")
        write_industry(industry, humans, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
