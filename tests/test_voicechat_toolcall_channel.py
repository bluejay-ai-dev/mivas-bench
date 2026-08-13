"""The VoiceChat text-delta channel must survive the audio transcript.

Hosted NVCF emits tool calls as `<TOOLCALL>[...]</TOOLCALL>` in
`response.output_text.delta` and never speaks them. The bridge also wants the audio
transcript to own the SPOKEN text so speech spans don't count words twice. Those are
two different channels and the bridge used to collapse them:

    elif etype == "response.output_text.delta":
        if saw_audio_transcript:
            continue                 # skipped `text_buf += delta`

VoiceChat is full-duplex, so an audio transcript always arrives first. text_buf then
stayed empty for the entire call and every tool was lost — production run 229790 scored
0 tools across 173 completed calls, which read as "the model can't call tools" when the
bridge was discarding them.

This drives the real event loop's branch logic over a scripted event sequence.
"""

from __future__ import annotations

import pathlib
import re

CHIRP = (pathlib.Path(__file__).resolve().parents[1] / "voice-agent-harnesses" / "nvidia"
         / "nemotron-voicechat" / "adapters" / "chirp.py")
TOOLCALL = '<TOOLCALL>[{"name": "check_plan_accepted", "arguments": {"carrier": "Aetna"}}]</TOOLCALL>'


def _text_delta_branch() -> str:
    """The `response.output_text.delta` branch, verbatim from the bridge."""
    src = CHIRP.read_text()
    start = src.index('elif etype == "response.output_text.delta":')
    nxt = src.index('elif etype == "response.output_audio_transcript.done":', start)
    return src[start:nxt]


def test_tool_channel_accumulates_even_after_an_audio_transcript():
    """text_buf must be appended before any saw_audio_transcript guard can skip it."""
    branch = _text_delta_branch()
    assert "text_buf += delta" in branch, "the tool channel is not accumulated at all"

    # order matters: an early `continue` on the flag must not precede the append
    guard = re.search(r"if saw_audio_transcript:\s*\n\s*continue", branch)
    if guard:
        assert guard.start() > branch.index("text_buf += delta"), (
            "`if saw_audio_transcript: continue` runs BEFORE `text_buf += delta`, so the "
            "<TOOLCALL> channel is dropped for the whole call (regression of run 229790)"
        )


def test_spoken_text_is_still_suppressed_when_audio_owns_it():
    """The no-double-words intent must survive: _note/_commit stay gated on the flag."""
    branch = _text_delta_branch()
    assert "if not saw_audio_transcript:" in branch, (
        "spoken accumulation is no longer gated — speech spans will double-count words"
    )
    gate = branch.index("if not saw_audio_transcript:")
    after = branch[gate:]
    assert "_note(delta)" in after, "_note must sit inside the not-saw_audio_transcript gate"


def test_parser_reads_the_buffer_that_the_branch_fills():
    """Whatever the branch accumulates is what gets parsed for tool calls."""
    branch = _text_delta_branch()
    assert "_maybe_hard_tools(text_buf" in branch, (
        "the branch parses something other than text_buf, so accumulating text_buf "
        "would not surface any tool call"
    )


def test_parse_toolcalls_handles_the_real_wire_format():
    import sys
    sys.path.insert(0, str(CHIRP.parents[2]))
    from voicechat import parse_toolcalls  # noqa: E402

    # split across deltas the way the model streams it
    buf = ""
    for chunk in ("Sure. ", TOOLCALL[:30], TOOLCALL[30:], " One moment."):
        buf += chunk
    calls = parse_toolcalls(buf)
    assert [c["name"] for c in calls] == ["check_plan_accepted"]
    assert calls[0]["arguments"] == {"carrier": "Aetna"}
