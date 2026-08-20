"""CHIRP TCP acceptor for Nova Sonic.

Each accepted connection is a new Python process (`adapters.chirp_call`).
The Bedrock CRT client is process-wide: two calls in one interpreter cancel
each other's streams. The parent never imports that client.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

_SOCK_FD_ENV = "MIVAS_CHIRP_SOCK_FD"
_HARNESS_DIR = Path(__file__).resolve().parents[1]


def _reap(_signum: int | None = None, _frame: object | None = None) -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _auth_configured() -> bool:
    return bool(
        os.environ.get("CHIRP_USER", "").strip()
        and os.environ.get("CHIRP_PASS", "").strip()
    )


def spawn_call(
    conn: socket.socket,
    *,
    model: str,
    industry: str,
    executable: str | None = None,
) -> subprocess.Popen[bytes]:
    """Hand `conn` to a fresh interpreter running `adapters.chirp_call`."""
    fd = conn.fileno()
    os.set_inheritable(fd, True)
    env = os.environ.copy()
    env[_SOCK_FD_ENV] = str(fd)
    path_parts = [str(_HARNESS_DIR)]
    for part in env.get("PYTHONPATH", "").split(os.pathsep):
        if part and part not in path_parts:
            path_parts.append(part)
    env["PYTHONPATH"] = os.pathsep.join(path_parts)
    proc = subprocess.Popen(
        [
            executable or sys.executable,
            str(_HARNESS_DIR / "adapters" / "chirp_call.py"),
            "--model",
            model,
            "--industry",
            industry,
        ],
        env=env,
        pass_fds=(fd,),
        close_fds=True,
    )
    conn.close()
    return proc


def serve(host: str, port: int, *, model: str, industry: str) -> None:
    signal.signal(signal.SIGCHLD, _reap)
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((host, port))
    lsock.listen(128)
    print(
        f"ws↔Nova Sonic {model} × {industry} :{port} "
        f"auth={_auth_configured()} call=process",
        flush=True,
    )
    try:
        while True:
            conn, _addr = lsock.accept()
            try:
                proc = spawn_call(conn, model=model, industry=industry)
            except Exception as e:
                print(f"chirp spawn failed: {type(e).__name__}: {e}", flush=True)
                conn.close()
                continue
            print(f"chirp spawn pid={proc.pid}", flush=True)
    finally:
        lsock.close()


def main(model: str | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--model",
        default=model or os.environ.get("NOVA_SONIC_MODEL", "amazon.nova-2-sonic-v1:0"),
    )
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--host", default=os.environ.get("CHIRP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIRP_PORT", "8774")))
    a = p.parse_args()
    serve(a.host, a.port, model=a.model, industry=a.industry)


if __name__ == "__main__":
    main()
