#!/usr/bin/env python3
"""Talk to a MIVAS voice agent.

Default: microphone in, speakers out (OpenAI Realtime).
Pass --text for a typed REPL instead.

Examples:
  python tests/converse.py
  python tests/converse.py --harness openai --industry control-industry
  python tests/converse.py --text
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import queue
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from helpers import load_dotenv, start_tool_server, stop_process  # noqa: E402

CHUNK_LENGTH_S = 0.04
SAMPLE_RATE = 24000
CHANNELS = 1
# Echo from laptop speakers easily exceeds the OpenAI demo's 0.015 gate and
# triggers barge-in ~0.5s into playback. Default = mute mic while assistant talks.
ENERGY_THRESHOLD = 0.08
PREBUFFER_CHUNKS = 6
FADE_OUT_MS = 12
PLAYBACK_ECHO_MARGIN = 0.05
PLAYBACK_MIC_HOLD_S = 0.35


def _load_harness_module(harness: str) -> Any:
    """Load family/runtime agent.py (e.g. openai/realtime-2.1)."""
    if "/" not in harness:
        raise ValueError(
            f"harness must be family/runtime (e.g. openai/realtime-2.1), got {harness!r}"
        )
    family, runtime = harness.split("/", 1)
    family_dir = ROOT / "voice-agent-harnesses" / family
    agent_path = family_dir / runtime / "agent.py"
    if not agent_path.is_file():
        raise FileNotFoundError(f"no agent.py for harness={harness}")
    # runtime.py lives on the family package path
    family_s = str(family_dir)
    if family_s not in sys.path:
        sys.path.insert(0, family_s)
    spec = importlib.util.spec_from_file_location(
        f"mivas_harness_{family}_{runtime.replace('-', '_')}", agent_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {agent_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _item_text(item: Any) -> str:
    parts: list[str] = []
    for content in getattr(item, "content", None) or []:
        transcript = getattr(content, "transcript", None)
        text = getattr(content, "text", None)
        if transcript:
            parts.append(transcript)
        elif text:
            parts.append(text)
    return " ".join(parts).strip()


def _print_event(event: Any, *, verbose: bool = False) -> None:
    et = event.type
    if et == "history_added":
        item = event.item
        role = getattr(item, "role", None) or getattr(item, "type", "item")
        text = _item_text(item)
        if text:
            print(f"{role}> {text}")
    elif et == "handoff":
        print(f"handoff> {event.from_agent.name} → {event.to_agent.name}")
    elif et == "tool_start":
        name = getattr(event.tool, "name", event.tool)
        print(f"tool_start> {name} {event.arguments}")
    elif et == "tool_end":
        name = getattr(event.tool, "name", event.tool)
        print(f"tool_end> {name} → {event.output}")
    elif et == "agent_start":
        print(f"agent> {event.agent.name}")
    elif et == "agent_end":
        if verbose:
            print(f"(turn done — agent={event.agent.name})")
    elif et == "error":
        print(f"error> {event.error}", file=sys.stderr)
    elif verbose and et not in {"audio", "history_updated", "raw_model_event"}:
        print(f"event> {et}")


class VoiceSession:
    """Mic capture + speaker playback for an OpenAI Realtime session."""

    def __init__(self, *, barge_in: bool = False) -> None:
        import numpy as np
        import sounddevice as sd
        from agents.realtime import RealtimePlaybackTracker

        self.np = np
        self.sd = sd
        self.FORMAT = np.int16
        self.barge_in = barge_in
        self.session: Any = None
        self.audio_stream: Any = None
        self.audio_player: Any = None
        self.recording = False
        self.audio_capture_task: asyncio.Task[None] | None = None
        self.playback_tracker = RealtimePlaybackTracker()
        self.output_queue: queue.Queue[Any] = queue.Queue(maxsize=0)
        self.interrupt_event = threading.Event()
        self.current_audio_chunk: tuple[Any, str, int] | None = None
        self.chunk_position = 0
        self.prebuffering = True
        self.prebuffer_target_chunks = PREBUFFER_CHUNKS
        self.fading = False
        self.fade_total_samples = 0
        self.fade_done_samples = 0
        self.fade_samples = int(SAMPLE_RATE * (FADE_OUT_MS / 1000.0))
        self.playback_rms = 0.0
        self._assistant_playing = False
        self._mic_hold_until = 0.0

    def _compute_rms(self, samples: Any) -> float:
        if samples.size == 0:
            return 0.0
        x = samples.astype(self.np.float32) / 32768.0
        return float(self.np.sqrt(self.np.mean(x * x)))

    def _update_playback_rms(self, samples: Any) -> None:
        sample_rms = self._compute_rms(samples)
        self.playback_rms = 0.9 * self.playback_rms + 0.1 * sample_rms

    def _output_callback(self, outdata: Any, frames: int, time: Any, status: Any) -> None:
        if status:
            print(f"audio out status: {status}")

        if self.interrupt_event.is_set():
            outdata.fill(0)
            if self.current_audio_chunk is None:
                while not self.output_queue.empty():
                    try:
                        self.output_queue.get_nowait()
                    except queue.Empty:
                        break
                self.prebuffering = True
                self.interrupt_event.clear()
                return

            if not self.fading:
                self.fading = True
                self.fade_done_samples = 0
                remaining_in_chunk = len(self.current_audio_chunk[0]) - self.chunk_position
                self.fade_total_samples = min(self.fade_samples, max(0, remaining_in_chunk))

            samples, item_id, content_index = self.current_audio_chunk
            samples_filled = 0
            while (
                samples_filled < len(outdata) and self.fade_done_samples < self.fade_total_samples
            ):
                remaining_output = len(outdata) - samples_filled
                remaining_fade = self.fade_total_samples - self.fade_done_samples
                n = min(remaining_output, remaining_fade)
                src = samples[self.chunk_position : self.chunk_position + n].astype(
                    self.np.float32
                )
                idx = self.np.arange(
                    self.fade_done_samples, self.fade_done_samples + n, dtype=self.np.float32
                )
                gain = 1.0 - (idx / float(max(self.fade_total_samples, 1)))
                ramped = self.np.clip(src * gain, -32768.0, 32767.0).astype(self.np.int16)
                outdata[samples_filled : samples_filled + n, 0] = ramped
                self._update_playback_rms(ramped)
                with suppress(Exception):
                    self.playback_tracker.on_play_bytes(
                        item_id=item_id, item_content_index=content_index, bytes=ramped.tobytes()
                    )
                samples_filled += n
                self.chunk_position += n
                self.fade_done_samples += n

            if self.fade_done_samples >= self.fade_total_samples:
                self.current_audio_chunk = None
                self.chunk_position = 0
                while not self.output_queue.empty():
                    try:
                        self.output_queue.get_nowait()
                    except queue.Empty:
                        break
                self.fading = False
                self.prebuffering = True
                self.interrupt_event.clear()
            return

        outdata.fill(0)
        samples_filled = 0
        while samples_filled < len(outdata):
            if self.current_audio_chunk is None:
                try:
                    if (
                        self.prebuffering
                        and self.output_queue.qsize() < self.prebuffer_target_chunks
                    ):
                        break
                    self.prebuffering = False
                    self.current_audio_chunk = self.output_queue.get_nowait()
                    self.chunk_position = 0
                except queue.Empty:
                    break

            remaining_output = len(outdata) - samples_filled
            samples, item_id, content_index = self.current_audio_chunk
            remaining_chunk = len(samples) - self.chunk_position
            n = min(remaining_output, remaining_chunk)
            if n > 0:
                chunk_data = samples[self.chunk_position : self.chunk_position + n]
                outdata[samples_filled : samples_filled + n, 0] = chunk_data
                self._update_playback_rms(chunk_data)
                samples_filled += n
                self.chunk_position += n
                with suppress(Exception):
                    self.playback_tracker.on_play_bytes(
                        item_id=item_id,
                        item_content_index=content_index,
                        bytes=chunk_data.tobytes(),
                    )
            if self.chunk_position >= len(samples):
                self.current_audio_chunk = None
                self.chunk_position = 0

    async def start(self) -> None:
        chunk_size = int(SAMPLE_RATE * CHUNK_LENGTH_S)
        self.audio_player = self.sd.OutputStream(
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            dtype=self.FORMAT,
            callback=self._output_callback,
            blocksize=chunk_size,
        )
        self.audio_player.start()

    async def start_mic(self) -> None:
        self.audio_stream = self.sd.InputStream(
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            dtype=self.FORMAT,
        )
        self.audio_stream.start()
        self.recording = True
        self.audio_capture_task = asyncio.create_task(self.capture_audio())

    async def stop(self) -> None:
        self.recording = False
        if self.audio_capture_task is not None:
            self.audio_capture_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.audio_capture_task
            self.audio_capture_task = None
        if self.audio_player and self.audio_player.active:
            self.audio_player.stop()
        if self.audio_player:
            self.audio_player.close()

    def _local_assistant_playing(self) -> bool:
        return (
            self._assistant_playing
            or self.current_audio_chunk is not None
            or not self.output_queue.empty()
            or time.monotonic() < self._mic_hold_until
        )

    def _session_alive(self) -> bool:
        session = self.session
        if session is None:
            return False
        return not getattr(session, "_closed", False) and not getattr(session, "_closing", False)

    async def capture_audio(self) -> None:
        if not self.audio_stream or not self.session:
            return
        read_size = int(SAMPLE_RATE * CHUNK_LENGTH_S)
        try:
            while self.recording and self._session_alive():
                if self.audio_stream.read_available < read_size:
                    await asyncio.sleep(0.01)
                    continue
                try:
                    data, _ = self.audio_stream.read(read_size)
                except Exception as e:
                    print(f"audio capture error: {e}", file=sys.stderr)
                    break
                audio_bytes = data.tobytes()

                # Default: don't upload mic while assistant audio is playing.
                # Laptop speaker echo otherwise trips server barge-in mid-utterance.
                if self._local_assistant_playing():
                    if not self.barge_in:
                        await asyncio.sleep(0)
                        continue
                    mic_rms = self._compute_rms(data.reshape(-1))
                    playback_gate = max(
                        ENERGY_THRESHOLD,
                        self.playback_rms * 1.5 + PLAYBACK_ECHO_MARGIN,
                    )
                    if mic_rms < playback_gate:
                        await asyncio.sleep(0)
                        continue
                    self.interrupt_event.set()

                try:
                    await self.session.send_audio(audio_bytes)
                except Exception:
                    # session closed (e.g. end_call) — expected, not a mic failure
                    break
                await asyncio.sleep(0)
        finally:
            self.recording = False
            if self.audio_stream and self.audio_stream.active:
                self.audio_stream.stop()
            if self.audio_stream:
                self.audio_stream.close()

    async def on_event(self, event: Any) -> None:
        _print_event(event)
        if event.type == "audio":
            self._assistant_playing = True
            np_audio = self.np.frombuffer(event.audio.data, dtype=self.np.int16)
            self.output_queue.put_nowait((np_audio, event.item_id, event.content_index))
        elif event.type == "audio_end":
            self._assistant_playing = False
            self._mic_hold_until = time.monotonic() + PLAYBACK_MIC_HOLD_S
        elif event.type == "audio_interrupted":
            self._assistant_playing = False
            self._mic_hold_until = time.monotonic() + PLAYBACK_MIC_HOLD_S
            self.prebuffering = True
            self.interrupt_event.set()


async def converse_voice(harness: str, industry: str, *, barge_in: bool = False) -> None:
    mod = _load_harness_module(harness)
    if not hasattr(mod, "build_from_blueprint"):
        raise RuntimeError(f"harness {harness} has no build_from_blueprint()")

    industry_dir = ROOT / "industries" / industry
    os.environ["INDUSTRY_DIR"] = str(industry_dir)
    os.environ.setdefault("TOOL_SERVER_URL", "http://127.0.0.1:8000")

    runner = mod.build_from_blueprint(industry_dir)
    voice = VoiceSession(barge_in=barge_in)

    print(f"voice chat: {harness} × {industry}")
    if barge_in:
        print("barge-in on — use headphones or echo will cut off replies")
    else:
        print("mic muted while agent speaks (use --barge-in + headphones to interrupt)")
    print("speak into your mic — Ctrl+C to quit\n")

    await voice.start()
    try:
        # Disable server-side interrupt unless barge-in requested; speaker echo
        # otherwise cancels the assistant after ~0.5s.
        model_config: dict[str, Any] = {
            "playback_tracker": voice.playback_tracker,
            "initial_model_settings": {
                "turn_detection": {
                    "type": "semantic_vad",
                    "interrupt_response": barge_in,
                    "create_response": True,
                },
            },
        }
        ctx: dict[str, Any] = {}
        async with await runner.run(context=ctx, model_config=model_config) as session:
            ctx["session"] = session
            voice.session = session
            await voice.start_mic()
            print("listening…")
            async for event in session:
                await voice.on_event(event)
    finally:
        await voice.stop()


async def converse_text(harness: str, industry: str) -> None:
    mod = _load_harness_module(harness)
    if not hasattr(mod, "build_from_blueprint"):
        raise RuntimeError(f"harness {harness} has no build_from_blueprint()")

    industry_dir = ROOT / "industries" / industry
    os.environ["INDUSTRY_DIR"] = str(industry_dir)
    os.environ.setdefault("TOOL_SERVER_URL", "http://127.0.0.1:8000")

    runner = mod.build_from_blueprint(industry_dir)
    turn_done = asyncio.Event()
    stop = asyncio.Event()

    print(f"text chat: {harness} × {industry}")
    print("type a message, or quit / exit / q to stop\n")

    ctx: dict[str, Any] = {}
    async with await runner.run(context=ctx) as session:
        ctx["session"] = session

        async def consume() -> None:
            async for event in session:
                _print_event(event, verbose=True)
                if event.type == "agent_end":
                    turn_done.set()
                if stop.is_set():
                    break

        consumer = asyncio.create_task(consume())
        try:
            while True:
                line = await asyncio.to_thread(input, "you> ")
                text = line.strip()
                if not text:
                    continue
                if text.lower() in {"quit", "exit", "q"}:
                    break
                turn_done.clear()
                await session.send_message(text)
                try:
                    await asyncio.wait_for(turn_done.wait(), timeout=120)
                except asyncio.TimeoutError:
                    print("(timed out waiting for agent_end)", file=sys.stderr)
        finally:
            stop.set()
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Speak (or type) with a MIVAS voice agent.")
    parser.add_argument(
        "--harness",
        default=os.environ.get("VOICE_AGENT", "openai/realtime-2.1"),
        help="Harness path family/runtime (default: $VOICE_AGENT or openai/realtime-2.1)",
    )
    parser.add_argument(
        "--industry",
        default=os.environ.get("INDUSTRY", "control-industry"),
        help="Industry pack (default: $INDUSTRY or control-industry)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TOOL_SERVER_PORT", "8000")),
        help="Tool server port",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Typed REPL instead of microphone/speakers",
    )
    parser.add_argument(
        "--barge-in",
        action="store_true",
        help="Allow interrupting the agent mid-speech (use headphones)",
    )
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY") and args.harness.startswith("openai"):
        print("OPENAI_API_KEY missing (set in root .env)", file=sys.stderr)
        sys.exit(1)

    if not args.text:
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
        except ImportError:
            print(
                "voice mode needs: uv sync   # installs sounddevice + numpy\n"
                "(from repo root)",
                file=sys.stderr,
            )
            sys.exit(1)

    tool_proc = start_tool_server(args.industry, port=args.port)
    os.environ["TOOL_SERVER_URL"] = f"http://127.0.0.1:{args.port}"
    try:
        if args.text:
            asyncio.run(converse_text(args.harness, args.industry))
        else:
            asyncio.run(
                converse_voice(args.harness, args.industry, barge_in=args.barge_in)
            )
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        stop_process(tool_proc)


if __name__ == "__main__":
    main()
