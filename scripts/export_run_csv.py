"""One row per simulation result: every metric plus the full digital-human definition.

Three sources, because no single one is complete:

* **built-in metrics** — `metrics[]` on the result (duration, latency percentiles, audio
  clarity/loudness/clipping/dropouts, agent + customer interruption counts, turns, wpm)
  and the evaluation object (goal_success, sentiment, redundancy, hallucination).
* **custom metrics** — the LLM judges attached to the simulation (Task completion 1-5,
  Premature call end), read from `evaluations[].custom_metrics` / `custom_evals`.
* **off-platform, computed here** — connection rate, tool-call adherence and handoff
  adherence.

Tool evidence comes from the **trace**, not from `tool_calls`. Bluejay extracts spans into
`tool_calls` in a single unretried read at POST time, so for the tail of a large run the
field is empty while the trace holds every `execute_tool` span (see D14 in
docs/healthcare/TRIAGE_LEDGER.md). `--tools-from` selects the source:
`trace` (default, authoritative), `api` (what the judge saw), or `both` (emits each
separately so you can see the gap).

    uv run python scripts/export_run_csv.py 229091 -o docs/healthcare/run_229091.csv
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
TERMINAL = {"COMPLETED", "FAILED", "NO_ANSWER", "NO_CONNECTION", "CANCELLED",
            "SYSTEM_ERROR", "ERROR"}
# a call that never reached the agent — these are what connection rate measures
NOT_CONNECTED = {"NO_ANSWER", "NO_CONNECTION", "SYSTEM_ERROR", "ERROR", "CANCELLED"}
HANDOFF_TOOLS = {"transfer_to_identity", "transfer_to_scheduling", "transfer_to_coverage",
                 "transfer_to_cosmetic", "transfer_to_billing", "transfer_to_clinical",
                 "transfer_to_human"}
# built-ins worth a column, in a sensible reading order
BUILTIN = [
    "duration", "num_turns", "interface",
    "agent_interruption_count", "customer_interruption_count",
    "avg_agent_latency", "max_agent_latency", "p50_agent_latency", "p90_agent_latency",
    "p95_agent_latency", "p99_agent_latency",
    "avg_customer_latency", "p50_customer_latency", "p90_customer_latency",
    "avg_punctuation_latency", "p50_punctuation_latency", "p90_punctuation_latency",
    "time_to_first_agent_utterance",
    "agent_audio_clarity", "agent_perceived_loudness", "agent_audio_clipping",
    "agent_pitch_variability", "agent_audio_dropouts", "pronunciation",
    "agent_turn_duration_avg", "agent_words_per_turn_avg", "customer_words_per_turn_avg",
    "agent_wpm", "agent_speak_percentage", "success", "redundancy",
]
DH_FIELDS = [
    "id", "name", "test_name", "intent", "success_criteria", "tags",
    "accent", "gender", "language", "fluency", "voice_speed", "verbosity",
    "creativity", "audio_quality", "background_noise", "background_noise_volume",
    "interruptions", "speaks_first_config", "allow_dtmf_tool", "allow_end_call_tool",
    "allow_silence_tool", "num_runs", "phone_number", "role_description",
    "hangup_phrases", "silence_timeout", "scripted_responses", "traits",
    "expected_tool_calls",
]


def _get(path: str, method: str = "GET") -> dict:
    key = os.environ.get("BLUEJAY_API_KEY")
    if not key:
        raise SystemExit("need BLUEJAY_API_KEY")
    # /v1/traces/{id} is a POST (GET returns 405), everything else here is a GET
    req = urllib.request.Request(
        f"{API}/{path}",
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} → {e.code} {e.read()[:300].decode(errors='replace')}")


def trace_tools(trace_ids: list[str]) -> list[tuple[str, str]]:
    """(timestamp, tool_name) for every execute_tool span, chronological.

    ClickHouse hands back an ISO-8601 timestamp string ("2026-08-13T02:30:12.148990000Z"),
    not epoch millis; lexical order on that format is chronological order.
    """
    out: list[tuple[str, str]] = []
    for tid in trace_ids or []:
        try:
            body = _get(f"traces/{tid}", method="POST")
        except SystemExit:
            continue
        try:
            rows = body["data"]["data"]["results"][0]["rows"]
        except (KeyError, IndexError, TypeError):
            continue
        for row in rows:
            d = row.get("data") or {}
            name = str(d.get("name") or "")
            if name.startswith("execute_tool "):
                out.append((str(d.get("timestamp") or ""), name.split(" ", 1)[1]))
    out.sort()
    return out


def expected_handoff_path(traits: list) -> list[str]:
    """The `expected_handoff_path` trait, stored as a python list literal."""
    for t in traits or []:
        if (t.get("trait_name") or "") == "expected_handoff_path":
            v = t.get("value")
            if isinstance(v, list):
                return [str(x) for x in v]
            try:
                parsed = ast.literal_eval(str(v))
                return [str(x) for x in parsed] if isinstance(parsed, list) else []
            except (ValueError, SyntaxError):
                return []
    return []


def handoff_adherence(expected: list[str], actual_handoffs: list[str]) -> tuple[str, float]:
    """Did the agent walk the expected multi-agent path?

    Subsequence, not equality: an agent may legitimately revisit a node, but the expected
    hops must appear in the expected ORDER. An empty expected path is adherent only if the
    agent made no handoffs (the off-rails cases must not transfer).
    """
    if not expected:
        return ("exact" if not actual_handoffs else "unexpected_handoffs",
                1.0 if not actual_handoffs else 0.0)
    i = 0
    for h in actual_handoffs:
        if i < len(expected) and h == expected[i]:
            i += 1
    matched = i / len(expected)
    if actual_handoffs == expected:
        return "exact", 1.0
    if i == len(expected):
        return "in_order_with_extras", matched
    return "incomplete", matched


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run")
    p.add_argument("--sim", default="30315")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--tools-from", choices=["trace", "api", "both"], default="both")
    p.add_argument("--workers", type=int, default=12)
    args = p.parse_args()

    # The bulk endpoint under-reports tool_calls for the tail of a large run (D14), so it is
    # used only to enumerate ids; every row's data comes from the per-result endpoint.
    listing = _get(f"retrieve-simulation-results/{args.run}").get("simulation_results", [])
    ids = [stub.get("id") for stub in listing if stub.get("id") is not None]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_get, f"retrieve-simulation-result/{rid}"): rid for rid in ids}
        by_id: dict = {}
        for fut in as_completed(futs):
            rid = futs[fut]
            stub = next(s for s in listing if s.get("id") == rid)
            by_id[rid] = fut.result().get("simulation_result") or stub
        results = [by_id[rid] for rid in ids]
    dhs = {d["id"]: d for d in _get(f"digital-humans-by-simulation/{args.sim}").get("digital_humans", [])}
    print(f"run {args.run}: {len(results)} results, {len(dhs)} digital humans", file=sys.stderr)

    # prefetch traces so tool extraction is not serial across 180 calls
    trace_cache: dict[str, list[tuple[str, str]]] = {}
    if args.tools_from in ("trace", "both"):
        tids = sorted({tid for r in results for tid in (r.get("trace_ids") or [])})
        def _one_trace(tid: str) -> tuple[str, list[tuple[str, str]]]:
            return tid, trace_tools([tid])
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for tid, pairs in pool.map(_one_trace, tids):
                trace_cache[tid] = pairs
        print(f"  fetched {len(tids)} traces", file=sys.stderr)

    rows = []
    for r in results:
        dh = dhs.get(r.get("digital_human_id")) or {}
        status = str(r.get("status") or "")
        groups = r.get("tool_calls") or []
        evs = r.get("evaluations") or []
        ev = evs[0] if isinstance(evs, list) and evs else (evs if isinstance(evs, dict) else {})

        expected_tools = sorted({g["name"] for g in groups if g.get("expected")})
        api_tools = [g["name"] for g in groups if g.get("actual")]
        if args.tools_from in ("trace", "both"):
            tr_pairs = [p for tid in (r.get("trace_ids") or []) for p in trace_cache.get(tid, [])]
            tr_pairs.sort()
        else:
            tr_pairs = []
        tr_tools = [n for _, n in tr_pairs]

        chosen = tr_tools if args.tools_from in ("trace", "both") and tr_tools else api_tools
        hits = sorted(set(expected_tools) & set(chosen))
        missing = sorted(set(expected_tools) - set(chosen))
        adherence = len(hits) / len(expected_tools) if expected_tools else ""

        exp_path = expected_handoff_path(dh.get("traits") or [])
        actual_handoffs = [n for n in chosen if n in HANDOFF_TOOLS]
        ho_verdict, ho_score = handoff_adherence(exp_path, actual_handoffs)

        row = {
            "run_id": args.run,
            "result_id": r.get("id"),
            "case": (dh.get("name") or "?").split()[0],
            "status": status,
            # off-platform: connection rate is the mean of this column
            "connected": 0 if status in NOT_CONNECTED else 1,
            "goal_success": ev.get("goal_success"),
            "goal_reasoning": (ev.get("goal_reasoning") or "").replace("\n", " "),
            # off-platform: tool-call adherence
            "expected_tools": ";".join(expected_tools),
            "tools_hit": ";".join(hits),
            "tools_missing": ";".join(missing),
            "tool_call_adherence": adherence,
            "n_traces": len(r.get("trace_ids") or []),
            "n_tools_trace": len(tr_tools),
            "n_tools_api": len(api_tools),
            "tool_extraction_gap": 1 if (tr_tools and not api_tools) else 0,
            # off-platform: handoff adherence
            "expected_handoff_path": ";".join(exp_path),
            "actual_handoff_path": ";".join(actual_handoffs),
            "handoff_adherence_verdict": ho_verdict,
            "handoff_adherence_score": ho_score,
            "tool_sequence_trace": ";".join(tr_tools),
        }
        for m in (r.get("metrics") or []):
            name = m.get("name")
            if name in BUILTIN:
                row[f"builtin_{name}"] = m.get("value")
        for name in BUILTIN:
            row.setdefault(f"builtin_{name}", "")
        row["builtin_duration_s"] = (row.get("builtin_duration") or 0) / 1000 if row.get("builtin_duration") else r.get("duration") or ""
        for k in ("sentiment_label", "sentiment_score", "hallucination", "redundancy",
                  "pronunciation_score", "agent_audio_clarity", "user_audio_clarity",
                  "agent_speak_percentage", "avg_agent_latency", "call_summary"):
            v = ev.get(k)
            row[f"eval_{k}"] = (str(v).replace("\n", " ") if k == "call_summary" else v)

        # custom metrics: the platform reports them under a couple of shapes
        cm = ev.get("custom_metrics") or ev.get("custom_evals") or r.get("custom_metrics") or []
        if isinstance(cm, dict):
            cm = [dict(v, name=k) if isinstance(v, dict) else {"name": k, "value": v}
                  for k, v in cm.items()]
        for entry in cm if isinstance(cm, list) else []:
            if not isinstance(entry, dict):
                continue
            nm = str(entry.get("metric_name") or entry.get("name") or "custom").strip()
            # the platform reports the score in `response_value`, with a typed mirror
            # keyed by response type (`yes_no_response`, `quantitative_response`, …).
            # `value` does not exist on these rows — reading it silently produced 60
            # empty custom-metric cells on the first export of run 229139.
            val = entry.get("response_value")
            if val is None:
                for key in ("yes_no_response", "quantitative_response", "pass_fail_response",
                            "enum_response", "qualitative_response", "json_response",
                            "int_value", "float_value", "boolean_value", "enum_value",
                            "qualitative_value", "json_value", "value"):
                    if entry.get(key) is not None:
                        val = entry[key]
                        break
            if entry.get("is_not_applicable"):
                val = "n/a"
            slug = nm.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("-", "_")
            row[f"custom_{slug}"] = val
            row[f"custom_{slug}_reasoning"] = str(entry.get("reasoning") or "").replace("\n", " ")

        for f in DH_FIELDS:
            v = dh.get(f)
            row[f"dh_{f}"] = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
        rows.append(row)

    if not rows:
        raise SystemExit("no results")
    cols: list[str] = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols.append(k)
    out = pathlib.Path(args.out) if args.out else pathlib.Path(f"run_{args.run}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in sorted(rows, key=lambda x: str(x["case"])):
            w.writerow(row)

    # run-level rollups: the aggregate forms of the off-platform metrics
    n = len(rows)
    conn = sum(r["connected"] for r in rows)
    durs = [r["builtin_duration_s"] for r in rows if isinstance(r.get("builtin_duration_s"), (int, float))]
    adh = [r["tool_call_adherence"] for r in rows if isinstance(r.get("tool_call_adherence"), float)]
    ho = [r["handoff_adherence_score"] for r in rows if isinstance(r.get("handoff_adherence_score"), float)]
    barge = [r["builtin_agent_interruption_count"] for r in rows
             if isinstance(r.get("builtin_agent_interruption_count"), (int, float))]
    gaps = sum(r["tool_extraction_gap"] for r in rows)
    print(f"\nwrote {out}  ({n} rows x {len(cols)} columns)")
    print(f"  connection rate         {conn}/{n} = {conn/n*100:.0f}%")
    if durs:
        print(f"  avg call duration       {sum(durs)/len(durs):.0f}s")
    if adh:
        print(f"  tool call adherence     {sum(adh)/len(adh)*100:.0f}% (mean per call)")
    if ho:
        print(f"  handoff adherence       {sum(ho)/len(ho)*100:.0f}% (mean), "
              f"{sum(1 for r in rows if r['handoff_adherence_verdict']=='exact')}/{n} exact")
    if barge:
        print(f"  agent barge-ins         {sum(barge)} total, {sum(barge)/len(barge):.2f}/call")
    if gaps:
        print(f"  NOTE {gaps} result(s) had tools in the trace but none in the API "
              f"(D14 extraction gap) — trace used")
    return 0


if __name__ == "__main__":
    sys.exit(main())
