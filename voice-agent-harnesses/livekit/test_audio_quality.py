"""Audio-quality gate: chopped TTS must fail even when the smoke rubric is green."""

from __future__ import annotations

from pathlib import Path

from audio_quality import score_audio_quality

# Alice 728130: 6 dropouts, mid-sentence cuts. Rubric still passed.
_ALICE = [
    {
        "speaker": "Agent",
        "utterance": "Thank you for calling Strauss Dermatology. This is Robin, an assistant. How can I help you?",
    },
    {
        "speaker": "A5-06 Alice Romano (itemisation)",
        "utterance": "Hi, I’m calling about a balance on my account.",
    },
    {"speaker": "Agent", "utterance": "Your current"},
    {"speaker": "Agent", "utterance": "I've got a balance of $340 on the"},
    {"speaker": "Agent", "utterance": "Here's the breakdown. $50 is a missed visit"},
]

_CLEAN = [
    {
        "speaker": "Agent",
        "utterance": "Thank you for calling Strauss Dermatology. This is Robin, an AI assistant. How can I help you?",
    },
    {
        "speaker": "A2-02 Jordan Lee (cancel inside the window)",
        "utterance": "Hi, I’d like to cancel my upcoming follow-up appointment.",
    },
    {
        "speaker": "Agent",
        "utterance": "I just need to confirm a couple of things. Could you please tell me the full name and date of birth of the patient?",
    },
    {"speaker": "Agent", "utterance": "Got it."},
]


def test_alice_chops_fail_the_gate() -> None:
    result = score_audio_quality(
        dropouts=6,
        clipping=0,
        transcript=_ALICE,
        expected_greeting_prefix="Thank you for calling",
    )
    assert result["ok"] is False
    joined = " ".join(result["defects"])
    assert "dropouts=6" in joined
    assert "Your current" in joined
    assert "on the" in joined
    assert "missed visit" in joined


def test_clean_call_passes_the_gate() -> None:
    result = score_audio_quality(
        dropouts=0,
        clipping=0,
        transcript=_CLEAN,
        expected_greeting_prefix="Thank you for calling",
    )
    assert result["ok"] is True, result["defects"]


def test_chopped_greeting_fails() -> None:
    result = score_audio_quality(
        dropouts=0,
        clipping=0,
        transcript=[
            {
                "speaker": "Agent",
                "utterance": "This is Robin, an AI assistant. How can I help you?",
            }
        ],
        expected_greeting_prefix="Thank you for calling",
    )
    assert result["ok"] is False
    assert any("does not start with" in d for d in result["defects"])


def test_cascaded_session_does_not_eager_cut() -> None:
    """eager EOT + preemptive TTS + hair-trigger VAD is what chopped Alice."""
    src = (Path(__file__).parent / "cascaded" / "agent.py").read_text()
    assert "eager_eot_threshold" not in src
    assert "preemptive_generation" in src
    assert '"enabled": False' in src or "'enabled': False" in src
    assert "min_words" in src
    assert 'greet="say"' in src or "greet='say'" in src


def test_greeting_waits_for_caller() -> None:
    src = (Path(__file__).parent / "harness.py").read_text()
    assert "wait_for_participant" in src
    assert "allow_interruptions=False" in src


if __name__ == "__main__":
    test_alice_chops_fail_the_gate()
    test_clean_call_passes_the_gate()
    test_chopped_greeting_fails()
    test_cascaded_session_does_not_eager_cut()
    test_greeting_waits_for_caller()
    print("ok")
