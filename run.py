#!/usr/bin/env python3
"""Build and run a MIVAS harness × industry combo (local or Kubernetes)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def _redact_cmd(cmd: list[str]) -> str:
    out: list[str] = []
    secret_prefixes = tuple(f"--from-literal={k}=" for k in (
        "OPENAI_API_KEY",
        "NVIDIA_API_KEY",
        "NGC_API_KEY",
        "VAPI_API_KEY",
        "RETELL_API_KEY",
        "BLAND_API_KEY",
        "CARTESIA_API_KEY",
        "ELEVENLABS_API_KEY",
        "ASSEMBLYAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "GOOGLE_API_KEY",
        "GROK_API_KEY",
        "XAI_API_KEY",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "BLUEJAY_API_KEY",
        "CHIRP_PASS",
        "PUBLIC_URL",
    ))
    for part in cmd:
        redacted = False
        for prefix in secret_prefixes:
            if part.startswith(prefix):
                out.append(prefix + "***")
                redacted = True
                break
        if redacted:
            continue
        if "OPENAI_API_KEY=" in part and not part.startswith("OPENAI_API_KEY=***"):
            out.append("OPENAI_API_KEY=***")
        elif "NVIDIA_API_KEY=" in part and not part.startswith("NVIDIA_API_KEY=***"):
            out.append("NVIDIA_API_KEY=***")
        else:
            out.append(part)
    return " ".join(out)


def run(cmd: list[str], **kwargs) -> None:
    print("+", _redact_cmd(cmd))
    subprocess.run(cmd, check=True, **kwargs)


def health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=1) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def split_harness(harness: str) -> tuple[str, str]:
    """HARNESS like openai/realtime-2.1 → (family, runtime)."""
    if "/" not in harness:
        raise ValueError(
            f"HARNESS must be family/runtime (e.g. openai/realtime-2.1), got {harness!r}"
        )
    family, runtime = harness.split("/", 1)
    return family, runtime


def harness_paths(harness: str) -> tuple[Path, Path]:
    family, runtime = split_harness(harness)
    family_dir = ROOT / "voice-agent-harnesses" / family
    agent_dir = family_dir / runtime
    return family_dir, agent_dir


# LiveKit / Pipecat workers register with LiveKit Cloud. They do not serve a
# public ingress adapter and do not need a public Ingress hostname.
WORKER_FAMILIES = frozenset({"livekit", "pipecat"})
PLATFORM_FAMILIES = frozenset({"vapi", "retell", "bland", "cartesia"})


def pair_needs_ingress(harness: str) -> bool:
    return split_harness(harness)[0] not in WORKER_FAMILIES


def ingress_adapter(harness: str) -> Path:
    """Public ingress script. adapters/chirp.py is Bluejay CHIRP only."""
    _, agent_dir = harness_paths(harness)
    conversationrelay = agent_dir / "adapters" / "conversationrelay.py"
    if conversationrelay.is_file():
        return conversationrelay
    return agent_dir / "adapters" / "chirp.py"


def pair_mivas_mode(harness: str) -> str:
    family = split_harness(harness)[0]
    if family in WORKER_FAMILIES:
        return "agent"
    if ingress_adapter(harness).name == "conversationrelay.py":
        return "conversationrelay"
    return "chirp"


def slug(harness: str, industry: str) -> str:
    return (
        f"{harness.replace('/', '-')}-{industry}"
        .replace("_", "-")
        .replace(".", "-")
        .lower()
    )


def image_ref(harness: str, industry: str) -> str:
    """Local tag, or registry image when MIVAS_IMAGE_PREFIX is set (ECR etc.)."""
    tag = slug(harness, industry)
    prefix = os.environ.get("MIVAS_IMAGE_PREFIX", "").strip().rstrip("/")
    if prefix:
        return f"{prefix}:{tag}"
    return f"mivas-bench:{tag}"


def pair_dns_host(harness: str, industry: str) -> str | None:
    """DNS host for a pair when MIVAS_BASE_DOMAIN is set (CHIRP and worker families)."""
    base = os.environ.get("MIVAS_BASE_DOMAIN", "").strip().lower().strip(".")
    if not base:
        return None
    return f"{slug(harness, industry)}.{base}"


def pair_host(harness: str, industry: str) -> str | None:
    """Stable DNS host Bluejay should dial (CHIRP / ConversationRelay only)."""
    if not pair_needs_ingress(harness):
        return None
    return pair_dns_host(harness, industry)


def pair_public_url(harness: str, industry: str) -> str:
    """HTTPS base for tool webhooks and Pipecat Cloud TOOL_SERVER_URL."""
    host = pair_dns_host(harness, industry) or pair_host(harness, industry)
    if host:
        return f"https://{host}"
    return os.environ.get("PUBLIC_URL", "").strip()


def pair_websocket_url(harness: str, industry: str) -> str | None:
    """Stable wss:// URL Bluejay should dial when CHIRP ingress is on."""
    host = pair_host(harness, industry)
    if host:
        return f"wss://{host}"
    return None


def ingress_enabled() -> bool:
    return bool(os.environ.get("MIVAS_BASE_DOMAIN", "").strip())


def _ecr_registry_host(prefix: str) -> str | None:
    """Return the ECR registry host from MIVAS_IMAGE_PREFIX, or None if not ECR."""
    host = prefix.strip().rstrip("/").split("/")[0]
    if ".dkr.ecr." in host and host.endswith(".amazonaws.com"):
        return host
    return None


def ecr_login(prefix: str) -> None:
    """docker login to ECR when MIVAS_IMAGE_PREFIX is an ECR URI."""
    host = _ecr_registry_host(prefix)
    if not host:
        return
    region = host.split(".dkr.ecr.", 1)[1].split(".", 1)[0]
    print(f"+ aws ecr get-login-password --region {region} | docker login {host}")
    password = subprocess.check_output(
        ["aws", "ecr", "get-login-password", "--region", region],
        text=True,
    ).strip()
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", host],
        input=password,
        text=True,
        check=True,
    )


def build_image(harness: str, industry: str, image: str) -> None:
    _, agent_dir = harness_paths(harness)
    dockerfile = agent_dir / "Dockerfile"
    if not dockerfile.is_file():
        raise FileNotFoundError(
            f"missing Dockerfile for harness {harness!r} "
            f"(expected {dockerfile})"
        )
    prefix = os.environ.get("MIVAS_IMAGE_PREFIX", "").strip()
    platforms = os.environ.get("MIVAS_IMAGE_PLATFORMS", "").strip()
    if prefix:
        # Auto Mode general-purpose nodes are amd64; system nodes are arm64.
        platforms = platforms or "linux/amd64,linux/arm64"
        print(f"building {image} (-f {dockerfile.relative_to(ROOT)}) {platforms} → push")
        run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                platforms,
                "-f",
                str(dockerfile),
                "--build-arg",
                f"INDUSTRY={industry}",
                "-t",
                image,
                "--push",
                str(ROOT),
            ]
        )
        return
    platforms = platforms or "linux/arm64"
    print(f"building {image} (-f {dockerfile.relative_to(ROOT)}) {platforms}")
    run(
        [
            "docker",
            "build",
            "--platform",
            platforms,
            "-f",
            str(dockerfile),
            "--build-arg",
            f"INDUSTRY={industry}",
            "-t",
            image,
            str(ROOT),
        ]
    )


def _render(template_name: str, harness: str, industry: str, image: str, service_type: str) -> str:
    family, runtime = split_harness(harness)
    pair_slug = slug(harness, industry)
    host = pair_dns_host(harness, industry) or ""
    public_url = pair_public_url(harness, industry)
    acm = os.environ.get("MIVAS_ACM_CERTIFICATE_ARN", "").strip()
    bluejay_api_url = (
        os.environ.get("BLUEJAY_API_URL") or "https://api.getbluejay.ai/v1"
    ).strip().rstrip("/")
    bluejay_otlp = (
        os.environ.get("BLUEJAY_OTLP_ENDPOINT")
        or "https://otlp.getbluejay.ai/v1/traces"
    ).strip()
    pull_policy = (
        "Always"
        if os.environ.get("MIVAS_IMAGE_PREFIX", "").strip()
        else "IfNotPresent"
    )
    template = (ROOT / "k8s" / template_name).read_text()
    return (
        template.replace("__HARNESS__", harness)
        .replace("__HARNESS_FAMILY__", family)
        .replace("__HARNESS_RUNTIME__", runtime)
        .replace("__INDUSTRY__", industry)
        .replace("__IMAGE__", image)
        .replace("__IMAGE_PULL_POLICY__", pull_policy)
        .replace("__SLUG__", pair_slug)
        .replace("__SERVICE_TYPE__", service_type)
        .replace("__PUBLIC_URL__", public_url)
        .replace("__HOST__", host)
        .replace("__ACM_CERTIFICATE_ARN__", acm)
        .replace("__BLUEJAY_API_URL__", bluejay_api_url)
        .replace("__BLUEJAY_OTLP_ENDPOINT__", bluejay_otlp)
        .replace("__MIVAS_MODE__", pair_mivas_mode(harness))
        .replace("__TWILIO_WELCOME_GREETING__", _twilio_welcome(industry))
        .replace("__REPLICAS__", str(replica_count()))
        .replace("__TOOLS_REPLICAS__", str(tools_replica_count()))
    )


def replica_count() -> int:
    """Harness Deployment replicas from MIVAS_REPLICAS (default 1)."""
    raw = os.environ.get("MIVAS_REPLICAS", "1").strip() or "1"
    try:
        n = int(raw)
    except ValueError:
        raise ValueError(f"MIVAS_REPLICAS must be a positive integer, got {raw!r}") from None
    if n < 1:
        raise ValueError(f"MIVAS_REPLICAS must be >= 1, got {n}")
    return n


def tools_replica_count() -> int:
    """Tools Deployment replicas. v1 is one writer: must be 1."""
    raw = os.environ.get("MIVAS_TOOLS_REPLICAS", "1").strip() or "1"
    try:
        n = int(raw)
    except ValueError:
        raise ValueError(
            f"MIVAS_TOOLS_REPLICAS must be a positive integer, got {raw!r}"
        ) from None
    if n != 1:
        raise ValueError(
            f"MIVAS_TOOLS_REPLICAS must be 1 in v1 (one SQLite writer per pair), got {n}"
        )
    return n


def _twilio_welcome(industry: str) -> str:
    return {
        "control-industry": "Welcome to Bluejay's Repair Services!",
        "healthcare": "Thank you for calling Straus Dermatology.",
        "finance": "Thank you for calling Copperline Credit Union.",
        "legal": "Thank you for calling Halverson and Reed.",
        "travel": "Thank you for calling Summit Air.",
    }.get(industry, "Hello.")


def secret_exists() -> bool:
    result = subprocess.run(
        ["kubectl", "get", "secret", "mivas-secrets"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


# Keys synced from the local environment into mivas-secrets (when present).
_SECRET_ENV_KEYS = (
    "OPENAI_API_KEY",
    "NVIDIA_API_KEY",
    "NGC_API_KEY",
    "VAPI_API_KEY",
    "RETELL_API_KEY",
    "BLAND_API_KEY",
    "CARTESIA_API_KEY",
    "ELEVENLABS_API_KEY",
    "ASSEMBLYAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "GOOGLE_API_KEY",
    "GROK_API_KEY",
    "XAI_API_KEY",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "VOICECHAT_WS_URL",
    "VOICECHAT_FUNCTION_ID",
    "BLUEJAY_API_KEY",
    "PUBLIC_URL",
    "CHIRP_USER",
    "CHIRP_PASS",
)


def ensure_secret() -> None:
    """Create or refresh mivas-secrets from env (provider keys + Bluejay/CHIRP).

    Merges into any existing Secret so a NVIDIA-only refresh cannot wipe
    OPENAI_API_KEY / custom CHIRP credentials already on the cluster.
    """
    present = {k: os.environ.get(k, "") for k in _SECRET_ENV_KEYS if os.environ.get(k, "")}
    exists = secret_exists()
    if not exists and not present:
        print(
            "missing Secret mivas-secrets and no provider/Bluejay keys in env\n"
            "create with: kubectl create secret generic mivas-secrets "
            "--from-literal=OPENAI_API_KEY=... (and/or other harness keys)",
            file=sys.stderr,
        )
        sys.exit(1)
    if not present and exists:
        # Secret already exists; nothing to sync from env.
        return

    literals: dict[str, str] = {}
    if exists:
        try:
            raw = subprocess.run(
                ["kubectl", "get", "secret", "mivas-secrets", "-o", "json"],
                check=True,
                capture_output=True,
                text=True,
            )
            data = (json.loads(raw.stdout or "{}").get("data") or {})
            for key, b64 in data.items():
                if isinstance(b64, str):
                    literals[key] = base64.b64decode(b64).decode("utf-8", errors="replace")
        except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as e:
            print(f"warn: could not read existing mivas-secrets ({e}); writing env keys only", file=sys.stderr)

    for key, value in present.items():
        literals[key] = value
    if "CHIRP_USER" not in literals:
        literals["CHIRP_USER"] = "mivas"
    if "CHIRP_PASS" not in literals:
        literals["CHIRP_PASS"] = "mivas"

    cmd = [
        "kubectl",
        "create",
        "secret",
        "generic",
        "mivas-secrets",
        "--dry-run=client",
        "-o",
        "yaml",
    ]
    for key, value in literals.items():
        cmd.append(f"--from-literal={key}={value}")
    try:
        rendered = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"kubectl create secret failed: {_redact_cmd(cmd)}", file=sys.stderr)
        print(e.stderr or e.stdout or str(e), file=sys.stderr)
        sys.exit(e.returncode)
    apply = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=rendered.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if apply.returncode != 0:
        print(apply.stderr or apply.stdout, file=sys.stderr)
        sys.exit(apply.returncode)
    print(f"+ kubectl apply secret/mivas-secrets → {(apply.stdout or '').strip()}")


def _kubectl_json(cmd: list[str]) -> dict:
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout or "{}")


def service_websocket_url(service_name: str, timeout_s: float = 120.0) -> str | None:
    """Resolve a dialable wss:// URL from the CHIRP Service."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        svc = _kubectl_json(["kubectl", "get", "svc", service_name, "-o", "json"])
        spec = svc.get("spec") or {}
        status = svc.get("status") or {}
        ports = spec.get("ports") or []
        port = next((p.get("port") for p in ports if p.get("name") == "chirp"), 8765)
        node_port = next((p.get("nodePort") for p in ports if p.get("name") == "chirp"), None)
        svc_type = spec.get("type")

        if svc_type == "LoadBalancer":
            ingress = ((status.get("loadBalancer") or {}).get("ingress") or [])
            if ingress:
                host = ingress[0].get("hostname") or ingress[0].get("ip")
                if host:
                    # Docker Desktop / local LBs often terminate TLS elsewhere; use ws for plain.
                    scheme = "wss" if os.environ.get("MIVAS_WSS", "").lower() in {"1", "true"} else "ws"
                    return f"{scheme}://{host}:{port}"

        if svc_type == "NodePort" and node_port:
            # Prefer explicit override; else try a node InternalIP.
            host = os.environ.get("MIVAS_NODE_HOST")
            if not host:
                nodes = _kubectl_json(["kubectl", "get", "nodes", "-o", "json"])
                for node in nodes.get("items") or []:
                    for addr in (node.get("status") or {}).get("addresses") or []:
                        if addr.get("type") == "InternalIP" and addr.get("address"):
                            host = addr["address"]
                            break
                    if host:
                        break
            if host:
                return f"ws://{host}:{node_port}"

        if svc_type == "ClusterIP":
            cluster_ip = spec.get("clusterIP")
            if cluster_ip and cluster_ip != "None":
                return f"ws://{cluster_ip}:{port}"

        time.sleep(2.0)
    return None


def parse_agents(raw: str) -> list[tuple[str, str]]:
    """Parse AGENTS=family/runtime:industry,family/runtime:industry,..."""
    pairs: list[tuple[str, str]] = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"AGENTS entry must be family/runtime:industry, got {entry!r}"
            )
        harness, _, industry = entry.partition(":")
        harness, industry = harness.strip(), industry.strip()
        if not harness or not industry:
            raise ValueError(
                f"AGENTS entry must be family/runtime:industry, got {entry!r}"
            )
        split_harness(harness)  # validate family/runtime shape
        pairs.append((harness, industry))
    if not pairs:
        raise ValueError("AGENTS is set but empty")
    return pairs


def validate_pair(harness: str, industry: str, *, require_ingress: bool) -> None:
    family_dir, agent_dir = harness_paths(harness)
    if not family_dir.is_dir():
        raise ValueError(f"unknown harness family: {family_dir.name}")
    if not (agent_dir / "agent.py").is_file():
        raise ValueError(f"unknown harness runtime (missing agent.py): {harness}")
    if require_ingress and not ingress_adapter(harness).is_file():
        raise ValueError(f"no ingress adapter for harness={harness}")
    industry_dir = ROOT / "industries" / industry
    if not industry_dir.is_dir():
        raise ValueError(f"unknown industry: {industry}")
    if not (industry_dir / "tool_server.py").is_file():
        raise ValueError(f"industry missing tool_server.py: {industry}")


def resolve_pairs(harness: str, industry: str) -> list[tuple[str, str]]:
    """AGENTS overrides single HARNESS/INDUSTRY when set."""
    raw = os.environ.get("AGENTS", "").strip()
    if raw:
        return parse_agents(raw)
    return [(harness, industry)]


def render_agents_yaml(pairs: list[tuple[str, str]], service_type: str) -> str:
    docs: list[str] = []
    use_ingress = ingress_enabled()
    if use_ingress and not os.environ.get("MIVAS_ACM_CERTIFICATE_ARN", "").strip():
        raise ValueError(
            "MIVAS_BASE_DOMAIN is set but MIVAS_ACM_CERTIFICATE_ARN is missing "
            "(needed for HTTPS/WSS Ingress on EKS)"
        )
    if use_ingress:
        # Cluster-scoped; same cert/group for every pair. Render with the first pair
        # so __ACM_CERTIFICATE_ARN__ is filled (other pair fields unused).
        h0, i0 = pairs[0]
        docs.append(_render("ingressclass.yaml", h0, i0, image_ref(h0, i0), service_type))
    for harness, industry in pairs:
        image = image_ref(harness, industry)
        docs.append(_render("deployment-tools.yaml", harness, industry, image, service_type))
        docs.append(_render("service-tools.yaml", harness, industry, image, service_type))
        docs.append(_render("deployment.yaml", harness, industry, image, service_type))
        docs.append(_render("service.yaml", harness, industry, image, service_type))
        if use_ingress and pair_needs_ingress(harness):
            docs.append(_render("ingress.yaml", harness, industry, image, service_type))
        elif use_ingress:
            docs.append(_render("ingress-tools.yaml", harness, industry, image, service_type))
    return "\n---\n".join(docs) + "\n"


def apply_agents(pairs: list[tuple[str, str]], *, follow_logs: bool) -> None:
    """kubectl apply one Deployment+Service(+Ingress) per harness×industry pair."""
    ensure_secret()
    use_ingress = ingress_enabled()
    if use_ingress:
        service_type = os.environ.get("MIVAS_SERVICE_TYPE", "ClusterIP")
    else:
        service_type = os.environ.get("MIVAS_SERVICE_TYPE", "LoadBalancer")

    try:
        yaml_text = render_agents_yaml(pairs, service_type)
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    n = replica_count()
    if n > 1:
        families = {split_harness(h)[0] for h, _ in pairs}
        print(
            f"MIVAS_REPLICAS={n} harness pods; tools Deployment stays at "
            f"{tools_replica_count()} (ClusterIP mivas-{{slug}}-tools).",
            file=sys.stderr,
        )
        if families & PLATFORM_FAMILIES:
            print(
                "warning: vapi/retell/bland/cartesia webhooks still hit a random "
                "CHIRP replica; bind is stored on the tools Service.",
                file=sys.stderr,
            )

    path = Path(tempfile.mkstemp(suffix=".yaml", prefix="mivas-agents-")[1])
    try:
        path.write_text(yaml_text)
        names = [f"mivas-{slug(h, i)}" for h, i in pairs]
        kind = "Deployment+Service+Ingress" if use_ingress else "Deployment+Service"
        print(f"applying {len(pairs)} agent {kind} → {', '.join(names)}")
        if use_ingress:
            base = os.environ["MIVAS_BASE_DOMAIN"].strip().lower().strip(".")
            print(f"stable hosts under *.{base} (same URL across redeploys for each slug)")
        run(["kubectl", "apply", "-f", str(path)])
    finally:
        path.unlink(missing_ok=True)

    for harness, industry in pairs:
        name = f"mivas-{slug(harness, industry)}"
        subprocess.run(
            ["kubectl", "rollout", "status", f"deployment/{name}-tools", "--timeout=180s"],
            check=False,
        )
        subprocess.run(
            ["kubectl", "rollout", "status", f"deployment/{name}", "--timeout=180s"],
            check=False,
        )
        stable = pair_websocket_url(harness, industry)
        public = pair_public_url(harness, industry)
        if split_harness(harness)[0] in WORKER_FAMILIES:
            print(
                f"LiveKit Cloud worker ({name}): connection_type=LIVEKIT "
                f"(pod registers with LIVEKIT_URL)"
            )
            if public:
                print(f"PUBLIC_URL / tools ({name}): {public}/tools")
            continue
        if stable:
            print(f"Bluejay websocket_url ({name}): {stable}")
            if public:
                print(f"PUBLIC_URL / tool webhooks ({name}): {public}")
        else:
            url = service_websocket_url(name, timeout_s=30.0)
            if url:
                print(f"CHIRP websocket URL ({name}): {url}")
            else:
                print(
                    f"Service {name} has no external address yet. Check:\n"
                    f"  kubectl get svc {name}\n"
                    "Or set MIVAS_BASE_DOMAIN (+ MIVAS_ACM_CERTIFICATE_ARN) for stable EKS Ingress URLs,\n"
                    "or MIVAS_SERVICE_TYPE=NodePort and MIVAS_NODE_HOST=<reachable-ip>.",
                    file=sys.stderr,
                )

    chirp_pairs = [(h, i) for h, i in pairs if pair_needs_ingress(h)]
    if chirp_pairs and use_ingress:
        print(
            "Point each Bluejay CHIRP agent websocket_url at the wss:// URL above "
            "(auth: CHIRP_USER/CHIRP_PASS). Hostnames are deterministic per slug."
        )
        print("List: kubectl get ingress,svc,deploy -l app=mivas-bench")
        print("ALB hostname (Cloudflare CNAME target, DNS-only / grey cloud):")
        print(
            "  kubectl get ingress -l app=mivas-bench "
            "-o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}'"
        )
    elif chirp_pairs:
        print("Point each Bluejay agent websocket_url at its Service (auth: CHIRP_USER/CHIRP_PASS).")
        print("List: kubectl get deploy,svc -l app=mivas-bench")
    else:
        print("List: kubectl get deploy,svc -l app=mivas-bench")

    if follow_logs and len(pairs) == 1:
        name = f"mivas-{slug(pairs[0][0], pairs[0][1])}"
        run(["kubectl", "logs", "-f", f"deployment/{name}"])
    elif follow_logs and len(pairs) > 1:
        print(
            "skipping log follow (--no-logs implied for multiple AGENTS); "
            "use kubectl logs -f deployment/<name>"
        )


def run_local(harness: str, industry: str, agent_check: bool, mode: str) -> None:
    family_dir, agent_dir = harness_paths(harness)
    port = os.environ.get("TOOL_SERVER_PORT", "8000")
    url = os.environ.get("TOOL_SERVER_URL", f"http://127.0.0.1:{port}")
    industry_dir = ROOT / "industries" / industry
    db_path = os.environ.get("MIVAS_DB_PATH", str(industry_dir / "db" / "runtime.db"))
    family, runtime = split_harness(harness)

    env = os.environ.copy()
    env.update(
        {
            "TOOL_SERVER_URL": url,
            "TOOL_SERVER_PORT": port,
            "MIVAS_DB_PATH": db_path,
            "INDUSTRY_DIR": str(industry_dir),
            "HARNESS": harness,
            "INDUSTRY": industry,
            "HARNESS_FAMILY": family,
            "HARNESS_RUNTIME": runtime,
            "MIVAS_MODE": mode,
            "PYTHONPATH": os.pathsep.join(
                [str(ROOT / "runtime"), str(family_dir)]
                + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
            ),
            "MIVAS_DB_SHARED": env.get("MIVAS_DB_SHARED", "1"),
        }
    )

    print(f"starting local tool server ({industry}) → {url}")
    tool_proc = subprocess.Popen(
        [sys.executable, str(industry_dir / "tool_server.py")],
        env=env,
        cwd=str(ROOT),
    )

    def cleanup(_signum=None, _frame=None) -> None:
        if tool_proc.poll() is None:
            tool_proc.terminate()
            try:
                tool_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tool_proc.kill()

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        for _ in range(60):
            if health_ok(url):
                break
            if tool_proc.poll() is not None:
                print("tool server exited before becoming healthy", file=sys.stderr)
                sys.exit(1)
            time.sleep(0.5)
        else:
            print("tool server failed health check", file=sys.stderr)
            sys.exit(1)

        if agent_check or mode == "check":
            agent_cmd = [sys.executable, str(agent_dir / "agent.py"), industry, "--check"]
            print(f"starting local harness check ({harness})")
            run(agent_cmd, env=env, cwd=str(ROOT))
            return

        if mode in ("chirp", "conversationrelay"):
            adapter = ingress_adapter(harness)
            if not adapter.is_file():
                print(f"no ingress adapter for harness={harness}", file=sys.stderr)
                sys.exit(1)
            label = (
                "ConversationRelay"
                if adapter.name == "conversationrelay.py"
                else "CHIRP"
            )
            print(f"starting local {label} ({harness}) — Ctrl+C to stop")
            run([sys.executable, str(adapter)], env=env, cwd=str(ROOT))
            return

        agent_cmd = [sys.executable, str(agent_dir / "agent.py"), industry]
        print(f"starting local harness agent ({harness})")
        run(agent_cmd, env=env, cwd=str(ROOT))
    finally:
        cleanup()


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Build and run a MIVAS harness × industry combo (local or Kubernetes)."
    )
    parser.add_argument(
        "--harness",
        default=os.environ.get("HARNESS")
        or os.environ.get("VOICE_AGENT", "openai/realtime-2.1"),
        help="Harness path family/runtime (default: $HARNESS or openai/realtime-2.1)",
    )
    parser.add_argument(
        "--industry",
        default=os.environ.get("INDUSTRY", "control-industry"),
        help="Industry pack (default: $INDUSTRY or control-industry)",
    )
    parser.add_argument("--build", action="store_true", help="docker build image(s)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="kubectl apply CHIRP Deployment + Service (one per AGENTS entry, or single harness/industry)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run tool server + ingress adapter/agent locally (default when no --build/--apply)",
    )
    parser.add_argument(
        "--mode",
        choices=("chirp", "conversationrelay", "agent", "check"),
        default=os.environ.get("MIVAS_MODE", "chirp"),
        help="Runtime mode (default: chirp for Bluejay CHIRP; Twilio uses conversationrelay)",
    )
    parser.add_argument("--check", action="store_true", help="Shortcut for --mode check")
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="After --apply, do not stream pod logs",
    )
    args = parser.parse_args()

    mode = "check" if args.check else args.mode
    do_local = args.local or not (args.build or args.apply)

    try:
        pairs = resolve_pairs(args.harness, args.industry)
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    for harness, industry in pairs:
        family = split_harness(harness)[0]
        require_ingress = family not in WORKER_FAMILIES and (
            args.apply or (do_local and mode in ("chirp", "conversationrelay"))
        )
        try:
            validate_pair(harness, industry, require_ingress=require_ingress)
        except ValueError as e:
            print(e, file=sys.stderr)
            sys.exit(1)

    if os.environ.get("AGENTS", "").strip():
        print(f"AGENTS → {len(pairs)} pair(s): " + ", ".join(f"{h}:{i}" for h, i in pairs))

    if args.build:
        prefix = os.environ.get("MIVAS_IMAGE_PREFIX", "").strip()
        if prefix:
            try:
                ecr_login(prefix)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"ECR login failed: {e}", file=sys.stderr)
                sys.exit(1)
        try:
            for harness, industry in pairs:
                build_image(harness, industry, image_ref(harness, industry))
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print(e, file=sys.stderr)
            sys.exit(1)

    if do_local:
        if len(pairs) > 1:
            print(
                "AGENTS lists multiple pairs; use --build/--apply to deploy them on Kubernetes.\n"
                "For a single local run, unset AGENTS and use HARNESS/INDUSTRY (or --harness/--industry).",
                file=sys.stderr,
            )
            sys.exit(1)
        run_local(pairs[0][0], pairs[0][1], agent_check=args.check, mode=mode)

    if args.apply:
        apply_agents(pairs, follow_logs=not args.no_logs)


if __name__ == "__main__":
    main()
