"""Local smoke for the Bland `base` runtime.

`--check` is offline: blueprint → pathway graph, asserting the receptionist →
handoff → scheduler → schedule_appointment → End Call chain and that the webhook
nodes point at PUBLIC_URL. Without it, run a real call (silence in) and print
whatever the agent says, which proves authorize + stream-v2 + the pathway boot.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import ensure_agent, load_blueprint, pathway_graph, session_ws_url  # noqa: E402


def check(industry: str) -> None:
    bp = load_blueprint(industry)
    graph = pathway_graph(bp, "https://example.test")
    nodes = {n["id"]: n for n in graph["nodes"]}
    # A Default node has no tools bound, so a prompt naming one gets read out loud.
    for agent_name in bp["agents"]:
        prompt = nodes[agent_name]["data"]["prompt"]
        assert not any(tool in prompt for tool in bp["catalog"]), prompt
    assert nodes[bp["start"]]["data"]["isStart"], f"{bp['start']} must be the start node"
    assert all(e.get("description") and "data" not in e for e in graph["edges"])
    if bp["start"] == "receptionist" and "handoff_to_scheduler" in bp["catalog"]:
        assert nodes["end"]["type"] == "End Call"
        for nid, tool in (("handoff", "handoff_to_scheduler"), ("book", "schedule_appointment")):
            assert nodes[nid]["type"] == "Webhook"
            assert nodes[nid]["data"]["url"] == f"https://example.test/tool/{tool}", nodes[nid]
        assert nodes["book"]["data"]["extractVars"][0][0] == "date"
        hops = {(e["source"], e["target"]) for e in graph["edges"]}
        assert hops == {
            ("receptionist", "handoff"),
            ("scheduler", "book"),
            ("receptionist", "end_receptionist"),
            ("scheduler", "end_scheduler"),
        }, hops
        assert [
            (n, nodes[n]["data"]["responsePathways"][0][3]["id"]) for n in ("handoff", "book")
        ] == [("handoff", "scheduler"), ("book", "end")]
        assert nodes["book"]["data"]["responsePathways"][1][3]["id"] == "scheduler"
    else:
        for name, agent in bp["agents"].items():
            assert nodes[name]["type"] == "Default"
            for tool in agent["tools"]:
                if tool.get("handoff"):
                    nid = f"{name}__{tool['name']}"
                    assert nodes[nid]["type"] == "Webhook"
                    assert nodes[nid]["data"]["url"] == (
                        f"https://example.test/tool/{tool['name']}"
                    )
                    assert nodes[nid]["data"]["responsePathways"][0][3]["id"] == tool["handoff_to"]
    print(f"ok — {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")


async def call(industry: str, public_url: str, seconds: float) -> None:
    public_url = os.environ.get("PUBLIC_URL", "").strip() or public_url
    if not public_url:
        raise SystemExit("need PUBLIC_URL (cloudflared https url) — Bland calls tools over HTTPS")
    ids = ensure_agent(industry, public_url)
    print(f"agent={ids['agent_id']} pathway={ids['pathway_id']}", flush=True)
    frame = b"\x00\x00" * 882  # 20 ms of 44.1 kHz silence
    async with websockets.connect(await session_ws_url(ids["agent_id"]), max_size=None) as ws:
        t0, audio = time.monotonic(), 0

        async def send() -> None:
            while time.monotonic() - t0 < seconds:
                await ws.send(frame)
                await asyncio.sleep(0.02)

        async def recv() -> None:
            nonlocal audio
            while time.monotonic() - t0 < seconds:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    audio += len(msg)
                else:
                    print(f"{time.monotonic() - t0:6.2f} {msg[:240]}", flush=True)

        results = await asyncio.gather(send(), recv(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        print(f"agent audio bytes={audio}", flush=True)
        assert audio > 0, "provider session produced no audio"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("industry", nargs="?", default="control-industry")
    p.add_argument("--check", action="store_true")
    p.add_argument("--public-url", default="")
    p.add_argument("--seconds", type=float, default=15.0)
    a = p.parse_args()
    if a.check:
        check(a.industry)
    else:
        asyncio.run(call(a.industry, a.public_url, a.seconds))
