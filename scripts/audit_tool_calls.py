#!/usr/bin/env python3
"""Audit a run's tool-call reporting against the tool server's own logs.

The tool server logs every HTTP dispatch (`POST /tools/{name}`), so it is the
ground truth for "did the model actually call this tool". This compares that
against what Bluejay recorded in each simulation result's `actual` array.

    python scripts/audit_tool_calls.py --run 231069 --selector mivas.slug=nvidia-nemotron-healthcare

A clean run prints `unaccounted dispatches: 0`. Anything else means we are
losing tool calls between the model and Bluejay.

NB: transfer_* and end_call are handled in-process by the CHIRP bridge and
never hit HTTP, so they legitimately appear in Bluejay with no dispatch. Only
the other direction (dispatched but not recorded) is a bug.
"""
import argparse, json, os, re, subprocess, sys, urllib.request
from collections import Counter

API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")


def _get(path):
    req = urllib.request.Request(f"{API}/{path}", headers={"X-API-Key": os.environ["BLUEJAY_API_KEY"]})
    return json.load(urllib.request.urlopen(req, timeout=90))


def recorded(run):
    """What Bluejay says each call actually invoked."""
    per_call, totals = [], Counter()
    for stub in _get(f"retrieve-simulation-results/{run}").get("simulation_results", []):
        r = _get(f"retrieve-simulation-result/{stub['id']}").get("simulation_result") or stub
        names = []
        for t in r.get("tool_calls") or []:
            names += [t["name"]] * len(t.get("actual") or [])
        totals.update(names)
        per_call.append((stub["id"], r.get("status"), r.get("duration"), names))
    return per_call, totals


def dispatched(selector, since):
    """What the tool server actually executed, attributed per call.

    The bridge emits `tool_post sim=<result_id> ... path=/tools/<name>` before
    each dispatch, so we key off that rather than the bare access log. Pods host
    several calls at once, so a pod+timestamp heuristic cannot tell co-tenants
    apart, and `--since` alone can drag in the previous run's dispatches.
    """
    pods = subprocess.run(["kubectl", "get", "pods", "-l", selector, "-o", "name"],
                          capture_output=True, text=True).stdout.split()
    per_call = {}
    for pod in pods:
        logs = subprocess.run(["kubectl", "logs", pod.split("/")[-1], f"--since={since}"],
                              capture_output=True, text=True).stdout
        for rid, name in re.findall(r"tool_post sim=(\d+).*?path=/tools/(\w+)", logs):
            per_call.setdefault(rid, Counter())[name] += 1
    return per_call, len(pods)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--selector", default="mivas.slug=nvidia-nemotron-healthcare")
    p.add_argument("--since", default="4h")
    a = p.parse_args()

    per_call, _ = recorded(a.run)
    disp, n_pods = dispatched(a.selector, a.since)
    if not n_pods:
        sys.exit(f"no pods matched selector {a.selector!r} — nothing to audit against")

    print(f"=== per-call tool usage · run {a.run} ===")
    print(f"  {'result':<9}{'status':<12}{'dur':>5}  {'disp':>4} {'rec':>4}  tools")
    unaccounted = 0
    for rid, status, dur, names in sorted(per_call, key=lambda x: -len(x[3])):
        executed = disp.get(str(rid), Counter())
        # transfer_* and end_call are handled in-process and never hit HTTP, so
        # only dispatched-but-unrecorded counts as a lost tool call.
        lost = sum(max(0, n - names.count(t)) for t, n in executed.items())
        unaccounted += lost
        flag = f"   <<< {lost} LOST" if lost else ""
        print(f"  {rid:<9}{status:<12}{str(dur):>5}  {sum(executed.values()):>4} {len(names):>4}  "
              f"{', '.join(names) or '(none)'}{flag}")

    print(f"\ntotal dispatches attributed: {sum(sum(c.values()) for c in disp.values())}")
    print(f"unaccounted dispatches: {unaccounted}")
    return 1 if unaccounted else 0


if __name__ == "__main__":
    sys.exit(main())
