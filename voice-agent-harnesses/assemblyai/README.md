# assemblyai

AssemblyAI Voice Agent harness via the raw `wss://agents.assemblyai.com/v1/ws` API — inline session config (system prompt, greeting, tools, audio format) sent on connect, no stored agent. Industry tools go through the state API (`TOOL_SERVER_URL`).

| Folder | Model |
|---|---|
| `voice-agent/` | [`assemblyai-voice-agent`](https://www.assemblyai.com/docs/voice-agents/voice-agent-api) |

Shared: `harness.py`. Tracing: `report.py` (GenAI-native OTel → Bluejay OTLP; `gen_ai.provider.name=assemblyai`). Run a runtime with `agent.py` (text turns via `conversation.message` + `reply.create` on stdin). Optional pcm websocket bridge under `adapters/` if an external evaluator needs one.

```bash
uv sync
export ASSEMBLYAI_API_KEY=...
# optional Bluejay traces: BLUEJAY_API_KEY (+ BLUEJAY_SERVICE_NAME=mivas-assemblyai)
# optional: ASSEMBLYAI_VOICE (default alba), ASSEMBLYAI_GREETING
uv run python industries/control-industry/tool_server.py
uv run python voice-agent-harnesses/assemblyai/voice-agent/agent.py control-industry
# optional: adapters/chirp.py (16 kHz in, 24→16 kHz out; links X-Simulation-Result-Id)
```
