"""Evidence bundle per failed case, so attribution rests on data and not on the judge.

The judge's reasoning is the thing most often wrong (it asserts a tool was not called while
the result records it), so a bundle carries the transcript, the digital human's intent and
criteria, the expected-vs-actual tool pairing and the trace's own tool sequence next to it.

    uv run python verifiers/attribution_bundle.py 230087 -o docs/finance/bundles
    uv run python verifiers/attribution_bundle.py 230087 --only F17 F31
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")


def _key() -> str:
    k = os.environ.get("BLUEJAY_API_KEY")
    if not k:
        raise SystemExit("need BLUEJAY_API_KEY")
    return k


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{API}/{path}", headers={"X-API-Key": _key()})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _fetch_url(url: str):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    except Exception:
        return None


def transcript_lines(result: dict) -> list[str]:
    url = result.get("transcript_url")
    data = _fetch_url(url) if url else None
    if data is None:
        return []
    items = data if isinstance(data, list) else (
        data.get("transcript") or data.get("messages") or [])
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        who = (m.get("role") or m.get("speaker") or "?").upper()
        said = m.get("utterance") or m.get("content") or m.get("text") or ""
        out.append(f"{who}: {said}")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_id")
    p.add_argument("-o", "--out", default="docs/finance/bundles")
    p.add_argument("--only", nargs="*", help="case keys, e.g. F17 F31")
    p.add_argument("--failures-only", action="store_true", default=True)
    p.add_argument("--all", dest="failures_only", action="store_false")
    args = p.parse_args()

    run = _get(f"retrieve-simulation-results/{args.run_id}")
    results = run.get("simulation_results") or run.get("results") or []
    ids = [r.get("id") for r in results]
    # the per-result endpoint carries only digital_human_id, so build the id -> definition
    # map once from the simulation the run belongs to
    dh_by_id: dict = {}
    for sid in (os.environ.get("MIVAS_SIM_ID") or "30311").split(","):
        for x in _get(f"digital-humans-by-simulation/{sid.strip()}").get(
                "digital_humans", []):
            dh_by_id[str(x.get("id"))] = x
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    written = 0
    for rid in ids:
        # per result, never the bulk read: the bulk endpoint truncates tool_calls
        d = (_get(f"retrieve-simulation-result/{rid}") or {}).get("simulation_result") or {}
        dh = d.get("digital_human") or dh_by_id.get(str(d.get("digital_human_id"))) or {}
        name = (dh.get("name") or "").strip()
        key = name.split()[0] if name else f"result_{rid}"
        if args.only and key not in args.only:
            continue
        ev = (d.get("evaluations") or [{}])[0]
        goal = ev.get("goal_success")
        if args.failures_only and goal is not False:
            continue

        pairs = []
        for t in d.get("tool_calls") or []:
            actual = t.get("actual") or []
            pairs.append({
                "name": t["name"],
                "expected": t.get("expected"),
                "n_actual": len(actual),
                "actual_args": [a.get("parameters") for a in actual][:4],
                "actual_out": [a.get("output") for a in actual][:2],
            })
        bundle = {
            "run_id": args.run_id,
            "result_id": rid,
            "case": key,
            "dh_name": dh.get("name"),
            "status": d.get("status"),
            "duration_s": d.get("duration"),
            "goal_success": goal,
            "goal_reasoning": ev.get("goal_reasoning"),
            "intent": dh.get("intent"),
            "success_criteria": dh.get("success_criteria"),
            "expected_handoff_path": next(
                (t.get("value") for t in (dh.get("traits") or [])
                 if t.get("trait_name") == "expected_handoff_path"), None),
            "tool_pairs": pairs,
            "tools_called_in_order": [
                p["name"] for p in pairs for _ in range(p["n_actual"])],
            "tools_missing": [p["name"] for p in pairs if p["n_actual"] == 0],
            "transcript": transcript_lines(d),
        }
        (outdir / f"{key or rid}.json").write_text(json.dumps(bundle, indent=2) + "\n")
        written += 1
    print(f"{written} bundle(s) → {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
