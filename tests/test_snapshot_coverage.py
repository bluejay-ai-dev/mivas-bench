"""Every family must freeze its call DB to S3, and freeze it under the Bluejay id.

The bucket sat empty for weeks because six families called `set_call_id` and
never any teardown hook, so `capture_final` never ran for them. A per-family
audit is the only thing that catches the next family added without one.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ROOT / "voice-agent-harnesses"
sys.path.insert(0, str(ROOT / "runtime"))

import call_id as call_id_mod  # noqa: E402

# family -> the file that owns its call teardown
HOOK_SITES = {
    "assemblyai": ["adapters/chirp.py"],
    "aws": ["report.py"],
    "bland": ["adapters/chirp.py"],
    "cartesia": ["adapters/chirp.py"],
    "deepgram": ["adapters/chirp.py"],
    "elevenlabs": ["adapters/chirp.py"],
    "gemini": ["adapters/chirp.py"],
    "grok": ["voice/adapters/chirp.py"],
    "livekit": ["harness.py"],
    "nvidia": ["bot.py"],
    "openai": ["report.py"],
    "pipecat": ["bot.py"],
    "qwen": ["report.py"],
    "retell": ["adapters/chirp.py"],
    "twilio": ["adapters/conversationrelay.py"],
    "vapi": ["adapters/chirp.py"],
}

# any one of these means the family reaches capture_final on teardown.
# `end_session` matches bare too: nvidia/pipecat/livekit hand it to
# asyncio.to_thread as a reference rather than calling it.
HOOKS = ("call_session(", "end_session", "capture_final")


def test_hook_sites_cover_every_shipped_family() -> None:
    """A new family directory must be added here, not silently skipped."""
    on_disk = {
        p.name
        for p in HARNESSES.iterdir()
        if p.is_dir() and (p / "harness.py").is_file()
    }
    assert on_disk == set(HOOK_SITES), (
        f"families without a declared snapshot hook site: {on_disk - set(HOOK_SITES)}"
    )


@pytest.mark.parametrize("family", sorted(HOOK_SITES))
def test_family_freezes_its_call_db(family: str) -> None:
    for rel in HOOK_SITES[family]:
        text = (HARNESSES / family / rel).read_text()
        assert any(h in text for h in HOOKS), (
            f"{family}/{rel} never reaches capture_final — its calls will "
            f"write a local .db and nothing to S3"
        )


def test_call_session_freezes_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setitem(
        sys.modules, "snapshot", type(sys)("snapshot")
    )
    sys.modules["snapshot"].capture_final = seen.append  # type: ignore[attr-defined]
    call_id_mod.reset()

    async def go() -> None:
        async with call_id_mod.call_session("725675"):
            pass

    asyncio.run(go())
    assert seen == ["725675"]


def test_call_session_freezes_the_late_twilio_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """ConversationRelay only reveals the real id in its setup frame."""
    seen: list[str] = []
    monkeypatch.setitem(sys.modules, "snapshot", type(sys)("snapshot"))
    sys.modules["snapshot"].capture_final = seen.append  # type: ignore[attr-defined]
    call_id_mod.reset()

    async def go() -> None:
        async with call_id_mod.call_session(None):
            call_id_mod.set_call_id("725807")  # setup frame lands here

    asyncio.run(go())
    assert seen == ["725807"]


def test_call_session_freezes_even_when_the_bridge_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setitem(sys.modules, "snapshot", type(sys)("snapshot"))
    sys.modules["snapshot"].capture_final = seen.append  # type: ignore[attr-defined]
    call_id_mod.reset()

    async def go() -> None:
        async with call_id_mod.call_session("725808"):
            raise RuntimeError("websocket died mid-call")

    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert seen == ["725808"]


def test_call_session_drops_its_registration() -> None:
    """A leaked session makes sole_session() resolve later calls to a dead id."""
    call_id_mod.reset()

    async def go() -> None:
        async with call_id_mod.call_session("725809"):
            assert call_id_mod.sole_session() == "725809"

    asyncio.run(go())
    assert call_id_mod.sole_session() is None


def test_snapshot_key_carries_the_bluejay_id() -> None:
    """Evals join on the simulation result id; the key must be exactly that."""
    import snapshot

    call_id_mod.reset()
    key = snapshot.snapshot_key("725675", ".final.json")
    assert re.fullmatch(r"[^/]+/[^/]+/725675\.final\.json", key), key
