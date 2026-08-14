"""16 kHz s16le pacer for the Grok ↔ CHIRP bridge.

Grok Voice dumps TTS as irregular PCM bursts. Bluejay timestamps CHIRP
frames by arrival, so forwarding those bursts compresses the recording
(greeting in ~78 ms wall-clock, agent_audio_dropouts > 0) and sounds
choppy. Pace outbound on a realtime playback clock at 20 ms.

Keep the clock across short underruns (Grok chunk jitter). A gap longer
than CATCHUP_S is a new utterance — restart so the next turn is not
dumped in one shot to "catch up".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

RATE = 16_000
WIDTH = 2
FRAME_MS = 20
FRAME_BYTES = RATE * WIDTH * FRAME_MS // 1000  # 640
BYTES_PER_SEC = RATE * WIDTH  # 32_000
CATCHUP_S = 0.100


class PcmPacer:
    """emit 16-bit PCM at realtime, 20 ms frames, from a bursty source.

    short gaps stay on the existing clock. a gap longer than CATCHUP_S
    restarts the clock. clear() drops queued frames on barge-in.
    """

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        *,
        frame_bytes: int = FRAME_BYTES,
        bytes_per_sec: int = BYTES_PER_SEC,
        catchup_s: float = CATCHUP_S,
    ) -> None:
        self._send = send
        self._frame = frame_bytes
        self._bps = bytes_per_sec
        self._catchup_s = catchup_s
        self._buf = bytearray()
        self._more = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._sent = 0
        self._t0: float | None = None

    def push(self, pcm: bytes) -> None:
        if not pcm or self._closed:
            return
        self._buf.extend(pcm)
        self._idle.clear()
        self._more.set()

    def clear(self) -> None:
        """drop queued frames (barge-in). the next push starts a new clock."""
        self._buf.clear()
        self._t0 = None
        self._sent = 0
        self._idle.set()
        self._more.set()

    def close(self) -> None:
        self._closed = True
        self._more.set()

    async def wait_until_idle(self, timeout: float = 30.0) -> None:
        """block until every full frame pushed so far has been sent."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except asyncio.TimeoutError:
            return

    async def run(self) -> None:
        try:
            while True:
                if len(self._buf) < self._frame:
                    if self._closed:
                        await self._flush_tail()
                        return
                    self._idle.set()
                    self._more.clear()
                    if len(self._buf) >= self._frame:
                        continue
                    await self._more.wait()
                    continue
                await self._emit_frame()
        finally:
            self._idle.set()

    async def _emit_frame(self) -> None:
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
            self._sent = 0
        due = self._t0 + self._sent / self._bps
        delay = due - now
        if delay < -self._catchup_s:
            self._t0 = now
            self._sent = 0
            delay = 0.0
        if delay > 0:
            await asyncio.sleep(delay)
        if not self._buf:
            return
        frame = bytes(self._buf[: self._frame])
        del self._buf[: self._frame]
        await self._send(frame)
        self._sent += len(frame)
        if len(self._buf) < self._frame:
            self._idle.set()

    async def _flush_tail(self) -> None:
        if not self._buf:
            return
        if len(self._buf) % WIDTH:
            self._buf.extend(b"\x00" * (WIDTH - len(self._buf) % WIDTH))
        await self._send(bytes(self._buf))
        self._buf.clear()
        self._idle.set()
