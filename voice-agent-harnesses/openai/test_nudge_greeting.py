"""The connect-time nudge must be a bare response.create — no user item."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters.chirp import _nudge_greeting  # noqa: E402


def test_nudge_is_bare_response_create() -> None:
    from agents.realtime.openai_realtime import _ConversionHelper

    sent = []

    class FakeModel:
        async def send_event(self, event):
            sent.append(event)

    class FakeSession:
        model = FakeModel()

    asyncio.run(_nudge_greeting(FakeSession()))

    assert len(sent) == 1, sent
    # The SDK silently drops raw messages it can't validate — prove it converts.
    converted = _ConversionHelper.try_convert_raw_message(sent[0])
    assert converted is not None, "raw nudge failed SDK validation"
    assert converted.type == "response.create", converted.type
    # No conversation item => no phantom user.turn / customer.speech span.
    assert "user_input" not in sent[0].message
    assert not sent[0].message.get("other_data")


if __name__ == "__main__":
    test_nudge_is_bare_response_create()
    print("ok nudge = bare response.create")
