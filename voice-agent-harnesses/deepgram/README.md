# deepgram

Deepgram Voice Agent harness via raw `websockets` (no SDK) — one WS session
(`wss://agent.deepgram.com/v1/agent/converse`) bundles listen (STT) + think (LLM)
+ speak (TTS). Industry tools go through the state API (`TOOL_SERVER_URL`).

| Folder | Model |
|---|---|
| `voice-agent/` | `deepgram-voice-agent` (flux-general-en listen, gpt-4.1 think, flux-hannah-en speak) |

Speak uses [Flux TTS](https://developers.deepgram.com/docs/flux-tts/overview) — any
`flux-{voice}-{lang}` model gets `version: v2` on the speak provider automatically; set
`DEEPGRAM_SPEAK_MODEL=aura-2-thalia-en` to fall back to Aura.

Shared: `harness.py` (builds the `Settings` payload from the blueprint). Tracing: `report.py`
(GenAI-native OTel → Bluejay OTLP; `gen_ai.provider.name=deepgram`). Run a runtime with
`agent.py` (`--check` validates the blueprint locally, and — with `DEEPGRAM_API_KEY` set —
also connects and sends `Settings` as a smoke test). Optional pcm websocket bridge under
`adapters/` if an external evaluator needs one.

```bash
uv sync
export DEEPGRAM_API_KEY=...
# optional overrides: DEEPGRAM_LISTEN_MODEL, DEEPGRAM_THINK_MODEL, DEEPGRAM_SPEAK_MODEL, DEEPGRAM_GREETING
# optional Bluejay traces: BLUEJAY_API_KEY (+ BLUEJAY_SERVICE_NAME=mivas-deepgram)
uv run python industries/control-industry/tool_server.py
uv run python voice-agent-harnesses/deepgram/voice-agent/agent.py control-industry
# optional: adapters/chirp.py (16 kHz in, 24↔16 kHz out; links X-Simulation-Result-Id)
```
