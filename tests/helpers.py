"""Shared helpers for MIVAS tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    path = path or (ROOT / ".env")
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=1) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def start_tool_server(industry: str, port: int = 8000) -> subprocess.Popen[bytes]:
    industry_dir = ROOT / "industries" / industry
    if not (industry_dir / "tool_server.py").is_file():
        raise FileNotFoundError(f"no tool_server.py for industry={industry}")

    env = os.environ.copy()
    env["TOOL_SERVER_PORT"] = str(port)
    env["TOOL_SERVER_URL"] = f"http://127.0.0.1:{port}"
    env["MIVAS_DB_PATH"] = str(industry_dir / "db" / "runtime.db")
    env.setdefault("MIVAS_DB_SHARED", "1")
    env["INDUSTRY_DIR"] = str(industry_dir)
    runtime = str(ROOT / "runtime")
    env["PYTHONPATH"] = runtime + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    proc = subprocess.Popen(
        [sys.executable, str(industry_dir / "tool_server.py")],
        env=env,
        cwd=str(ROOT),
    )
    url = env["TOOL_SERVER_URL"]
    for _ in range(60):
        if health_ok(url):
            return proc
        if proc.poll() is not None:
            raise RuntimeError("tool server exited before becoming healthy")
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("tool server failed health check")


def stop_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
