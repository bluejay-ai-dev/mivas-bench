#!/usr/bin/env python3
"""Phase-0/1 gate: is this machine, this pair, and this deployment ready for Bluejay?

    uv run python .claude/skills/mivas-run/scripts/preflight.py --harness openai/realtime-2.1 --industry control-industry
    uv run python .claude/skills/mivas-run/scripts/preflight.py ... --k8s                 # pods + wss probe via MIVAS_BASE_DOMAIN
    uv run python .claude/skills/mivas-run/scripts/preflight.py ... --url wss://HOST      # probe an explicit URL (tunnel, Baseten proxy)

Prints one PASS/FAIL/WARN/SKIP line per check; exit 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))

PROVIDER_KEYS = {
    "openai": [["OPENAI_API_KEY"]],
    "gemini": [["GOOGLE_API_KEY"], ["LIVEKIT_URL"], ["LIVEKIT_API_KEY"], ["LIVEKIT_API_SECRET"]],
    "aws": [["AWS_ACCESS_KEY_ID", "AWS_PROFILE"], ["AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"]],
    "grok": [["GROK_API_KEY", "XAI_API_KEY"]],
    "qwen": [["DASHSCOPE_API_KEY", "QWEN_API_KEY"]],
    "nvidia": [["NVIDIA_API_KEY", "NEMOTRON_LLM_BASE_URL"]],
    "livekit": [["LIVEKIT_URL"], ["LIVEKIT_API_KEY"], ["LIVEKIT_API_SECRET"], ["OPENAI_API_KEY"], ["DEEPGRAM_API_KEY"], ["ELEVENLABS_API_KEY"]],
}
WORKER_FAMILIES = {"livekit", "gemini"}

rows: list[tuple[str, str, str]] = []


def rec(status: str, name: str, detail: str = "") -> None:
    rows.append((status, name, detail))
    print(f"{status:<5} {name:<34} {detail}", flush=True)


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        rec("WARN", ".env", "missing; copy .env.example and fill keys")
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


def which(name: str, required: bool = True) -> bool:
    ok = shutil.which(name) is not None
    rec("PASS" if ok else ("FAIL" if required else "WARN"), f"tool: {name}", shutil.which(name) or "not on PATH")
    return ok


def check_bluejay_key() -> None:
    key = os.environ.get("BLUEJAY_API_KEY", "").strip()
    if not key:
        rec("FAIL", "BLUEJAY_API_KEY", "unset — create one under API Keys in the Bluejay app")
        return
    api = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
    req = urllib.request.Request(f"{api}/all-agents", headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
        rows_ = body if isinstance(body, list) else (body.get("agents") or body.get("data") or [])
        rec("PASS", "BLUEJAY_API_KEY", f"GET all-agents 200 ({len(rows_)} agents visible)")
    except urllib.error.HTTPError as e:
        rec("FAIL", "BLUEJAY_API_KEY", f"GET all-agents → {e.code} (wrong key or org?)")
    except urllib.error.URLError as e:
        rec("FAIL", "BLUEJAY_API_KEY", f"{api} unreachable: {e}")


def check_pair(harness: str, industry: str) -> None:
    from run import harness_paths, ingress_adapter, split_harness

    family, _ = split_harness(harness)
    family_dir, agent_dir = harness_paths(harness)
    rec("PASS" if (agent_dir / "agent.py").is_file() else "FAIL", "harness agent.py", str(agent_dir / "agent.py"))
    rec("PASS" if (agent_dir / "Dockerfile").is_file() else "FAIL", "harness Dockerfile", str(agent_dir / "Dockerfile"))
    if family in WORKER_FAMILIES:
        rec("PASS", "harness ingress", f"{family} is a LiveKit SIP worker (no CHIRP adapter)")
    else:
        adapter = ingress_adapter(harness)
        rec("PASS" if adapter.is_file() else "FAIL", "harness ingress adapter", str(adapter))
    ind = ROOT / "industries" / industry
    for f in ("agent_blueprint.json", "tools.json", "tool_server.py", "db/schema.sql", "db/seed.sql"):
        rec("PASS" if (ind / f).is_file() else "FAIL", f"industry {f}", str(ind / f))
    tasks = ind / "tasks"
    if tasks.is_dir():
        rec("PASS", "industry tasks", f"{len(list(tasks.iterdir()))} cases")
    else:
        rec("WARN", "industry tasks", "none — control-industry is a wiring smoke, not a scored suite")


def check_provider_keys(harness: str) -> None:
    family = harness.split("/", 1)[0]
    groups = PROVIDER_KEYS.get(family)
    if groups is None:
        rec("WARN", "provider keys", f"unknown family {family}; check its README for env vars")
        return
    for alts in groups:
        if any(os.environ.get(k, "").strip() for k in alts):
            rec("PASS", f"env {' | '.join(alts)}", "set")
        else:
            rec("FAIL", f"env {' | '.join(alts)}", "unset — the pod reads it from mivas-secrets (synced from .env on --apply)")


def check_blueprint(harness: str, industry: str) -> None:
    cmd = ["uv", "run", "python", "run.py", "--harness", harness, "--industry", industry, "--check"]
    # .env AGENTS= (a fleet list) would override --harness/--industry inside run.py
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=300,
                          env={**os.environ, "AGENTS": ""})
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
    rec("PASS" if proc.returncode == 0 else "FAIL", "run.py --check", tail[0][:110])


def check_storage() -> None:
    bucket = os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip()
    if not bucket:
        rec("WARN", "snapshot store", "MIVAS_SNAPSHOT_BUCKET unset — hangup state stays in the pod; verifier state compare is skipped")
        return
    try:
        import boto3
    except ImportError:
        rec("FAIL", "snapshot store", "boto3 missing in this venv (uv sync)")
        return
    try:
        region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-west-1"
        boto3.client("s3", region_name=region).head_bucket(Bucket=bucket)
        ep = os.environ.get("AWS_ENDPOINT_URL_S3", "") or "aws"
        rec("PASS", "snapshot store", f"s3://{bucket} reachable from here (endpoint {ep})")
    except Exception as e:  # noqa: BLE001
        rec("FAIL", "snapshot store", f"head_bucket {bucket}: {type(e).__name__}: {str(e)[:90]}")


def check_k8s(harness: str, industry: str) -> str | None:
    from run import slug

    if not which("kubectl"):
        return None
    ctx = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True)
    rec("PASS" if ctx.returncode == 0 else "FAIL", "kube context", ctx.stdout.strip() or ctx.stderr.strip()[:100])
    name = f"mivas-{slug(harness, industry)}"
    dep = subprocess.run(["kubectl", "get", "deploy", name, "-o", "json"], capture_output=True, text=True)
    if dep.returncode != 0:
        rec("FAIL", f"deployment {name}", "not found — run.py --build --apply (or --codebuild) first")
        return None
    st = json.loads(dep.stdout).get("status") or {}
    ready, want = st.get("readyReplicas", 0), st.get("replicas", 0)
    rec("PASS" if ready and ready == want else "FAIL", f"deployment {name}", f"{ready}/{want} ready")
    pods = subprocess.run(["kubectl", "get", "pods", "-l", f"mivas.slug={slug(harness, industry)}", "-o", "name"],
                          capture_output=True, text=True).stdout.split()
    listening = 0
    for pod in pods:
        logs = subprocess.run(["kubectl", "logs", pod, "--tail=400"], capture_output=True, text=True).stdout
        if any(tok in logs for tok in ("ws↔", "starting CHIRP", "starting LiveKit SIP worker", "listening")):
            listening += 1
        if "snapshot: NO AWS CREDENTIALS" in logs:
            rec("FAIL", f"{pod} snapshot creds", "pod has a bucket but no credentials (Pod Identity / IRSA / static keys)")
    rec("PASS" if pods and listening == len(pods) else "FAIL", "CHIRP listening", f"{listening}/{len(pods)} pods log the bind line")
    base = os.environ.get("MIVAS_BASE_DOMAIN", "").strip()
    return f"wss://{slug(harness, industry)}.{base}" if base and harness.split('/')[0] not in WORKER_FAMILIES else None


def probe_wss(url: str, user: str, password: str) -> None:
    """One WebSocket upgrade with CHIRP basic auth. 101 = ingress, TLS and auth all work."""
    from urllib.parse import urlparse

    u = urlparse(url)
    host, port = u.hostname or "", u.port or (443 if u.scheme == "wss" else 80)
    path = u.path or "/"
    key = base64.b64encode(os.urandom(16)).decode()
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Basic {auth}\r\n"
        f"X-Simulation-Result-Id: preflight\r\n\r\n"
    )
    try:
        raw = socket.create_connection((host, port), timeout=15)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if u.scheme == "wss" else raw
        sock.sendall(request.encode())
        head = sock.recv(4096).decode(errors="replace").split("\r\n", 1)[0]
        sock.close()
    except Exception as e:  # noqa: BLE001
        rec("FAIL", "wss probe", f"{url}: {type(e).__name__}: {str(e)[:100]}")
        return
    if " 101 " in head:
        rec("PASS", "wss probe", f"{url} → {head}")
    elif " 401 " in head or " 403 " in head:
        rec("FAIL", "wss probe", f"{url} → {head} (CHIRP_USER/CHIRP_PASS mismatch with the Bluejay agent)")
    else:
        rec("FAIL", "wss probe", f"{url} → {head}")
    if u.scheme != "wss":
        rec("FAIL", "wss probe", "Bluejay requires wss:// — put a tunnel or TLS ingress in front")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--harness", default=os.environ.get("HARNESS", "openai/realtime-2.1"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--k8s", action="store_true", help="also check the Deployment and probe wss://{slug}.$MIVAS_BASE_DOMAIN")
    p.add_argument("--url", help="probe this wss:// URL instead (tunnel, Baseten proxy, custom DNS)")
    p.add_argument("--skip-check", action="store_true", help="skip run.py --check (slow on first uv sync)")
    a = p.parse_args(argv)
    load_dotenv()
    print(f"preflight {a.harness} × {a.industry}\n", flush=True)
    which("uv"); which("docker", required=False)
    v = sys.version_info
    rec("PASS" if (v.major, v.minor) >= (3, 12) else "FAIL", "python", f"{v.major}.{v.minor}")
    check_bluejay_key()
    check_pair(a.harness, a.industry)
    check_provider_keys(a.harness)
    if not a.skip_check:
        check_blueprint(a.harness, a.industry)
    check_storage()
    url = a.url
    if a.k8s:
        url = check_k8s(a.harness, a.industry) or url
    if url:
        probe_wss(url, os.environ.get("CHIRP_USER", "mivas"), os.environ.get("CHIRP_PASS", "mivas"))
    elif a.k8s:
        rec("SKIP", "wss probe", "no MIVAS_BASE_DOMAIN and no --url (worker family, or local: pass the tunnel URL)")
    fails = [r for r in rows if r[0] == "FAIL"]
    print(f"\n{len(fails)} FAIL · {sum(r[0] == 'WARN' for r in rows)} WARN · {sum(r[0] == 'PASS' for r in rows)} PASS", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
