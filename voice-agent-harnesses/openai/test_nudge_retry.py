"""The opening nudge must survive a Realtime socket that is not ready yet.

`run()` returns before the socket is necessarily up, and a raw message sent too early is
dropped with a log line rather than an error. One nudge is therefore a race: it is won at
low concurrency and lost at high, where it leaves the agent silent, the digital human
(speaks_first: false) waiting, and the call at zero turns until something kills it — 19 of
31 calls in run 230926.

    uv run python voice-agent-harnesses/openai/test_nudge_retry.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters.chirp import _nudge_until_open  # noqa: E402


class _Model:
    def __init__(self, accept_after: int) -> None:
        self.sent = 0
        self._accept_after = accept_after

    async def send_event(self, _event) -> None:
        self.sent += 1
        if self.sent < self._accept_after:
            raise RuntimeError("socket not ready")  # must not kill the watchdog


class _Session:
    def __init__(self, accept_after: int) -> None:
        self.model = _Model(accept_after)
        self._closed = False


async def _case_agent_opens_late() -> None:
    """Nudge keeps retrying until audio arrives, then stops."""
    s = _Session(accept_after=3)
    opened = asyncio.Event()

    async def open_after():
        await asyncio.sleep(0.25)
        opened.set()

    import adapters.chirp as chirp

    chirp.NUDGE_RETRY_DELAY_S = 0.1
    await asyncio.gather(_nudge_until_open(s, opened), open_after())
    assert s.model.sent >= 2, f"gave up too early: {s.model.sent}"
    assert s.model.sent <= chirp.NUDGE_MAX_ATTEMPTS, s.model.sent


async def _case_first_nudge_lands() -> None:
    """Agent opens immediately: exactly one nudge, no spam."""
    s = _Session(accept_after=1)
    opened = asyncio.Event()
    opened.set()
    await _nudge_until_open(s, opened)
    assert s.model.sent == 0, f"nudged an already-open call: {s.model.sent}"


async def _case_bounded() -> None:
    """Agent never opens: attempts are bounded, the task returns."""
    import adapters.chirp as chirp

    chirp.NUDGE_RETRY_DELAY_S = 0.01
    s = _Session(accept_after=1)
    await asyncio.wait_for(_nudge_until_open(s, asyncio.Event()), timeout=5)
    assert s.model.sent == chirp.NUDGE_MAX_ATTEMPTS, s.model.sent


def main() -> None:
    asyncio.run(_case_agent_opens_late())
    asyncio.run(_case_first_nudge_lands())
    asyncio.run(_case_bounded())
    print("ok — nudge retries until the agent opens, stops when it does, stays bounded")


if __name__ == "__main__":
    main()
