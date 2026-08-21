# gemini

Gemini Live over LiveKit SIP. Each subfolder is one model. Industry tools go
to `TOOL_SERVER_URL`. Bluejay reaches the worker with `connection_type=SIP`.

| Folder | Model |
|---|---|
| `flash-live-3.1/` | `gemini-3.1-flash-live-preview` |
| `2.5-flash-native-audio/` | `gemini-2.5-flash-native-audio` |

Audio is Gemini's. 3.1 cannot change instructions or tools mid-session, so a
handoff opens a new Live socket for the target agent. 2.5 can
`generate_reply`; it still uses one Live socket per agent because Gemini Live
tools are fixed at connect.

One Live call per worker process. Extra rooms go to other replicas.

```bash
export GOOGLE_API_KEY=...
export LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
uv run python industries/control-industry/tool_server.py
.venv/bin/python voice-agent-harnesses/gemini/flash-live-3.1/agent.py start
```

`--check` loads the blueprint without LiveKit.
