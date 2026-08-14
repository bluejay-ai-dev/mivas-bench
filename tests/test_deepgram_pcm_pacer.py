"""Deepgram CHIRP bridge must pace TTS bursts at realtime 20 ms frames."""

from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "voice-agent-harnesses" / "deepgram"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pcm = _load("deepgram_pcm", FAMILY / "pcm.py")
FRAME_BYTES = pcm.FRAME_BYTES
PcmPacer = pcm.PcmPacer
CATCHUP_S = pcm.CATCHUP_S


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


def test_pacer_catchup_window_covers_sentence_pauses() -> None:
    assert CATCHUP_S >= 2.0


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
