# gemini

Gemini Live over LiveKit SIP. Each subfolder is one model. Industry tools go
to `TOOL_SERVER_URL`. Bluejay reaches the worker with `connection_type=SIP`.

| Folder | Model |
|---|---|
| `flash-live-3.1/` | `gemini-3.1-flash-live-preview` |
| `2.5-flash-native-audio/` | `gemini-2.5-flash-native-audio` |
| `3.8-live/` | `gemini-3.8-live`; `3.8-live@extended` runs `gemini-3.8-live-extended-thinking` (`GEMINI_THINKING_LEVEL`, default HIGH via variants.json) |

Audio is Gemini's. 3.1 cannot change instructions or tools mid-session, so a
handoff opens a new Live socket for the target agent. 2.5 can
`generate_reply`; it still uses one Live socket per agent because Gemini Live
tools are fixed at connect.

One Live call per worker process. Extra rooms go to other replicas.

3.8 extended thinking, measured against the API: it needs a `thinking_level`,
it closes the socket (1007) on any `FunctionResponse.scheduling`, and one
caller turn becomes several generations (filler, tool call, answer), each
closed by `turn_complete` with `interaction_status=IN_PROGRESS`. The runtime
therefore sends no scheduling, keeps every generation's playout instead of
letting the next one cut it, and completes an empty turn after each tool
response so the result is not held WHEN_IDLE on a line that never idles.

```bash
export GOOGLE_API_KEY=...
export LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
uv run python industries/control-industry/tool_server.py
.venv/bin/python voice-agent-harnesses/gemini/flash-live-3.1/agent.py start
```

`--check` loads the blueprint without LiveKit.
