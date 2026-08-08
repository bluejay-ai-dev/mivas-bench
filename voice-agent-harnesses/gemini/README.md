# gemini

Gemini Live harnesses via the `google-genai` SDK. Each subfolder is one model runtime; industry tools go through the state API (`TOOL_SERVER_URL`).

| Folder | Model |
|---|---|
| `flash-live-3.1/` | [`gemini-3.1-flash-live-preview`](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview) |
| `2.5-flash-native-audio/` | `gemini-2.5-flash-native-audio-preview-12-2025` |

Shared: `harness.py`. Run a runtime with `agent.py` (text turns on stdin). Optional pcm websocket bridge under `adapters/` if an external evaluator needs one.

```bash
uv sync
export GOOGLE_API_KEY=...
uv run python industries/control-industry/tool_server.py
uv run python voice-agent-harnesses/gemini/flash-live-3.1/agent.py control-industry
# optional: adapters/chirp.py (16 kHz in, 24→16 kHz out)
```
