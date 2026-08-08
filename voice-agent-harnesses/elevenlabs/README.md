# elevenlabs

ElevenLabs Conversational AI (ElevenAgents) harness via the REST + websocket API — **native multi-agent**, not soft handoff. Each blueprint agent is a persisted ElevenLabs agent; the receptionist hands off to the scheduler with the built-in `transfer_to_agent` system tool, server-side. `end_call` is likewise the `end_call` system tool. The harness only ever executes client tools (`schedule_appointment`), which go through the state API (`TOOL_SERVER_URL`).

| Folder | Model |
|---|---|
| `convai/` | `elevenlabs-convai` |

Shared: `harness.py` (`ensure_agents` creates or reuses the receptionist/scheduler pair). Tracing: `report.py` (GenAI-native OTel → Bluejay OTLP; `gen_ai.provider.name=elevenlabs`). Run a runtime with `agent.py` (text turns via `user_message` on stdin). Optional pcm websocket bridge under `adapters/` if an external evaluator needs one.

## Agent creation

`ensure_agents(industry_dir)` creates two agents on first run (via `POST /v1/convai/agents/create`) and caches their IDs in `.agents.json` (gitignored, keyed by industry name) so re-runs don't spam creates:

- **scheduler** — prompt from the industry's scheduler system prompt, no first message (it's only ever reached via transfer), `schedule_appointment` client tool + `end_call` system tool.
- **receptionist** (entry point) — prompt from the industry's receptionist system prompt, short greeting first message, `transfer_to_agent` system tool (pointing at the scheduler) + `end_call` system tool.

Both use `pcm_16000` ASR/TTS audio (matches Chirp's 16 kHz — no resampling either direction). Override with `ELEVENLABS_RECEPTIONIST_AGENT_ID`/`ELEVENLABS_SCHEDULER_AGENT_ID` to skip creation entirely and point at existing agents.

```bash
uv sync
export ELEVENLABS_API_KEY=...
# optional: ELEVENLABS_VOICE_ID (default 21m00Tcm4TlvDq8ikWAM / Rachel), ELEVENLABS_GREETING
# optional Bluejay traces: BLUEJAY_API_KEY (+ BLUEJAY_SERVICE_NAME=mivas-elevenlabs)
uv run python industries/control-industry/tool_server.py
uv run python voice-agent-harnesses/elevenlabs/convai/agent.py control-industry --check   # ensure_agents + print IDs
uv run python voice-agent-harnesses/elevenlabs/convai/agent.py control-industry
# optional: adapters/chirp.py (16 kHz both ways, no resample; links X-Simulation-Result-Id)
```
