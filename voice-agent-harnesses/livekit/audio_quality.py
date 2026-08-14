"""Audio-quality gate for LiveKit smoke transcripts.

Bluejay `goal_success` and the utterances/tools/traces rubric can all pass while
the recording is chopped. Score dropouts, clipping, and truncated agent lines
before calling a run good.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MAX_DROPOUTS = 0
MAX_CLIPPING = 0.0
# Healthcare opener is ~90 chars; a chopped greeting is typically < 50.
MIN_GREETING_CHARS = 60
INCOMPLETE_TAIL = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "your",
    }
)
_ALLOWED_SHORT = frozenset(
    {
        "got it",
        "okay",
        "ok",
        "yes",
        "no",
        "sure",
        "thanks",
        "thank you",
        "you're welcome",
        "alright",
        "all right",
        "bye",
        "goodbye",
        "take care",
    }
)
_END_PUNCT = re.compile(r'[.!?]"?$')


def _text(turn: dict[str, Any]) -> str:
    return str(turn.get("utterance") or turn.get("text") or "").strip()


def _is_agent(turn: dict[str, Any]) -> bool:
    speaker = str(turn.get("speaker") or turn.get("role") or "").lower()
    return speaker == "agent" or speaker.startswith("agent")


def agent_turns(transcript: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in transcript if _is_agent(t)]


def truncated_agent_utterances(transcript: Iterable[dict[str, Any]]) -> list[str]:
    """Agent lines that stop mid-phrase (the Alice 'Your current' / 'on the' pattern)."""
    chopped: list[str] = []
    for turn in agent_turns(transcript):
        text = _text(turn)
        if not text:
            continue
        words = re.findall(r"[A-Za-z0-9$']+", text)
        if not words:
            continue
        compact = " ".join(words).lower().rstrip(".")
        if compact in _ALLOWED_SHORT:
            continue
        last = words[-1].lower().rstrip(".,!?")
        if last in INCOMPLETE_TAIL:
            chopped.append(text)
            continue
        # ASR of a cut-off sentence usually has no terminal punct, even if an
        # earlier clause already ended ("Here's the breakdown. $50 is a missed visit").
        if not _END_PUNCT.search(text):
            chopped.append(text)
    return chopped


def greeting_chopped(transcript: Iterable[dict[str, Any]], expected_prefix: str = "") -> str | None:
    agents = agent_turns(transcript)
    if not agents:
        return "no agent utterance"
    first = _text(agents[0])
    if expected_prefix and not first.lower().startswith(expected_prefix.lower()):
        return f"greeting {first!r} does not start with {expected_prefix!r}"
    if len(first) < MIN_GREETING_CHARS:
        return f"greeting too short ({len(first)} chars): {first!r}"
    return None


def score_audio_quality(
    *,
    dropouts: int | float | None,
    clipping: int | float | None,
    transcript: Iterable[dict[str, Any]],
    expected_greeting_prefix: str = "",
) -> dict[str, Any]:
    defects: list[str] = []
    if dropouts is None:
        defects.append("agent_audio_dropouts missing")
    elif dropouts > MAX_DROPOUTS:
        defects.append(f"agent_audio_dropouts={dropouts} (max {MAX_DROPOUTS})")
    if clipping is None:
        defects.append("agent_audio_clipping missing")
    elif clipping > MAX_CLIPPING:
        defects.append(f"agent_audio_clipping={clipping} (max {MAX_CLIPPING})")
    for line in truncated_agent_utterances(transcript):
        defects.append(f"truncated agent utterance: {line!r}")
    greeting = greeting_chopped(transcript, expected_greeting_prefix)
    if greeting:
        defects.append(greeting)
    return {"ok": not defects, "defects": defects}
