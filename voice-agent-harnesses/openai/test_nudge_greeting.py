"""The connect-time nudge must be a bare response.create the SDK accepts.

A raw message the SDK can't validate is dropped with a log line rather than an error,
so a bad one shows up only as an agent that never opens the call.
Run: `python test_nudge_greeting.py`
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters.chirp import NUDGE_GREETING  # noqa: E402
from agents.realtime.openai_realtime import _ConversionHelper  # noqa: E402

converted = _ConversionHelper.try_convert_raw_message(NUDGE_GREETING)
assert converted is not None, "raw nudge failed SDK validation"
assert converted.type == "response.create", converted.type
# no conversation item => no phantom user.turn / customer.speech span
assert not NUDGE_GREETING.message.get("other_data"), NUDGE_GREETING.message

print("ok nudge = bare response.create")
