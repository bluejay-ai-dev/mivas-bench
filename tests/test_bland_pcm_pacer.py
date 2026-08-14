"""Bland CHIRP bridge must pace resampled TTS at realtime 20 ms frames."""

from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "bland"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pcm = _load("bland_pcm", FAMILY / "pcm.py")
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
        pacer.push(b"\x00" * (FRAME_BYTES * 5))
        await pacer.wait_until_idle()
        pacer.close()
        await task

    asyncio.run(go())
    assert [len(f) for f in sent] == [FRAME_BYTES] * 5
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    assert gaps, "expected paced gaps between frames"
    assert all(0.012 < g < 0.040 for g in gaps), gaps
    assert (times[-1] - times[0]) >= 0.070


def test_pacer_fills_underrun_with_silence_while_holding() -> None:
    sent: list[bytes] = []
    times: list[float] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)
        times.append(time.monotonic())

    async def go() -> None:
        pacer = PcmPacer(send)
        task = asyncio.create_task(pacer.run())
        pacer.hold(True)
        pacer.push(b"\x11" * FRAME_BYTES)
        await asyncio.sleep(0.085)  # ~4 frames: 1 pcm + 3 silence
        pacer.hold(False)
        await pacer.wait_until_idle()
        pacer.close()
        await task

    asyncio.run(go())
    assert len(sent) >= 3
    assert sent[0] == b"\x11" * FRAME_BYTES
    assert any(f == b"\x00" * FRAME_BYTES for f in sent[1:])
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    assert all(0.010 < g < 0.045 for g in gaps), gaps


def test_pacer_does_not_fill_when_not_holding() -> None:
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    async def go() -> None:
        pacer = PcmPacer(send)
        task = asyncio.create_task(pacer.run())
        pacer.push(b"\x11" * FRAME_BYTES)
        await pacer.wait_until_idle()
        await asyncio.sleep(0.08)
        pacer.close()
        await task

    asyncio.run(go())
    assert sent == [b"\x11" * FRAME_BYTES]


def test_chirp_bridge_holds_pacer_during_agent_turn() -> None:
    text = (FAMILY / "adapters" / "chirp.py").read_text()
    assert "PcmPacer" in text
    assert "pacer.hold(True)" in text
    assert "pacer.hold(False)" in text
    assert "AGENT_RMS_OFF" in text
    assert "pacer.push" in text
