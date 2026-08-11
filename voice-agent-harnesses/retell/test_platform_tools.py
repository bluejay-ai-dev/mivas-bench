"""Check the call-record → span mapping against a real Retell payload.

Excerpt from call_364d5c477f2439013ac9b093d80. Run: `python test_platform_tools.py`
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import handoff_tool_names, platform_tool_calls  # noqa: E402

RECORD = {
    "start_timestamp": 1786384392155,
    "transcript_with_tool_calls": [
        {"role": "agent", "content": "Welcome to Bluejay's Repair Services!"},
        {
            "role": "tool_call_invocation",
            "tool_call_id": "call_3Osr",
            "name": "transition_to_scheduler",
            "arguments": "{}",
            "time_sec": 18.524,
        },
        {
            "role": "tool_call_invocation",
            "tool_call_id": "call_pXbW",
            "name": "schedule_appointment",
            "arguments": '{"date":"08/18/2026"}',
            "time_sec": 38.143,
            "type": "custom",
        },
        {
            "role": "tool_call_result",
            "tool_call_id": "call_pXbW",
            "successful": True,
            "time_sec": 38.455,
        },
        {
            "role": "tool_call_invocation",
            "tool_call_id": "call_wqGU",
            "name": "end_call",
            "arguments": '{"execution_message":"Goodbye!"}',
            "time_sec": 63.191,
            "type": "end_call",
        },
    ],
}
BP = {"agents": {"receptionist": {"tools": [{"name": "handoff_to_scheduler", "handoff": True, "handoff_to": "scheduler"}]}}}

calls = platform_tool_calls(RECORD, handoff_tool_names(BP))

# schedule_appointment hit our webhook and already has a live span — must not double up.
assert [c["name"] for c in calls] == ["handoff_to_scheduler", "end_call"], calls
# the edge tool is reported under the blueprint name, with Retell's name kept alongside.
assert calls[0]["provider_name"] == "transition_to_scheduler"
# time_sec is relative to start_timestamp (epoch ms) → epoch ns.
assert calls[0]["start_ns"] == 1786384392155 * 1_000_000 + 18_524_000_000
# no result row for a transition, so it gets a 10 ms sliver rather than zero width.
assert calls[0]["end_ns"] - calls[0]["start_ns"] == 10_000_000
assert calls[1]["arguments"] == {"execution_message": "Goodbye!"}
# a record with no start_timestamp can't be placed on the timeline — emit nothing.
assert platform_tool_calls({"transcript_with_tool_calls": RECORD["transcript_with_tool_calls"]}, {}) == []

print("ok", [c["name"] for c in calls])
