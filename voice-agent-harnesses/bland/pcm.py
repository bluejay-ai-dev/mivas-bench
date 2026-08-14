"""16 kHz s16le pacer for the Bland ↔ CHIRP bridge.

Bland stream-v2 is 44.1 kHz and continuous, but the websocket still stalls
across JSON status frames and pathway webhooks. Bluejay timestamps CHIRP
frames by arrival, so those holes show up as agent_audio_dropouts.

While an agent turn is open, emit a realtime 20 ms clock: real PCM when it
is buffered, silence when it is not. Between turns, send nothing.
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
SILENCE_FRAME = b"\x00" * FRAME_BYTES
# only skip *silence* backlog after a stall; never drop real PCM
CATCHUP_S = 0.400


def take_frames(buf: bytearray, pcm: bytes, frame_bytes: int = FRAME_BYTES) -> list[bytes]:
    """append pcm and peel off complete frames. leftover bytes stay in buf."""
    if pcm:
        buf.extend(pcm)
    frames: list[bytes] = []
    while len(buf) >= frame_bytes:
        frames.append(bytes(buf[:frame_bytes]))
        del buf[:frame_bytes]
    return frames


class PcmPacer:
    """realtime 20 ms frames; fill underruns with silence while a turn is open."""

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
        self._silence = b"\x00" * frame_bytes
        self._buf = bytearray()
        self._more = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._hold = False
        self._sent = 0
        self._t0: float | None = None

    def hold(self, on: bool) -> None:
        """open/close an agent turn. while open, underruns emit silence."""
        if on:
            self._hold = True
            self._idle.clear()
            # a new turn starts a fresh clock so leftover idle time cannot
            # dump the first burst in one shot
            self._t0 = time.monotonic()
            self._sent = 0
        else:
            self._hold = False
            rem = len(self._buf) % self._frame
            if rem:
                self._buf.extend(b"\x00" * (self._frame - rem))
        self._more.set()

    def push(self, pcm: bytes) -> None:
        if not pcm or self._closed:
            return
        if len(pcm) % WIDTH:
            pcm = pcm[: len(pcm) - (len(pcm) % WIDTH)]
        if not pcm:
            return
        self._buf.extend(pcm)
        self._idle.clear()
        self._more.set()

    def close(self) -> None:
        self._closed = True
        self._hold = False
        self._more.set()

    async def wait_until_idle(self, timeout: float = 5.0) -> None:
        """block until every full frame pushed so far has been sent."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except asyncio.TimeoutError:
            return

    async def run(self) -> None:
        try:
            while True:
                if self._closed:
                    await self._flush_tail()
                    return
                if len(self._buf) >= self._frame or self._hold:
                    await self._emit_next()
                    continue
                self._idle.set()
                self._more.clear()
                if len(self._buf) >= self._frame or self._hold or self._closed:
                    continue
                await self._more.wait()
        finally:
            self._idle.set()

    async def _sleep_until_due(self) -> None:
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
            self._sent = 0
            return
        due = self._t0 + self._sent / self._bps
        delay = due - now
        if delay < -self._catchup_s and len(self._buf) < self._frame:
            # stalled with no PCM — snap the clock rather than emitting a
            # backlog of silence that Bluejay would hear as a dump
            self._t0 = now
            self._sent = 0
            return
        if delay > 0:
            await asyncio.sleep(delay)

    async def _emit_next(self) -> None:
        await self._sleep_until_due()
        if len(self._buf) >= self._frame:
            frame = bytes(self._buf[: self._frame])
            del self._buf[: self._frame]
            await self._send(frame)
            self._sent += len(frame)
            if len(self._buf) < self._frame and not self._hold:
                self._idle.set()
            return
        if self._hold:
            await self._send(self._silence)
            self._sent += len(self._silence)

    async def _flush_tail(self) -> None:
        if not self._buf:
            return
        if len(self._buf) % WIDTH:
            self._buf.extend(b"\x00" * (WIDTH - len(self._buf) % WIDTH))
        rem = len(self._buf) % self._frame
        if rem:
            self._buf.extend(b"\x00" * (self._frame - rem))
        while len(self._buf) >= self._frame:
            await self._emit_next()
        self._idle.set()
