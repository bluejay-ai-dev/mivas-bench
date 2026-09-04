# openai

OpenAI Realtime harnesses. Each subfolder is one model runtime.

| Folder | Model |
|---|---|
| `realtime-2.1/` | [`gpt-realtime-2.1`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1) |
| `realtime-2.1-mini/` | [`gpt-realtime-2.1-mini`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini) |

Shared builder: `harness.py`. Tracing/reporting: `report.py`.

- Industry tools → industry state API (`TOOL_SERVER_URL`)
- Session tools (`session: true`, e.g. `end_call`) → harness-local + close realtime session
- Clock: pack `TODAY` (not the wall clock) injected as `Today is …` on every agent
- Handoffs → Realtime handoffs
- Tracing → Realtime event proxy (`report.py`) parses session events into a Bluejay OTel `voice.call` tree (user/agent turns, transcripts, tools, handoffs) **plus a `chat <model>` generation span per response** carrying the full `gen_ai.usage.*` token breakdown (input/output, audio, text, cached, reasoning) and time-to-first-token (`mivas.ttft_ms` / `gen_ai.server.time_to_first_token`) — the same telemetry LangSmith/Langfuse pull, exported to Bluejay's own OTLP (no external backend). Chirp stamps `X-Simulation-Result-Id` and POSTs `{trace_ids}`. Optional Realtime API server-side traces still go to the OpenAI dashboard.

Callers must `context["session"] = session` after `runner.run(context=ctx)`. Optional pcm websocket bridge under `adapters/` if an external evaluator needs one.

```bash
uv sync
# set HARNESS=openai/realtime-2.1 in root .env
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
