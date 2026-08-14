"""Vapi CHIRP bridge must pace Flash TTS bursts at realtime 20 ms frames."""

from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "vapi"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pcm = _load("vapi_pcm", FAMILY / "pcm.py")
FRAME_BYTES = pcm.FRAME_BYTES
PcmPacer = pcm.PcmPacer
take_frames = pcm.take_frames


def test_take_frames_holds_partial_and_odd_leftover() -> None:
    buf = bytearray()
    assert take_frames(buf, b"\x01" * 100) == []
    assert len(buf) == 100
    frames = take_frames(buf, b"\x02" * (FRAME_BYTES - 100 + 3))
    assert len(frames) == 1
    assert len(frames[0]) == FRAME_BYTES
    assert len(buf) == 3
    take_frames(buf, b"\x03" * (FRAME_BYTES - 3))
    assert buf == b""


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
    # the new utterance must start near t1, not dump all three frames instantly
    assert times[1] - t1 < 0.050
    later = [times[i + 1] - times[i] for i in range(1, 3)]
    assert all(0.012 < g < 0.040 for g in later), later


def test_pacer_flushes_odd_tail_on_close() -> None:
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    async def go() -> None:
        pacer = PcmPacer(send)
        task = asyncio.create_task(pacer.run())
        pacer.push(b"\x00" * 5)
        pacer.close()
        await task

    asyncio.run(go())
    assert len(sent) == 1
    assert len(sent[0]) == 6
    assert sent[0][-1] == 0


def test_chirp_bridge_paces_outbound_pcm() -> None:
    text = (FAMILY / "adapters" / "chirp.py").read_text()
    assert "PcmPacer" in text
    assert "wait_until_idle" in text
    assert "take_frames" in text
