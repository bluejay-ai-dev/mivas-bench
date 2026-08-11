# Pipecat harness

Pipecat runs our code, so this harness is the agent itself — not a bridge to
someone else's. All three industry tools execute in-process and are wrapped with
`report.tool_span`, so `handoff_to_scheduler`, `schedule_appointment` and
`end_call` all produce real `execute_tool` spans with no untimeable gaps.

## Runtimes

| runtime | stack |
|---|---|
| `cascaded` | Deepgram Flux `flux-general-en` → OpenAI `gpt-4.1` → ElevenLabs `eleven_flash_v2_5` |
| `openai-realtime-2.1` | OpenAI Realtime `gpt-realtime-2.1` |
| `gemini-flash-live-3.1` | Google Gemini Live `gemini-3.1-flash-live-preview` |

One deployed Pipecat Cloud agent serves all three; the runtime is chosen per call
from the start `body`, i.e. from the Bluejay agent's `pipecat_agent_configuration`.
The model is a runtime setting, not a deployment property, so three copies of the
same image would buy nothing.

## Handoff

`handoff_to_scheduler` is a real agent switch, not a prompt injection. Each
blueprint agent gets its own prompt and its own tool set, and the receptionist's
model is never told `schedule_appointment` exists. Which Pipecat mechanism does
the switching depends on the runtime, because Pipecat's own machinery does:

| runtime | mechanism |
|---|---|
| `cascaded` | **Pipecat Flows** (`pipecat.flows`, now in core; the standalone `pipecat-ai-flows` / `pipecat_flows` is deprecated). One `NodeConfig` per blueprint agent, each with its own `task_messages` and its own `functions`. The consolidated handler returns `(result, next_node)` — `transition_to` / `transition_callback` were removed in Flows 1.0 — and `FlowManager` swaps the context (`ContextStrategy.RESET`) and the advertised tool set (`LLMSetToolsFrame`). |
| `openai-realtime-2.1`, `gemini-flash-live-3.1` | **`LLMSwitcher`** over one S2S service per blueprint agent. Each service opens its own websocket session with its own `instructions` and its own `tools`; the handoff pushes `ManuallySwitchServiceFrame(service=scheduler_llm)` plus an `LLMRunFrame`, and `ServiceSwitcher`'s per-branch filters wire the call to the other session. |

Flows is not used for the S2S runtimes because it does not support them —
"Speech-to-speech (realtime) models aren't supported — Gemini Live, OpenAI
Realtime, Ultravox, and AWS Nova Sonic" — precisely because it transitions by
mutating one live session's context and tools. Two sessions make that
unnecessary: nothing is mutated, the call is simply rewired.

Two consequences worth knowing:

- The shared `LLMContext` carries **no tools and no system message**. Context
  tools would override the S2S services' own (`OpenAIRealtimeLLMService._send_session_update`:
  "tools given in the context override the tools in the session properties")
  and hand the receptionist the scheduler's tools. With none set, Pipecat falls
  back to each service's own tools, including for handler registration
  (`LLMService._sync_registered_tool_handlers`).
- Both S2S sessions connect at `StartFrame` (the switcher's filters pass
  lifecycle frames) and stay connected for the call. The inactive one receives
  no audio and its output never leaves its branch.

`gemini-flash-live-3.1` additionally carries an ElevenLabs TTS used *only* to speak
the scripted opener (`harness.GREETING`, verbatim from the receptionist prompt).
Gemini 3.1 Live will not speak until the caller does, which otherwise burns ~63 s of
a 180 s call on dead air; the LiveKit harness works around the same plugin limit with
`session.say(GREETING)`. The model still says everything else itself. The TTS is
gated by a `FunctionFilter` on both sides — see the comments in `bot.py` — and the
`LLMRunFrame` is still queued alongside the opener, without which the Gemini service
answers nothing and the socket closes with `1008`.

## How Bluejay reaches it

`connection_type=PIPECAT` is **not** a CHIRP bridge and not a self-hosted worker.
Bluejay's LiveKit worker calls Pipecat Cloud:

```
Bluejay DH ──LiveKit room── bridge ──Daily room── Pipecat Cloud agent (this code)
                              │
                              └── POST https://api.pipecat.daily.co/v1/public/<pipecat_agent_name>/start
                                  Authorization: Bearer <org integrations.pipecat_api_key>
                                  body = agent.pipecat_agent_configuration
```

Two consequences that differ from the other harnesses:

- **The bot runs in Pipecat Cloud, so `TOOL_SERVER_URL` must be publicly
  reachable.** There is no tool webhook, but the industry tool server still needs
  a tunnel. The URL is passed per-agent in `pipecat_agent_configuration`, so a new
  cloudflared URL only needs an `update-agent`, not a redeploy.
- **Bluejay passes no per-run metadata.** LiveKit dispatch injects
  `X-Simulation-Result-Id` into the job metadata
  (`livekit_agent/src/agent_bootstrap/hydration.py`); the Pipecat path
  (`src/agent_bootstrap/connection_handlers.py:handle_pipecat_simulation`) forwards
  only the *static* `agent.pipecat_agent_configuration` and ignores the run's own
  `pipecat_agent_configuration`. So the config carries `simulation_id` and
  `report.resolve_simulation_result_id` looks the live result up over the Bluejay
  API. That is exact at `max_concurrent: 1`; concurrency needs Bluejay to pass the
  id through.

The Pipecat Cloud key stored in the org integrations must be a **public** key
(`pk_...`). `/v1/public/<agent>/start` rejects a private key with
`PCC-1002 "Attempt to start agent without public api key"`.

## Agent config

```jsonc
// Bluejay agent: connection_type = PIPECAT, pipecat_agent_name = "mivas-control"
{
  "runtime": "cascaded",                                  // or the other two
  "tool_server_url": "https://<tunnel>.trycloudflare.com",
  "simulation_id": 30227
}
```

## Env

| var | use |
|---|---|
| `OPENAI_API_KEY` / `GOOGLE_API_KEY` / `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY` | model providers |
| `BLUEJAY_API_KEY` | OTLP export + `update-simulation-result` + result-id lookup |
| `BLUEJAY_API_URL`, `BLUEJAY_OTLP_ENDPOINT`, `BLUEJAY_SERVICE_NAME` | defaults are the prod Bluejay endpoints / `mivas-pipecat` |
| `TOOL_SERVER_URL` | industry tool server; overridden by `body.tool_server_url` |
| `PIPECAT_PRIVATE_API_KEY` | deploy only (`sk_...`) |

## Commands

```bash
# tool server (shared, port 8000) + a tunnel the deployed bot can reach
uv run python industries/control-industry/tool_server.py
cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate

# offline checks (each asserts the receptionist is never given schedule_appointment)
uv run python voice-agent-harnesses/pipecat/harness.py                       # tools + handoff
uv run python voice-agent-harnesses/pipecat/cascaded/agent.py                # services + pipeline
uv run python voice-agent-harnesses/pipecat/openai-realtime-2.1/agent.py
uv run python voice-agent-harnesses/pipecat/gemini-flash-live-3.1/agent.py

# deploy (REST; no Docker daemon and no interactive CLI login needed)
set -a && source .env && set +a
export PIPECAT_PRIVATE_API_KEY=sk_... BLUEJAY_API_KEY=...
uv run python voice-agent-harnesses/pipecat/deploy.py
```
