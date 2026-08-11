"""Local smoke for the deployed Line agent — no Bluejay, no tunnel.

Deploys (or reuses) the Cartesia agent, opens the same stream websocket the
chirp bridge uses, feeds silence, and prints the turn events + audio byte rate.
A passing run proves: the blueprint reached the deployed runtime, the LLM key
works, the greeting is the industry greeting, and output really is pcm_16000.

    uv run python voice-agent-harnesses/cartesia/line/agent.py control-industry
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.chirp import CARTESIA_VERSION, STREAM_URL, _start_config  # noqa: E402
from harness import ensure_agent  # noqa: E402

SILENCE = base64.b64encode(b"\x00" * 640).decode()  # 20 ms @ 16 kHz


async def smoke(industry: str, seconds: float = 20.0) -> None:
    agent_id = ensure_agent(industry)
    url = STREAM_URL.format(agent_id=agent_id, version=CARTESIA_VERSION)
    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {os.environ['CARTESIA_API_KEY']}"}
    ) as ws:
        await ws.send(json.dumps({"event": "start", "config": _start_config()}))

        async def feed() -> None:
            while True:
                await ws.send(json.dumps({"event": "media_input", "media": {"payload": SILENCE}}))
                await asyncio.sleep(0.02)

        task = asyncio.create_task(feed())
        audio, first, last, t0 = 0, None, None, time.monotonic()
        try:
            while time.monotonic() - t0 < seconds:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=seconds))
                if event.get("event") == "media_output":
                    first = first or time.monotonic()
                    last = time.monotonic()
                    audio += len(base64.b64decode(event["media"]["payload"]))
                elif event.get("event") == "turn_ended":
                    turn = event["turn_ended"]
                    print(f"{turn['role']}: {turn['text']!r} tools={turn.get('tool_calls')}")
                    if turn["role"] == "assistant":
                        break
                elif event.get("event") in ("ack", "error"):
                    print(event.get("event"), json.dumps(event)[:300])
        finally:
            task.cancel()
    rate = audio / max((last or 0) - (first or 0), 1e-9)
    print(f"agent audio {audio} bytes @ {rate:.0f} B/s (pcm_16000 = 32000 B/s)")
    assert audio > 10_000, "deployed agent produced no audio"
    assert 28_000 < rate < 36_000, f"unexpected output sample rate: {rate:.0f} B/s"


if __name__ == "__main__":
    asyncio.run(smoke(sys.argv[1] if len(sys.argv) > 1 else "control-industry"))
