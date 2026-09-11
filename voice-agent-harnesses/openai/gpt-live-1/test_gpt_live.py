"""Wire-level checks for the GPT-Live client against a fake socket (no network)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live as live_mod  # noqa: E402
from live import LiveSession  # noqa: E402
from pack import REPO_ROOT, load_pack  # noqa: E402

CONTROL = REPO_ROOT / "industries" / "control-industry"


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None: ...

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.sent if e["type"] == kind]


def _session(tool_log: list) -> tuple[LiveSession, FakeWS]:
    async def audio(_: bytes) -> None: ...

    async def run_tool(name: str, args: dict) -> dict:
        tool_log.append((name, args))
        return {"success": True, "date": args.get("date")}

    live = LiveSession(load_pack(CONTROL), api_key="k", on_audio=audio, run_tool=run_tool)
    ws = FakeWS()
    live._ws = ws
    return live, ws


def _fc(call_id: str, name: str, args: dict, delegation="item_1") -> dict:
    return {
        "type": "response.event",
        "delegation_id": delegation,
        "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "status": "completed", "call_id": call_id,
                     "name": name, "arguments": json.dumps(args)},
        },
    }


def _completed(rid: str, delegation="item_1") -> dict:
    return {"type": "response.event", "delegation_id": delegation,
            "event": {"type": "response.completed", "response": {"id": rid, "usage": {"input_tokens": 3, "output_tokens": 2}}}}


async def _ack_commands(live: LiveSession, ws: FakeWS, ack_for: dict[str, str]) -> None:
    """Acknowledge the listed pending commands the way the server would. Anything
    not listed (the fire-and-forget appends) is left unanswered on purpose."""
    for _ in range(50):
        await asyncio.sleep(0)
        for eid, fut in list(live._waiters.items()):
            if fut.done():
                continue
            sent = next(e for e in ws.sent if e.get("event_id") == eid)
            ack = ack_for.get(sent["type"])
            if ack:
                await live._handle({"type": ack, "client_event_id": eid, "session": {}})


def test_session_start_shape() -> None:
    live, _ = _session([])
    cfg = live.session_config()
    assert cfg["model"] == "gpt-live-1"
    assert cfg["audio"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert cfg["audio"]["output"]["voice"] == live_mod.VOICE
    tools = {t["name"]: t for t in cfg["delegation"]["responses"]["tools"]}
    assert set(tools) == {"handoff_to_scheduler", "end_call"}  # receptionist only
    assert tools["end_call"]["type"] == "function" and "parameters" in tools["end_call"]
    assert "Delegation policy" in cfg["instructions"]
    tail = cfg["instructions"].split("Backend tools:", 1)[1]
    assert "handoff_to_scheduler" not in tail and "end_call" not in tail  # capabilities, not tool names
    assert "Today is" in cfg["delegation"]["responses"]["instructions"]


def test_function_call_then_completed_submits_output_and_continues() -> None:
    log: list = []

    async def go() -> None:
        live, ws = _session(log)
        await live._handle({"type": "response.event", "delegation_id": "item_1",
                            "event": {"type": "response.created", "response": {"id": "resp_1"}}})
        # arrive at the scheduler stage first so schedule_appointment is a known tool
        live.stage = live.pack.stages["scheduler"]
        await live._handle(_fc("call_1", "schedule_appointment", {"date": "09/08/2026"}))
        await live._handle(_fc("call_1", "schedule_appointment", {"date": "09/08/2026"}))  # duplicate
        await live._handle(_completed("resp_1"))
        await live.drain()
        outputs = ws.of("response.item.create")
        assert len(outputs) == 1 and outputs[0]["item"]["call_id"] == "call_1"
        assert json.loads(outputs[0]["item"]["output"]) == {"success": True, "date": "09/08/2026"}
        assert len(ws.of("response.create")) == 1
        assert ws.sent.index(outputs[0]) < ws.sent.index(ws.of("response.create")[0])
        assert log == [("schedule_appointment", {"date": "09/08/2026"})]

    asyncio.run(go())


def test_handoff_answers_call_then_swaps_backend_then_continues() -> None:
    async def go() -> None:
        live, ws = _session([])
        await live._handle(_fc("call_h", "handoff_to_scheduler", {}))
        acks = asyncio.create_task(_ack_commands(live, ws, {"session.update": "session.updated"}))
        await live._handle(_completed("resp_h"))
        await acks
        await live.drain()
        upd = ws.of("session.update")
        assert len(upd) == 1
        resp = upd[0]["session"]["delegation"]["responses"]
        assert {t["name"] for t in resp["tools"]} == {"schedule_appointment", "end_call"}
        assert "scheduler" in resp["instructions"].lower()
        notice = ws.of("session.instructions.append")
        assert notice and notice[0]["delegation_id"] is None
        assert "scheduler" in notice[0]["content"]
        # output first (the service queues session.update behind the pending call), then update, notice, continue
        out_i = ws.sent.index(ws.of("response.item.create")[0])
        assert out_i < ws.sent.index(upd[0]) < ws.sent.index(notice[0]) < ws.sent.index(ws.of("response.create")[0])
        assert live.stage.name == "scheduler"
        out = json.loads(ws.of("response.item.create")[0]["item"]["output"])
        assert out["success"] is True and out["to_agent"] == "scheduler" and "scheduler" in out["note"]

    asyncio.run(go())


def test_end_call_closes_after_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_mod, "END_CALL_GRACE_S", 0.2)
    monkeypatch.setattr(live_mod, "CLOSED_TIMEOUT_S", 0.2)

    async def go() -> None:
        live, ws = _session([])
        await live._handle(_fc("call_e", "end_call", {"reason": "done"}))
        await live._handle(_completed("resp_e"))
        await asyncio.sleep(0.05)
        assert ws.of("response.item.create")  # end_call is answered, never POSTed
        assert not ws.of("session.close"), "must wait for the backend's summary turn"
        await live._handle(_completed("resp_e2"))  # backend summary, no function calls
        await asyncio.sleep(0.5)
        assert ws.of("session.close"), "no session.close after end_call"
        await live._handle({"type": "session.closed", "reason": "client_request", "usage": {"seconds": 4.2}})
        assert live.close_reason == "client_request" and live.usage_seconds == 4.2

    asyncio.run(go())


def test_speak_first_appends_instructions_without_awaiting_the_report() -> None:
    """session.instructions.appended only fires once caller audio advances the
    timeline, so the greeting must not block on it."""

    async def go() -> None:
        live, ws = _session([])
        await asyncio.wait_for(live.speak_first(), 0.5)  # no ack is ever delivered
        sent = ws.of("session.instructions.append")
        assert sent and not ws.of("session.commentary.append")
        assert all(e["delegation_id"] is None for e in sent)
        content = "".join(e["content"] for e in sent)
        assert "without waiting" in content
        if live.pack.greeting:
            assert live.pack.greeting in content

    asyncio.run(go())


def test_error_correlates_to_command() -> None:
    async def go() -> None:
        live, ws = _session([])
        task = asyncio.create_task(live._command({"type": "session.instructions.append", "delegation_id": None, "content": "hi"}, ack="session.instructions.appended"))
        await asyncio.sleep(0)
        eid = ws.sent[-1]["event_id"]
        await live._handle({"type": "error", "error": {"code": "bad", "message": "nope", "client_event_id": eid}})
        with pytest.raises(live_mod.LiveError, match="bad"):
            await task

    asyncio.run(go())


def test_append_chunks_respect_cap() -> None:
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(60))
    chunks = live_mod._chunks(text)
    assert "".join(chunks) == text
    assert all(len(c) <= live_mod.APPEND_MAX_CHARS for c in chunks)

