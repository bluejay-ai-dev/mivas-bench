# grok

xAI Grok Speech-to-Speech harnesses. Each subfolder is one model runtime.

| Folder | Model |
|---|---|
| `voice/` | [`grok-voice-latest`](https://docs.x.ai/developers/model-capabilities/audio/voice-agent) (alias for `grok-voice-think-fast-2.0`) |

Shared builder: `harness.py`. Tracing/reporting: `report.py` (`BLUEJAY_SERVICE_NAME=mivas-grok`, provider `xai`).

- Industry tools → industry state API (`TOOL_SERVER_URL`)
- Session tools (`session: true`, e.g. `end_call`) → harness-local + close
- Handoffs → hard dual-session switch (one Grok WS per blueprint agent; idle gets no audio)
- Speak-first: bare `response.create` after `session.updated` (pack owns greeting text)
- Audio: Grok PCM 24 kHz ↔ CHIRP 16 kHz `pcm_s16le` + `speech.started` / `speech.completed`
- Tracing → Bluejay OTel `voice.call` (`agent.speech` / `customer.speech` / `execute_tool`); Chirp stamps `X-Simulation-Result-Id` and POSTs `{trace_ids}`

```bash
uv sync
# GROK_API_KEY (or XAI_API_KEY) in root .env
uv run python run.py --harness grok/voice --mode check
uv run python industries/control-industry/tool_server.py
CHIRP_PORT=8768 CHIRP_USER=mivas CHIRP_PASS=mivas \
  uv run python run.py --harness grok/voice --mode chirp
```

## Env

| var | use |
|---|---|
| `GROK_API_KEY` | xAI API key (`Authorization: Bearer …`) |
| `XAI_API_KEY` | alias for `GROK_API_KEY` |
| `GROK_VOICE_MODEL` | default `grok-voice-latest` |
| `GROK_VOICE` | session voice (default `eve`) |
| `GROK_WS_URL` | override WS base (default `wss://api.x.ai/v1/realtime`) |
| `TOOL_SERVER_URL` | industry state API |
| `BLUEJAY_API_KEY` | OTel + `update-simulation-result` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | Bluejay websocket auth (default port `8768`) |
