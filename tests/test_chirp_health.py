"""GET /health on the CHIRP port answers 200 while WebSocket upgrades still work."""

from __future__ import annotations

import asyncio
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from chirp_health import process_request  # noqa: E402


async def _roundtrip() -> tuple[int, int, str]:
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    async def echo(ws):
        async for msg in ws:
            await ws.send(msg)

    async with serve(echo, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()

        def get(path: str) -> int:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        health = await loop.run_in_executor(None, get, "/health")
        other = await loop.run_in_executor(None, get, "/")
        async with connect(f"ws://127.0.0.1:{port}/") as ws:
            await ws.send("ping")
            echoed = await ws.recv()
        return health, other, echoed


def test_health_and_upgrade_coexist() -> None:
    health, other, echoed = asyncio.run(_roundtrip())
    assert health == 200
    assert other == 404
    assert echoed == "ping"
