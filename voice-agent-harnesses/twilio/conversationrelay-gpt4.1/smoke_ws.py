"""Local ConversationRelay protocol smoke (no Twilio phone required).

Connects to a running adapters/chirp.py and drives setup → prompt turns that
should trigger handoff + schedule_appointment against the industry tool server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import websockets


async def main() -> None:
    host = os.environ.get("CHIRP_HOST", "127.0.0.1")
    port = int(os.environ.get("CHIRP_PORT", "8773"))
    url = f"ws://{host}:{port}/ws?simulation_result_id=0"
    async with websockets.connect(url) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CA_smoke_local",
                    "from": "+15555550100",
                    "to": "+15555550101",
                }
            )
        )
        prompts = [
            "Hi, I'd like to schedule a repair appointment for next Tuesday afternoon.",
            "Yes, next Tuesday afternoon works.",
            "That date is fine, please book it.",
            "Thanks, that's all.",
        ]
        for p in prompts:
            await ws.send(
                json.dumps(
                    {"type": "prompt", "voicePrompt": p, "lang": "en-US", "last": True}
                )
            )
            # Collect text tokens until last:true, with a timeout.
            chunks: list[str] = []
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                msg = json.loads(raw)
                if msg.get("type") == "text":
                    chunks.append(msg.get("token") or "")
                    if msg.get("last"):
                        break
                elif msg.get("type") == "end":
                    print("END", msg)
                    print("AGENT", "".join(chunks))
                    return
                else:
                    print("OTHER", msg)
            print("USER ", p)
            print("AGENT", "".join(chunks))
            print("---")
        await asyncio.sleep(3)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    asyncio.run(main())
