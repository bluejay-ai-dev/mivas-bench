"""Deploy this harness to Pipecat Cloud over the REST API (no Docker, no CLI).

The `pipecat` CLI needs an interactive browser login or a personal access token,
and a local Docker daemon; the REST API needs neither — it takes a tarball of the
build context, builds it in-cloud, and deploys the resulting image.

    export PIPECAT_PRIVATE_API_KEY=sk_...        # Pipecat Cloud *private* key
    set -a && source .env && set +a              # provider keys → secret set
    export BLUEJAY_API_KEY=...
    uv run python voice-agent-harnesses/pipecat/deploy.py

Uploads the secret set, builds, then creates or updates the agent. One deployed
agent serves all three runtimes; the runtime is chosen per call by the Bluejay
agent's `pipecat_agent_configuration.runtime`.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from harness import RUNTIME_SECRET_KEYS

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1]
API = "https://api.pipecat.daily.co/v1"

AGENT_NAME = os.environ.get("PIPECAT_AGENT_NAME", "mivas-control")
SECRET_SET = os.environ.get("PIPECAT_SECRET_SET", "mivas-secrets")
REGION = os.environ.get("PIPECAT_REGION", "us-west")
INDUSTRY = os.environ.get("INDUSTRY", "control-industry")

# Provider keys the bot needs at runtime, forwarded from the local environment.
SECRET_KEYS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "BLUEJAY_API_KEY",
    "BLUEJAY_API_URL",
    "BLUEJAY_OTLP_ENDPOINT",
    "BLUEJAY_SERVICE_NAME",
)
# Files copied into the build context (Dockerfile COPYs *.py + industries/).
CONTEXT_FILES = ("Dockerfile", "requirements.txt", "bot.py", "harness.py", "report.py", "check.py")


def api(path: str, data: dict | None = None, method: str | None = None) -> dict:
    key = os.environ.get("PIPECAT_PRIVATE_API_KEY", "")
    if not key:
        sys.exit("PIPECAT_PRIVATE_API_KEY (sk_...) is required")
    req = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        raise SystemExit(f"{method or 'POST'} {path} → {e.code} {body}") from e


def build_context() -> bytes:
    """tar.gz of the harness plus the industry it serves."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in CONTEXT_FILES:
            tar.add(HARNESS_DIR / name, arcname=name)
        tar.add(
            REPO_ROOT / "industries" / INDUSTRY,
            arcname=f"industries/{INDUSTRY}",
            filter=lambda ti: None if ti.name.endswith((".db", "__pycache__")) else ti,
        )
    return buf.getvalue()


def upload_and_build(context: bytes) -> str:
    import subprocess
    import tempfile

    up = api("/builds/upload-url", {"region": REGION})
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as fh:
        fh.write(context)
        fh.flush()
        cmd = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST", up["uploadUrl"]]
        for k, v in up["uploadFields"].items():
            cmd += ["-F", f"{k}={v}"]
        # the presigned policy pins Content-Type but the field is not returned
        cmd += ["-F", "Content-Type=application/gzip", "-F", f"file=@{fh.name}"]
        code = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    if code != "204":
        raise SystemExit(f"context upload failed: HTTP {code}")

    created = api(
        "/builds",
        {"uploadId": up["uploadId"], "region": REGION, "dockerfilePath": "Dockerfile"},
    )
    build_id = created["build"]["id"]
    print(f"build {build_id} cached={created.get('cached')}", flush=True)

    deadline = time.time() + 1800
    while time.time() < deadline:
        status = api(f"/builds/{build_id}")["build"]["status"]
        if status in ("success", "succeeded"):
            return build_id
        if status in ("failed", "error"):
            logs = api(f"/builds/{build_id}/logs")
            raise SystemExit(f"build failed:\n{json.dumps(logs)[-3000:]}")
        print(f"  build {status}...", flush=True)
        time.sleep(10)
    raise SystemExit("build timed out")


def main() -> int:
    secrets = [
        {"secretKey": k, "secretValue": os.environ[k]}
        for k in SECRET_KEYS
        if os.environ.get(k)
    ]
    required = {"BLUEJAY_API_KEY"}
    for runtime_keys in RUNTIME_SECRET_KEYS.values():
        required.update(runtime_keys)
    missing = sorted(k for k in required if not os.environ.get(k))
    if missing:
        sys.exit(f"missing required env: {missing}")
    api(f"/secrets/{SECRET_SET}", {"secrets": secrets, "region": REGION}, method="PUT")
    print(f"secret set {SECRET_SET}: {[s['secretKey'] for s in secrets]}", flush=True)

    build_id = upload_and_build(build_context())

    body = {
        "buildId": build_id,
        "region": REGION,
        "secretSet": SECRET_SET,
        "autoScaling": {"minAgents": 1, "maxAgents": 3},
        "agentProfile": "agent-1x",
        "maxSessionDuration": 600,
    }
    existing = {s["name"] for s in api("/agents").get("services", [])}
    if AGENT_NAME in existing:
        body.pop("region", None)
        body["forceRedeploy"] = True
        api(f"/agents/{AGENT_NAME}", body)
        print(f"updated agent {AGENT_NAME}")
    else:
        api("/agents", {"serviceName": AGENT_NAME, **body})
        print(f"created agent {AGENT_NAME}")

    for _ in range(60):
        details = api(f"/agents/{AGENT_NAME}")
        if details.get("ready") and details.get("activeDeploymentReady"):
            print(f"agent {AGENT_NAME} ready")
            return 0
        time.sleep(10)
    print(f"agent {AGENT_NAME} deployed but not reporting ready yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main() or 0)
