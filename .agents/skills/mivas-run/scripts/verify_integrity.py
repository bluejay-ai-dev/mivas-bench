#!/usr/bin/env python3
"""Phase 3: harness integrity + benchmark integrity after a passing smoke.

    uv run python .agents/skills/mivas-run/scripts/verify_integrity.py \
        --harness openai/realtime-2.1 --industry healthcare \
        --smoke-dir verify-out/smoke/openai-realtime-2-1-healthcare/RUN_ID [--k8s]

Harness integrity: repo unit tests, blueprint --check, smoke rubric all PASS,
every smoke result is scorable (verify_run.py), and a hangup snapshot exists for
every smoke conversation (S3-compatible store, or the pod's /data/calls).
Benchmark integrity (scored industries): task suite builds into digital humans
without contract errors, expected final states replay cleanly onto the pack's
seed, and the verifier tests pass. Exit 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

rows: list[tuple[str, str, str]] = []


def rec(status: str, name: str, detail: str = "") -> None:
    rows.append((status, name, detail))
    print(f"{status:<5} {name:<36} {detail}", flush=True)


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


def sh(name: str, cmd: list[str], *, ok_detail: str = "ok", timeout: int = 900) -> bool:
    # AGENTS="" so a fleet list in .env cannot override --harness/--industry inside run.py
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "AGENTS": ""})
    out = (proc.stdout + proc.stderr).strip().splitlines()
    if proc.returncode == 0:
        rec("PASS", name, ok_detail if ok_detail != "tail" else (out[-1][:100] if out else ""))
        return True
    failed = [l for l in out if l.startswith("FAILED") or "Error" in l or "error" in l][:4]
    rec("FAIL", name, " | ".join(failed or out[-2:])[:160])
    return False


def smoke_results(smoke_dir: Path) -> list[dict]:
    import score_rubric

    return [score_rubric._unwrap(json.loads(p.read_text())) for p in sorted(smoke_dir.glob("*.json")) if p.name != "smoke.json"]


def check_snapshots(results: list[dict], slug: str, k8s: bool) -> None:
    ids = [str(r.get("id")) for r in results if r.get("id") is not None]
    bucket = os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip()
    prefix = (os.environ.get("MIVAS_SNAPSHOT_PREFIX", "mivas").strip() or "mivas").strip("/")
    if bucket:
        import boto3

        region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-west-1"
        s3 = boto3.client("s3", region_name=region)
        missing = []
        for rid in ids:
            try:
                s3.head_object(Bucket=bucket, Key=f"{prefix}/{slug}/{rid}.final.json")
            except Exception:  # noqa: BLE001
                missing.append(rid)
        rec("PASS" if not missing else "FAIL", "hangup snapshots (S3)",
            f"{len(ids) - len(missing)}/{len(ids)} in s3://{bucket}/{prefix}/{slug}/" + (f" missing {missing}" if missing else ""))
        return
    if k8s:
        pods = subprocess.run(["kubectl", "get", "pods", "-l", f"mivas.slug={slug}", "-o", "name"],
                              capture_output=True, text=True).stdout.split()
        found = set()
        for pod in pods:
            ls = subprocess.run(["kubectl", "exec", pod, "--", "ls", "/data/calls"], capture_output=True, text=True).stdout
            for rid in ids:
                if f"{rid}.final.json" in ls:
                    found.add(rid)
        rec("PASS" if found == set(ids) else "FAIL", "hangup snapshots (pod /data/calls)",
            f"{len(found)}/{len(ids)} present across {len(pods)} pod(s); set MIVAS_SNAPSHOT_BUCKET before a full run")
        return
    rec("SKIP", "hangup snapshots", "no MIVAS_SNAPSHOT_BUCKET and not --k8s")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--harness", default=os.environ.get("HARNESS", "openai/realtime-2.1"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--smoke-dir", type=Path, help="results dir written by bluejay.py smoke")
    p.add_argument("--k8s", action="store_true", help="look for snapshots in the pods when no bucket is set")
    p.add_argument("--skip-tests", action="store_true")
    a = p.parse_args(argv)
    load_dotenv()
    from run import slug

    pair = slug(a.harness, a.industry)
    print(f"integrity {a.harness} × {a.industry}\n== harness", flush=True)

    if not a.skip_tests:
        sh("repo unit tests", ["uv", "run", "pytest", "tests", "-q", "-p", "no:cacheprovider"], ok_detail="tail")
    sh("blueprint --check", ["uv", "run", "python", "run.py", "--harness", a.harness, "--industry", a.industry, "--check"])

    if a.smoke_dir and a.smoke_dir.is_dir():
        results = smoke_results(a.smoke_dir)
        sh("smoke rubric", [sys.executable, str(HERE / "bluejay.py"), "score", str(a.smoke_dir)], ok_detail=f"{len(results)} calls PASS")
        run_id = None
        meta = a.smoke_dir / "smoke.json"
        if meta.is_file():
            run_id = json.loads(meta.read_text()).get("run_id")
        if run_id:
            sh("scorable (verify_run.py)", ["uv", "run", "python", "verifiers/verify_run.py", str(run_id)], ok_detail="no void results")
        else:
            rec("SKIP", "scorable (verify_run.py)", "smoke.json missing run_id")
        check_snapshots(results, pair, a.k8s)
    else:
        rec("SKIP", "smoke rubric", "pass --smoke-dir from bluejay.py smoke")

    print("== benchmark", flush=True)
    tasks = ROOT / "industries" / a.industry / "tasks"
    if not tasks.is_dir():
        rec("SKIP", "task suite", f"{a.industry} has no scored tasks (control-industry is wiring-only)")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            dh_json = Path(tmp) / "dh.json"
            proc = subprocess.run(["uv", "run", "python", "scripts/tasks_to_digital_humans.py", "--industry", a.industry, "--json"],
                                  cwd=str(ROOT), capture_output=True, text=True)
            if proc.returncode == 0:
                dh_json.write_text(proc.stdout)
                parsed = json.loads(proc.stdout)
                n = len(parsed.get("digital_humans", parsed) if isinstance(parsed, dict) else parsed)
                rec("PASS", "tasks → digital humans", f"{n} cases pass the pack contract check")
                sh("expected final states replay", ["uv", "run", "python", "verifiers/expected_final_state.py",
                    "--industry", a.industry, "--from-json", str(dh_json), "--out", str(Path(tmp) / "expected")],
                   ok_detail="every task's exp_tool_calls replay onto seed")
            else:
                rec("FAIL", "tasks → digital humans", (proc.stderr or proc.stdout).strip().splitlines()[-1][:150])
        if not a.skip_tests:
            sh("verifier tests", ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider",
                "tests/test_expected_final_state.py", "tests/test_verify_task_run.py",
                "tests/test_tasks_to_digital_humans.py", "tests/test_industry_contract.py",
                "tests/test_tool_server.py", "tests/test_snapshot.py", "tests/test_snapshot_coverage.py"], ok_detail="tail")

    fails = [r for r in rows if r[0] == "FAIL"]
    print(f"\n{len(fails)} FAIL · {sum(r[0] == 'SKIP' for r in rows)} SKIP · {sum(r[0] == 'PASS' for r in rows)} PASS", flush=True)
    print("INTEGRITY PASS" if not fails else "INTEGRITY FAIL", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
