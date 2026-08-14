"""Nemotron healthcare greeting + VoiceChat echo barge-in regressions.

Cascaded run 230627: all 6 healthcare DHs NO_ANSWER. Reception.md says the
greeting already played, Flows never TTS'd the pack opener, Bluejay hung up
after 120s of silence.

VoiceChat run 230628: connected and talked, but CHIRP VAD on agent echo fired
`barge_in:chirp` and dropped remaining TTS (`customer_speaking` / `_user_live`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NVIDIA = ROOT / "voice-agent-harnesses" / "nvidia"
BOT = NVIDIA / "bot.py"
CHIRP = NVIDIA / "nemotron-voicechat" / "adapters" / "chirp.py"


def test_healthcare_blueprint_exposes_greeting() -> None:
    if str(NVIDIA) not in sys.path:
        sys.path.insert(0, str(NVIDIA))
    from harness import load_blueprint  # noqa: E402

    bp = load_blueprint(ROOT / "industries" / "healthcare")
    assert "Straus Dermatology" in bp["greeting"]
    control = load_blueprint(ROOT / "industries" / "control-industry")
    assert control["greeting"] == ""


def test_cascaded_bot_speaks_pack_greeting_without_waiting_on_llm() -> None:
    src = BOT.read_text()
    assert "TTSSpeakFrame" in src
    assert "tts.queue_frame" in src
    assert "_speak_pack_greeting" in src
    # Run 230683: sleep-poll of tts._started never flipped on this Pipecat and
    # 8s of asyncio.sleep took 31s wall because the loop was blocked. Wait on
    # the Event set when NonblockingNvidiaTTSService.start() finishes.
    assert "_await_tts_ready" not in src
    assert 'getattr(tts, "_started"' not in src
    assert "ready.wait()" in src
    # Run 230706: queueing TTSSpeakFrame before start() finished dropped it.
    assert "greeting anyway" not in src
    # initialize must not gate the greeting — 6-way NO_ANSWER was initialize()
    # blocking on a starved LLMRun before any TTS left the pod.
    connected = src[src.index("async def _connected") :]
    assert "await flow_manager.initialize" not in connected.split("async def ")[0]
    assert "create_task(_speak_pack_greeting" in connected
    assert "create_task(_init_start_node" in connected
    # Healthcare pack greeting already plays; opening LLMRun timed out and the
    # caller never got a reply after "I'd like the earliest appointment".
    assert "respond_immediately=not has_greeting" in src


def test_cascaded_nvidia_client_init_is_off_loop() -> None:
    if str(NVIDIA) not in sys.path:
        sys.path.insert(0, str(NVIDIA))
    from harness import io_workers  # noqa: E402

    src = (NVIDIA / "harness.py").read_text()
    assert "asyncio.to_thread(self._initialize_client)" in src
    assert "asyncio.to_thread(self._blocking_start)" in src
    assert "self.ready = asyncio.Event()" in src
    assert "install_io_executor" in src
    assert "attach_magpie" in src
    assert "warm_magpie" in src
    assert io_workers() >= 8
    # Run 230744: a semaphore around run_tts delayed the last sentences of a
    # turn past Pipecat's TTS-context cleanup, so the agent cut off mid-word.
    assert "tts_slots" not in src
    bot = BOT.read_text()
    assert "asyncio.to_thread(SileroVADAnalyzer)" in bot
    assert "install_io_executor" in bot
    cascaded = (NVIDIA / "adapters" / "chirp.py").read_text()
    assert "warm_magpie" in cascaded
    assert "install_io_executor" in cascaded


def test_magpie_channel_is_per_call_and_config_is_shared() -> None:
    """Run 230716: every call shared one SpeechSynthesisService, so the first DH
    to hang up ran _close_client() on it and the other five TTS'd into a dead
    channel ("Cannot invoke RPC: Channel closed!"). Only the config may be
    process-wide; the gRPC channel must belong to one call."""
    if str(NVIDIA) not in sys.path:
        sys.path.insert(0, str(NVIDIA))
    import harness  # noqa: E402

    harness._MAGPIE_CONFIG = None

    class FakeTTS:
        rpcs = 0

        def __init__(self) -> None:
            self._service = object()  # a distinct channel per call
            self._config = None

        def _initialize_client(self) -> None:
            pass

        def _create_synthesis_config(self):
            type(self).rpcs += 1
            return "config"

        def _load_zero_shot_audio_prompt(self) -> None:
            pass

    calls = [FakeTTS() for _ in range(6)]
    for tts in calls:
        harness.attach_magpie(tts)

    # GetRivaSynthesisConfig — the RPC that hung six-way — runs exactly once.
    assert FakeTTS.rpcs == 1
    assert all(t._config == "config" for t in calls)
    # ...but no two calls may end up on the same channel.
    assert len({id(t._service) for t in calls}) == 6

    harness._MAGPIE_CONFIG = None


def test_io_workers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    if str(NVIDIA) not in sys.path:
        sys.path.insert(0, str(NVIDIA))
    import harness  # noqa: E402

    monkeypatch.setenv("NEMOTRON_IO_WORKERS", "4")
    with pytest.raises(ValueError):
        harness.io_workers()


def test_voicechat_does_not_barge_on_chirp_vad_alone() -> None:
    src = CHIRP.read_text()
    start = src.index('if etype == "speech.started":')
    nxt = src.index('elif etype == "speech.completed":', start)
    branch = src[start:nxt]
    assert "_real_barge_in" in branch
    assert "barge_in:chirp" not in branch
    assert 'ctl["last_user_loud"] = time.monotonic()' not in branch, (
        "speech.started must not stamp last_user_loud — that made echo look like "
        "a live caller and muted the rest of the agent turn"
    )


def test_voicechat_forwards_agent_pcm_unless_real_user_barge() -> None:
    src = CHIRP.read_text()
    start = src.index('if etype == "response.output_audio.delta":')
    nxt = src.index('elif etype in {"response.output_audio.done", "response.done"}:', start)
    branch = src[start:nxt]
    assert 'if ctl["customer_speaking"]' not in branch
    assert "if _user_live(now):" not in branch
    assert "_real_barge_in" in branch
    send = branch.index("await ws.send(pcm16)")
    barge = branch.index("if _real_barge_in(now):")
    assert barge < send
