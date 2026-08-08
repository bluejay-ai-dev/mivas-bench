# openai

OpenAI Realtime harnesses. Each subfolder is one model runtime.

| Folder | Model |
|---|---|
| `realtime-2.1/` | [`gpt-realtime-2.1`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1) |
| `realtime-2.1-mini/` | [`gpt-realtime-2.1-mini`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini) |

Shared builder: `harness.py`. Tracing/reporting: `report.py`.

- Industry tools → industry state API (`TOOL_SERVER_URL`)
- Session tools (`session: true`, e.g. `end_call`) → harness-local + close realtime session
- Handoffs → Realtime handoffs
- Tracing → official `opentelemetry-instrumentation-openai-agents` (RealtimeSession patch + GenAI OTel) → Bluejay OTLP; Chirp stamps `X-Simulation-Result-Id` and POSTs `{trace_ids}`. Realtime API server-side traces also go to the OpenAI dashboard.

Callers must `context["session"] = session` after `runner.run(context=ctx)`. Optional pcm websocket bridge under `adapters/` if an external evaluator needs one.

```bash
uv sync
# set VOICE_AGENT=openai/realtime-2.1 in root .env
uv run python run.py --check
uv run python tests/converse.py
uv run python voice-agent-harnesses/openai/realtime-2.1/agent.py control-industry

# Bluejay via k8s CHIRP (preferred)
uv run python run.py --build --apply --no-logs

# Bluejay CHIRP local (needs OPENAI_API_KEY, tool server, optional CHIRP_USER/CHIRP_PASS)
uv run python industries/control-industry/tool_server.py
CHIRP_PORT=8765 uv run python voice-agent-harnesses/openai/realtime-2.1/adapters/chirp.py
CHIRP_PORT=8766 uv run python voice-agent-harnesses/openai/realtime-2.1-mini/adapters/chirp.py
```
