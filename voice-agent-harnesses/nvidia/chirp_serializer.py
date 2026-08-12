"""CHIRP (16 kHz pcm_s16le + speech.* JSON) ↔ Pipecat FastAPIWebsocketTransport."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

EmitFn = Callable[[str | bytes], Awaitable[None]]


def _speech_event(etype: str, utterance_id: str) -> str:
    return json.dumps(
        {
            "type": etype,
            "id": str(uuid.uuid4()),
            "ts_ms": int(time.time() * 1000),
            "data": {"utterance_id": utterance_id},
        },
        separators=(",", ":"),
    )


class ChirpFrameSerializer(FrameSerializer):
    """Raw PCM in/out at 16 kHz; emits speech.started/completed around agent audio."""

    class InputParams(FrameSerializer.InputParams):
        sample_rate: int = 16000
        num_channels: int = 1

    def __init__(self, params: InputParams | None = None, *, emit: EmitFn | None = None):
        params = params or ChirpFrameSerializer.InputParams()
        super().__init__(params)
        self._params: ChirpFrameSerializer.InputParams = params
        self._emit = emit
        self._utt: str | None = None

    def set_emit(self, emit: EmitFn | None) -> None:
        self._emit = emit

    async def setup(self, frame: StartFrame):
        _ = frame

    async def _close_utt(self) -> str | None:
        if self._utt is None:
            return None
        uid = self._utt
        self._utt = None
        event = _speech_event("speech.completed", uid)
        if self._emit is not None:
            await self._emit(event)
            return None
        return event

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if self.should_ignore_frame(frame):
            return None

        if isinstance(frame, InterruptionFrame):
            return await self._close_utt()

        if isinstance(frame, OutputAudioRawFrame):
            if not frame.audio:
                return None
            if self._utt is None:
                self._utt = f"u_{uuid.uuid4().hex[:12]}"
                started = _speech_event("speech.started", self._utt)
                if self._emit is not None:
                    await self._emit(started)
                else:
                    # No side-channel: prefer starting the utterance marker; the
                    # first audio chunk follows on the next OutputAudioRawFrame.
                    return started
            return bytes(frame.audio)

        if isinstance(frame, (EndFrame, CancelFrame)):
            return await self._close_utt()

        return None

    async def deserialize(self, data: str | bytes) -> Frame | list[Frame] | None:
        if isinstance(data, bytes):
            if not data:
                return None
            return InputAudioRawFrame(
                audio=data,
                sample_rate=self._params.sample_rate,
                num_channels=self._params.num_channels,
            )
        # Bluejay speech.* text frames are handled in the chirp adapter for OTel.
        return None
