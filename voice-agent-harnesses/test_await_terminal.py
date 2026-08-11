"""Guard both halves of the tool double-count trap in every harness's report.py.

Bluejay re-extracts the execute_tool spans on every update-simulation-result POST
and APPENDS them, so a second POST gives each expected tool two actuals. Two ways
to trip it: the wait returning mid-eval, or the relink firing when eval never wiped
the link. See §6b of PROVIDER_INTEGRATION_HANDOFF.md.

Run: ./.venv/bin/python voice-agent-harnesses/test_await_terminal.py
"""

import asyncio
import importlib
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent
HARNESSES = (
    "openai gemini deepgram assemblyai elevenlabs "
    "vapi bland cartesia retell livekit pipecat"
).split()
TRACKED = 5  # the rest are untracked WIP: skip them rather than hard-fail
os.environ.setdefault("BLUEJAY_API_KEY", "test-key")


class Feed:
    """Fake httpx client: scripted statuses, counting polls and POSTs."""

    status_code, text = 200, ""

    def __init__(self, *statuses, trace_ids=()):
        self.statuses, self.trace_ids = statuses, list(trace_ids)
        self.polls = self.posts = 0

    async def get(self, url, headers=None):
        self.polls += 1
        return self

    async def post(self, url, json=None, headers=None):
        self.posts += 1
        return self

    def json(self):
        st = self.statuses[min(self.polls - 1, len(self.statuses) - 1)]
        return {"simulation_result": {"status": st, "trace_ids": self.trace_ids}}


async def main():
    checked, skipped = [], []
    for name in HARNESSES:
        if not (BASE / name / "report.py").is_file():
            skipped.append(name)
            continue
        sys.path.insert(0, str(BASE / name))
        sys.modules.pop("report", None)
        mod = importlib.import_module("report")
        sys.path.pop(0)
        wait = mod._await_terminal_upsert

        if not hasattr(mod, "_relink_after_final"):
            f = Feed("EVALUATING", "COMPLETED")
            got = await wait(f, "1", timeout=30)
            assert (got, f.polls, f.posts) == ("COMPLETED", 2, 0)
            checked.append(name)
            print(f"ok {name} (single-post final-status contract)")
            continue

        relink = mod._relink_after_final

        assert wait.__defaults__[-1] == 300.0, f"{name}: budget under eval's ~175 s"
        assert await wait(Feed("EVALUATING"), "1", timeout=2.5) is None, f"{name}: posts mid-eval"
        f = Feed("EVALUATING", "EVALUATING", "COMPLETED")
        got = await wait(f, "1", timeout=30)
        assert (got, f.polls) == ("COMPLETED", 3), f"{name}: settled {got} after {f.polls} poll(s)"

        f = Feed("COMPLETED", trace_ids=["abc"])
        await relink(f, "1", {}, "k", "abc", "EVALUATING")
        assert f.posts == 0, f"{name}: relinked over a live link, double-counts every tool"
        f = Feed("COMPLETED")
        await relink(f, "1", {}, "k", "abc", "EVALUATING")
        assert f.posts == 1, f"{name}: no relink after eval wiped the link"

        checked.append(name)
        print(f"ok {name}")

    print(f"skipped (no report.py here): {', '.join(skipped)}" if skipped else "")
    assert len(checked) >= TRACKED, f"only checked {checked}"
    print(f"{len(checked)} harnesses: wait holds for final, relink only on a wiped link")


asyncio.run(main())
