#!/usr/bin/env python3
"""Build and run a MIVAS harness × industry combo (local or Kubernetes Job)."""

from __future__ import annotations

import argparse
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
        elif "OPENAI_API_KEY=" in part and not part.startswith("OPENAI_API_KEY=***"):
            out.append("OPENAI_API_KEY=***")
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
    return f"{harness.replace('/', '-')}-{industry}".replace("_", "-")


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


def render_job(harness: str, industry: str, image: str) -> Path:
    family, runtime = split_harness(harness)
    template = (ROOT / "k8s" / "job.yaml").read_text()
    rendered = (
        template.replace("__VOICE_AGENT__", harness)
        .replace("__HARNESS_FAMILY__", family)
        .replace("__HARNESS_RUNTIME__", runtime)
        .replace("__INDUSTRY__", industry)
        .replace("__IMAGE__", image)
        .replace("__JOB_SLUG__", slug(harness, industry))
    )
    out = Path(tempfile.mkstemp(suffix=".yaml", prefix="mivas-job-")[1])
    out.write_text(rendered)
    return out


def secret_exists() -> bool:
    result = subprocess.run(
        ["kubectl", "get", "secret", "mivas-secrets"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def apply_job(harness: str, industry: str, image: str, job_name: str, follow_logs: bool) -> None:
    if not secret_exists():
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            print(
                "missing Secret mivas-secrets and OPENAI_API_KEY unset\n"
                "create with: kubectl create secret generic mivas-secrets "
                "--from-literal=OPENAI_API_KEY=...",
                file=sys.stderr,
            )
            sys.exit(1)
        run(
            [
                "kubectl",
                "create",
                "secret",
                "generic",
                "mivas-secrets",
                f"--from-literal=OPENAI_API_KEY={api_key}",
            ]
        )

    subprocess.run(
        ["kubectl", "delete", "job", job_name, "--ignore-not-found"],
        check=False,
    )
    rendered = render_job(harness, industry, image)
    try:
        print(f"applying Job {job_name} (image={image})")
        run(["kubectl", "apply", "-f", str(rendered)])
    finally:
        rendered.unlink(missing_ok=True)

    subprocess.run(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "pod",
            "-l",
            f"job-name={job_name}",
            "--timeout=120s",
        ],
        check=False,
    )
    if follow_logs:
        run(["kubectl", "logs", "-f", f"job/{job_name}"])


def run_local(harness: str, industry: str, agent_check: bool) -> None:
    family_dir, agent_dir = harness_paths(harness)
    port = os.environ.get("TOOL_SERVER_PORT", "8000")
    url = os.environ.get("TOOL_SERVER_URL", f"http://127.0.0.1:{port}")
    industry_dir = ROOT / "industries" / industry
    db_path = os.environ.get("MIVAS_DB_PATH", str(industry_dir / "db" / "runtime.db"))

    env = os.environ.copy()
    env.update(
        {
            "TOOL_SERVER_URL": url,
            "TOOL_SERVER_PORT": port,
            "MIVAS_DB_PATH": db_path,
            "INDUSTRY_DIR": str(industry_dir),
            "VOICE_AGENT": harness,
            "INDUSTRY": industry,
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

        agent_cmd = [sys.executable, str(agent_dir / "agent.py"), industry]
        if agent_check:
            agent_cmd.append("--check")
        print(f"starting local harness agent ({harness})")
        run(agent_cmd, env=env, cwd=str(ROOT))
    finally:
        cleanup()


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Build and run a MIVAS harness × industry combo (local or Kubernetes Job)."
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
    parser.add_argument("--apply", action="store_true", help="kubectl apply rendered Job")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run tool server + agent locally (default when no --build/--apply)",
    )
    parser.add_argument("--check", action="store_true", help="Pass --check to agent.py")
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="After --apply, do not stream pod logs",
    )
    args = parser.parse_args()

    harness = args.harness
    industry = args.industry
    image = f"mivas-bench:{slug(harness, industry)}"
    job_name = f"mivas-{slug(harness, industry)}"

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
        run_local(harness, industry, args.check)
    if args.apply:
        apply_job(harness, industry, image, job_name, follow_logs=not args.no_logs)


if __name__ == "__main__":
    main()
