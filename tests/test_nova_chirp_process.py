"""Nova CHIRP: one container, one OS process per call (Bedrock CRT isolation)."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / "voice-agent-harnesses" / "aws"
CHIRP = AWS / "adapters" / "chirp.py"
CHIRP_CALL = AWS / "adapters" / "chirp_call.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_parent_source_does_not_import_bedrock() -> None:
    text = CHIRP.read_text()
    assert "aws_sdk_bedrock_runtime" not in text
    assert "from harness import" not in text
    assert "open_session" not in text
    assert 'pass_fds=(fd,)' in text
    assert "chirp_call.py" in text


def test_worker_uses_inherited_socket() -> None:
    text = CHIRP_CALL.read_text()
    assert "connect_accepted_socket" in text
    assert "MIVAS_CHIRP_SOCK_FD" in text
    assert "server = Server(" not in text
    assert "connection.handshake()" in text


def test_worker_import_does_not_load_bedrock() -> None:
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(AWS)!r})\n"
        "import adapters.chirp_call\n"
        "assert 'aws_sdk_bedrock_runtime' not in sys.modules\n"
        "assert 'harness' not in sys.modules\n"
    )
    subprocess.check_call([sys.executable, "-c", script], cwd=str(ROOT))


def test_parent_import_does_not_load_bedrock() -> None:
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(AWS)!r})\n"
        "import adapters.chirp\n"
        "assert 'aws_sdk_bedrock_runtime' not in sys.modules\n"
        "assert 'harness' not in sys.modules\n"
    )
    subprocess.check_call([sys.executable, "-c", script], cwd=str(ROOT))


def test_two_connections_are_two_processes(tmp_path: Path) -> None:
    port = _free_port()
    env = os.environ.copy()
    env["MIVAS_CHIRP_TEST_ECHO"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(AWS) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("CHIRP_USER", None)
    env.pop("CHIRP_PASS", None)
    log_path = tmp_path / "chirp-parent.log"
    log_f = log_path.open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "adapters.chirp",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--model",
            "echo",
            "--industry",
            "control-industry",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 8
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if "call=process" in log_path.read_text():
                ready = True
                break
            time.sleep(0.05)
        assert ready, f"parent died before listen\n{log_path.read_text()}\npoll={proc.poll()}"

        async def one() -> str:
            async with websockets.connect(f"ws://127.0.0.1:{port}", open_timeout=8) as ws:
                pid = await asyncio.wait_for(ws.recv(), timeout=8)
                await ws.send("ping")
                pong = await asyncio.wait_for(ws.recv(), timeout=8)
                assert pong == "ping"
                return str(pid)

        async def both() -> list[str]:
            return list(await asyncio.wait_for(asyncio.gather(one(), one()), timeout=20))

        pids = asyncio.run(both())
        assert pids[0] != pids[1], pids
        assert all(p.isdigit() for p in pids), pids
    except Exception:
        print(log_path.read_text(), file=sys.stderr)
        raise
    finally:
        log_f.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
