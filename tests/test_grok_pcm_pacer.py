"""Grok CHIRP bridge must pace TTS bursts at realtime 20 ms frames."""

from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "grok"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pcm = _load("grok_pcm", FAMILY / "pcm.py")
FRAME_BYTES = pcm.FRAME_BYTES
PcmPacer = pcm.PcmPacer
grok_harness = _load("grok_harness", FAMILY / "harness.py")


def test_pacer_emits_burst_as_realtime_20ms_frames() -> None:
    sent: list[bytes] = []
    times: list[float] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)
        times.append(time.monotonic())

    async def go() -> None:
        pacer = PcmPacer(send)
        task = asyncio.create_task(pacer.run())
        pacer.push(b"\x00" * (FRAME_BYTES * 5))  # 100 ms dumped at once
        await pacer.wait_until_idle()
        pacer.close()
        await task

    asyncio.run(go())
    assert [len(f) for f in sent] == [FRAME_BYTES] * 5
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    assert gaps, "expected paced gaps between frames"
    assert all(0.012 < g < 0.040 for g in gaps), gaps
    assert (times[-1] - times[0]) >= 0.070


def test_pacer_holds_clock_across_chunk_jitter() -> None:
    """Grok streams a turn as several bursts a few ms apart.

    the stretch pacer sent the first frame of each burst immediately, so
    chunk boundaries slammed two frames ~1 ms apart (audible chop). a
    playback clock must keep ~20 ms spacing across those underruns.
    """
    times: list[float] = []

    async def send(frame: bytes) -> None:
        times.append(time.monotonic())

    async def go() -> None:
        pacer = PcmPacer(send)
        task = asyncio.create_task(pacer.run())
        pacer.push(b"\x00" * (FRAME_BYTES * 2))
        await pacer.wait_until_idle()
        await asyncio.sleep(0.005)
        pacer.push(b"\x00" * (FRAME_BYTES * 3))
        await pacer.wait_until_idle()
        pacer.close()
        await task

    asyncio.run(go())
    assert len(times) == 5
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    assert all(0.012 < g < 0.040 for g in gaps), gaps


def test_pacer_restarts_clock_after_a_turn_gap() -> None:
    times: list[float] = []

    async def send(frame: bytes) -> None:
        times.append(time.monotonic())

    async def go() -> None:
        pacer = PcmPacer(send)
        task = asyncio.create_task(pacer.run())
        pacer.push(b"\x00" * FRAME_BYTES)
        await pacer.wait_until_idle()
        await asyncio.sleep(0.30)
        t1 = time.monotonic()
        pacer.push(b"\x00" * (FRAME_BYTES * 3))
        await pacer.wait_until_idle()
        pacer.close()
        await task
        return t1

    t1 = asyncio.run(go())
    assert len(times) == 4
    assert times[1] - t1 < 0.050
    later = [times[i + 1] - times[i] for i in range(1, 3)]
    assert all(0.012 < g < 0.040 for g in later), later


def test_pacer_clear_drops_queued_frames() -> None:
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    async def go() -> None:
        pacer = PcmPacer(send)
        task = asyncio.create_task(pacer.run())
        pacer.push(b"\x00" * (FRAME_BYTES * 20))
        pacer.clear()
        await pacer.wait_until_idle()
        pacer.close()
        await task

    asyncio.run(go())
    assert sent == []


def test_chirp_bridge_paces_outbound_pcm() -> None:
    text = (FAMILY / "voice" / "adapters" / "chirp.py").read_text()
    assert "PcmPacer" in text
    assert "wait_until_idle" in text
    assert "pacer.push" in text


def test_chirp_echo_wins_over_inbound_rms() -> None:
    """TTS echo is loud on the mix; user_loud must not cancel the greeting."""
    text = (FAMILY / "voice" / "adapters" / "chirp.py").read_text()
    assert "def _real_barge_in" in text
    assert "if _agent_echo_risk(now):" in text
    assert "if turns.agent_utt is not None:" in text
    assert "grok VAD speech_started IGNORED echo" in text
    assert "server_vad will cancel the greeting" in text
    assert "barge SKIP (agent idle)" in text
    assert "_paced_send" in text
    assert "greeting watchdog skip (already greeted)" in text
    assert "greeting watchdog re-nudge" in text
    assert "AGENT_UTT_GAP" not in text
    assert "agent.speech GAP" not in text


def test_transfer_to_human_hangs_up_after_http() -> None:
    bp = {"agents": {"reception": {"tools": [{"name": "transfer_to_human"}]}}}
    state = {"agent": "reception"}
    payload = {"ok": True, "data": {"transferred": True}}

    class _Resp:
        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return _Resp()

    async def go() -> tuple[dict, bool]:
        with patch.object(grok_harness.httpx, "AsyncClient", return_value=_Client()):
            return await grok_harness._execute_tool("transfer_to_human", {}, bp, state)

    result, stop = asyncio.run(go())
    assert result == payload
    assert stop is True
