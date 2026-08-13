"""Self-check for the upsert settle delay (MIVAS_UPSERT_SETTLE_SECONDS).

A POST that beats ClickHouse ingest links a trace and extracts zero tools, which reads
as "the agent called nothing" and fails every tool-requiring criterion. Asserts the
delay is applied only when configured, and that the default path is untouched.

    uv run python voice-agent-harnesses/openai/test_upsert_settle.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402


class _Resp:
    status_code = 200

    def json(self):
        return {"simulation_result": {"status": "CONVERSATION_ENDED"}}


class _Client:
    async def get(self, *a, **kw):
        return _Resp()


async def _run(settle: str | None) -> float:
    os.environ["MIVAS_UPSERT_BEFORE_EVAL"] = "1"
    os.environ.setdefault("BLUEJAY_API_KEY", "test-key")
    if settle is None:
        os.environ.pop("MIVAS_UPSERT_SETTLE_SECONDS", None)
    else:
        os.environ["MIVAS_UPSERT_SETTLE_SECONDS"] = settle
    t0 = time.monotonic()
    st = await report._await_terminal_upsert(_Client(), "1", timeout=5.0)
    assert st == "CONVERSATION_ENDED", st
    return time.monotonic() - t0


def main() -> None:
    took = asyncio.run(_run("1.5"))
    assert took >= 1.5, f"settle not applied: {took:.2f}s"
    took = asyncio.run(_run("0"))
    assert took < 0.5, f"explicit zero delayed: {took:.2f}s"
    took = asyncio.run(_run(None))
    assert took < 0.5, f"unset path delayed: {took:.2f}s"
    print("ok — settle honoured when set, zero-cost when unset")


if __name__ == "__main__":
    main()
