"""Vapi harness — Deepgram Flux STT × gpt-4.1 × ElevenLabs Flash v2.5, crossed
with any industry agent_blueprint.json.

There is no local text loop: Vapi runs the squad server-side and the only way in
is the websocket transport, so this entry point is `--check` (push the blueprint
to Vapi and print the ids) plus a one-shot audio smoke that opens a real call and
reports what came back.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import TOOL_SERVER_URL, ensure_squad, industry_path, start_websocket_call  # noqa: E402

MODEL = "vapi-flux-gpt4.1-flash2.5"


async def smoke(squad_id: str, seconds: float = 20.0) -> None:
    """Open a call, feed silence, print events + audio byte count."""
    url, call_id = start_websocket_call(squad_id)
    print(f"call={call_id}")
    audio = 0
    async with websockets.connect(url, max_size=None) as ws:

        async def feed() -> None:
            while True:  # 20 ms of 16 kHz silence
                await ws.send(b"\x00" * 640)
                await asyncio.sleep(0.02)

        feeder = asyncio.create_task(feed())
        try:
            async with asyncio.timeout(seconds):
                async for raw in ws:
                    if isinstance(raw, bytes):
                        audio += len(raw)
                    else:
                        print(json.loads(raw))
        except TimeoutError:
            pass
        finally:
            feeder.cancel()
    print(f"agent audio {audio}B")


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    public_url = os.environ.get("PUBLIC_URL", "").strip()
    if not public_url:
        raise SystemExit("need PUBLIC_URL (cloudflared https url)")
    ids = ensure_squad(industry_dir, public_url)
    print(
        f"ok {industry_dir.name} × {MODEL} squad={ids['squad_id']} "
        f"receptionist={ids['receptionist_id']} scheduler={ids['scheduler_id']} "
        f"tool_server={TOOL_SERVER_URL}"
    )
    if "--check" not in sys.argv:
        asyncio.run(smoke(ids["squad_id"]))
