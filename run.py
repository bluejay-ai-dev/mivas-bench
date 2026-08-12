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
    for part in cmd:
        if part.startswith("--from-literal=OPENAI_API_KEY="):
            out.append("--from-literal=OPENAI_API_KEY=***")
        elif part.startswith("--from-literal=NVIDIA_API_KEY="):
            out.append("--from-literal=NVIDIA_API_KEY=***")
        elif part.startswith("--from-literal=BLUEJAY_API_KEY="):
            out.append("--from-literal=BLUEJAY_API_KEY=***")
        elif part.startswith("--from-literal=CHIRP_PASS="):
            out.append("--from-literal=CHIRP_PASS=***")
        elif "OPENAI_API_KEY=" in part and not part.startswith("OPENAI_API_KEY=***"):
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
    """VOICE_AGENT like openai/realtime-2.1 → (family, runtime)."""
    if "/" not in harness:
        raise ValueError(
            f"VOICE_AGENT must be family/runtime (e.g. openai/realtime-2.1), got {harness!r}"
        )
    family, runtime = harness.split("/", 1)
    return family, runtime


def harness_paths(harness: str) -> tuple[Path, Path]:
    family, runtime = split_harness(harness)
    family_dir = ROOT / "voice-agent-harnesses" / family
    agent_dir = family_dir / runtime
    return family_dir, agent_dir


def slug(harness: str, industry: str) -> str:
    return (
        f"{harness.replace('/', '-')}-{industry}"
        .replace("_", "-")
        .replace(".", "-")
        .lower()
    )


def build_image(harness: str, industry: str, image: str) -> None:
    family, runtime = split_harness(harness)
    print(f"building {image}")
    run(
        [
            "docker",
            "build",
            "--build-arg",
            f"HARNESS_FAMILY={family}",
            "--build-arg",
            f"HARNESS_RUNTIME={runtime}",
            "--build-arg",
            f"VOICE_AGENT={harness}",
            "--build-arg",
            f"INDUSTRY={industry}",
            "-t",
            image,
            str(ROOT),
        ]
    )


def _render(template_name: str, harness: str, industry: str, image: str, service_type: str) -> str:
    family, runtime = split_harness(harness)
    template = (ROOT / "k8s" / template_name).read_text()
    return (
        template.replace("__VOICE_AGENT__", harness)
        .replace("__HARNESS_FAMILY__", family)
        .replace("__HARNESS_RUNTIME__", runtime)
        .replace("__INDUSTRY__", industry)
        .replace("__IMAGE__", image)
        .replace("__JOB_SLUG__", slug(harness, industry))
        .replace("__SERVICE_TYPE__", service_type)
    )


def secret_exists() -> bool:
    result = subprocess.run(
        ["kubectl", "get", "secret", "mivas-secrets"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def ensure_secret() -> None:
    """Create or refresh mivas-secrets from env (provider keys + Bluejay/CHIRP).

    Merges into any existing Secret so a NVIDIA-only refresh cannot wipe
    OPENAI_API_KEY / custom CHIRP credentials already on the cluster.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    nvidia_key = os.environ.get("NVIDIA_API_KEY", "")
    exists = secret_exists()
    if not exists and not api_key and not nvidia_key:
        print(
            "missing Secret mivas-secrets and no OPENAI_API_KEY/NVIDIA_API_KEY in env\n"
            "create with: kubectl create secret generic mivas-secrets "
            "--from-literal=OPENAI_API_KEY=... and/or --from-literal=NVIDIA_API_KEY=...",
            file=sys.stderr,
        )
        sys.exit(1)
    if not api_key and not nvidia_key and not os.environ.get("BLUEJAY_API_KEY"):
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

    if api_key:
        literals["OPENAI_API_KEY"] = api_key
    if nvidia_key:
        literals["NVIDIA_API_KEY"] = nvidia_key
    if os.environ.get("BLUEJAY_API_KEY"):
        literals["BLUEJAY_API_KEY"] = os.environ["BLUEJAY_API_KEY"]
    if os.environ.get("CHIRP_USER"):
        literals["CHIRP_USER"] = os.environ["CHIRP_USER"]
    elif "CHIRP_USER" not in literals:
        literals["CHIRP_USER"] = "mivas"
    if os.environ.get("CHIRP_PASS"):
        literals["CHIRP_PASS"] = os.environ["CHIRP_PASS"]
    elif "CHIRP_PASS" not in literals:
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


def apply_chirp(harness: str, industry: str, image: str, name: str, follow_logs: bool) -> None:
    ensure_secret()
    service_type = os.environ.get("MIVAS_SERVICE_TYPE", "LoadBalancer")

    for kind, resource in (("deploy", f"deployment/{name}"), ("svc", f"svc/{name}")):
        subprocess.run(
            ["kubectl", "delete", kind, name, "--ignore-not-found"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    rendered_files: list[Path] = []
    try:
        for template in ("deployment.yaml", "service.yaml"):
            text = _render(template, harness, industry, image, service_type)
            path = Path(tempfile.mkstemp(suffix=".yaml", prefix=f"mivas-{template}-")[1])
            path.write_text(text)
            rendered_files.append(path)
            print(f"applying {template} → {name}")
            run(["kubectl", "apply", "-f", str(path)])
    finally:
        for path in rendered_files:
            path.unlink(missing_ok=True)

    subprocess.run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{name}",
            "--timeout=180s",
        ],
        check=False,
    )

    url = service_websocket_url(name)
    if url:
        print(f"CHIRP websocket URL: {url}")
        print("Point the Bluejay agent websocket_url at that address (auth: CHIRP_USER/CHIRP_PASS).")
    else:
        print(
            "Service has no external address yet. Check:\n"
            f"  kubectl get svc {name}\n"
            "Or set MIVAS_SERVICE_TYPE=NodePort and MIVAS_NODE_HOST=<reachable-ip>.",
            file=sys.stderr,
        )

    if follow_logs:
        run(["kubectl", "logs", "-f", f"deployment/{name}"])


def run_local(harness: str, industry: str, agent_check: bool, mode: str) -> None:
    family_dir, agent_dir = harness_paths(harness)
    port = os.environ.get("TOOL_SERVER_PORT", "8000")
    url = os.environ.get("TOOL_SERVER_URL", f"http://127.0.0.1:{port}")
    industry_dir = ROOT / "industries" / industry
    db_path = os.environ.get("MIVAS_DB_PATH", str(industry_dir / "db" / "runtime.db"))
    _, runtime = split_harness(harness)

    env = os.environ.copy()
    env.update(
        {
            "TOOL_SERVER_URL": url,
            "TOOL_SERVER_PORT": port,
            "MIVAS_DB_PATH": db_path,
            "INDUSTRY_DIR": str(industry_dir),
            "VOICE_AGENT": harness,
            "INDUSTRY": industry,
            "HARNESS_RUNTIME": runtime,
            "MIVAS_MODE": mode,
            "PYTHONPATH": str(family_dir)
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
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

        if mode == "chirp":
            chirp = agent_dir / "adapters" / "chirp.py"
            if not chirp.is_file():
                print(f"no chirp adapter for harness={harness}", file=sys.stderr)
                sys.exit(1)
            print(f"starting local CHIRP ({harness}) — Ctrl+C to stop")
            run([sys.executable, str(chirp)], env=env, cwd=str(ROOT))
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
        default=os.environ.get("VOICE_AGENT", "openai/realtime-2.1"),
        help="Harness path family/runtime (default: $VOICE_AGENT or openai/realtime-2.1)",
    )
    parser.add_argument(
        "--industry",
        default=os.environ.get("INDUSTRY", "control-industry"),
        help="Industry pack (default: $INDUSTRY or control-industry)",
    )
    parser.add_argument("--build", action="store_true", help="docker build image")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="kubectl apply CHIRP Deployment + Service",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run tool server + chirp/agent locally (default when no --build/--apply)",
    )
    parser.add_argument(
        "--mode",
        choices=("chirp", "agent", "check"),
        default=os.environ.get("MIVAS_MODE", "chirp"),
        help="Runtime mode (default: chirp for Bluejay WebSocket sims)",
    )
    parser.add_argument("--check", action="store_true", help="Shortcut for --mode check")
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="After --apply, do not stream pod logs",
    )
    args = parser.parse_args()

    harness = args.harness
    industry = args.industry
    mode = "check" if args.check else args.mode
    image = f"mivas-bench:{slug(harness, industry)}"
    name = f"mivas-{slug(harness, industry)}"

    do_local = args.local or not (args.build or args.apply)

    try:
        family_dir, agent_dir = harness_paths(harness)
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    if not family_dir.is_dir():
        print(f"unknown harness family: {family_dir.name}", file=sys.stderr)
        sys.exit(1)
    if not (agent_dir / "agent.py").is_file():
        print(f"unknown harness runtime (missing agent.py): {harness}", file=sys.stderr)
        sys.exit(1)
    if not (ROOT / "industries" / industry).is_dir():
        print(f"unknown industry: {industry}", file=sys.stderr)
        sys.exit(1)
    if not (ROOT / "industries" / industry / "tool_server.py").is_file():
        print(f"industry missing tool_server.py: {industry}", file=sys.stderr)
        sys.exit(1)

    if args.build:
        build_image(harness, industry, image)
    if do_local:
        run_local(harness, industry, agent_check=args.check, mode=mode)
    if args.apply:
        if mode != "chirp":
            print("--apply deploys the CHIRP server; use --mode chirp (default)", file=sys.stderr)
            sys.exit(1)
        apply_chirp(harness, industry, image, name, follow_logs=not args.no_logs)


if __name__ == "__main__":
    main()
