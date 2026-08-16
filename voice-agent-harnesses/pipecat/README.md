# Pipecat harness

Pipecat runs our code, so this harness is the agent itself — not a bridge to
someone else's. All three industry tools execute in-process and are wrapped with
`report.tool_span`, so `handoff_to_scheduler`, `schedule_appointment` and
`end_call` all produce real `execute_tool` spans with no untimeable gaps.

Bluejay dials a **static Daily pinless SIP URI** (`connection_type=SIP`). Daily
is the SIP fabric: it webhooks the cluster dispatcher, which forwards to an idle
Pipecat worker on k8s. The bot process runs on that pod over `DailyTransport`.
There is no LiveKit in this path, and Pipecat Cloud is not the worker pool.

```
Bluejay SIP INVITE
  → Daily pinless URI
  → POST https://pipecat-dialin.<MIVAS_BASE_DOMAIN>/dialin/<slug>
  → dispatcher retries POST http://mivas-<slug>:8000/tools/dialin
  → in-pod bot on 127.0.0.1:8080 (409 if already on a call)
  → Daily room + DailyDialinSettings(call_id, call_domain)
```

`slug` is the k8s pair name, e.g. `pipecat-cascaded-healthcare`. One in-flight
call per worker process; extra INVITEs get 409 until a replica is free. Scale
with `MIVAS_REPLICAS`.

## Runtimes

| runtime | stack | k8s slug (healthcare) |
|---|---|---|
| `cascaded` | Deepgram Flux `flux-general-en` → OpenAI `gpt-4.1` → ElevenLabs `eleven_flash_v2_5` | `pipecat-cascaded-healthcare` |
| `openai-realtime-2.1` | OpenAI Realtime `gpt-realtime-2.1` | `pipecat-openai-realtime-2-1-healthcare` |
| `gemini-flash-live-3.1` | Google Gemini Live `gemini-3.1-flash-live-preview` | `pipecat-gemini-flash-live-3-1-healthcare` |

Each runtime is its own worker process (`bot.py` via `agent.py`). The k8s pod
runs that worker plus the industry tool server on `127.0.0.1:8000`.

## Handoff

`handoff_to_scheduler` is a real agent switch, not a prompt injection. Each
blueprint agent gets its own prompt and its own tool set, and the receptionist's
model is never told `schedule_appointment` exists. Which Pipecat mechanism does
the switching depends on the runtime, because Pipecat's own machinery does:

| runtime | mechanism |
|---|---|
| `cascaded` | **Pipecat Flows** (`pipecat.flows`). One `NodeConfig` per blueprint agent, each with its own `task_messages` and its own `functions`. The consolidated handler returns `(result, next_node)` and `FlowManager` swaps the context (`ContextStrategy.RESET`) and the advertised tool set (`LLMSetToolsFrame`). |
| `openai-realtime-2.1`, `gemini-flash-live-3.1` | **`LLMSwitcher`** over one S2S service per blueprint agent. Each service opens its own websocket session with its own `instructions` and its own `tools`; the handoff pushes `ManuallySwitchServiceFrame(service=scheduler_llm)` plus an `LLMRunFrame`. |

Flows is not used for the S2S runtimes because it does not support them —
"Speech-to-speech (realtime) models aren't supported — Gemini Live, OpenAI
Realtime, Ultravox, and AWS Nova Sonic."

Two consequences worth knowing:

- The shared `LLMContext` carries **no tools and no system message**. Context
  tools would override the S2S services' own and hand the receptionist the
  scheduler's tools.
- Both S2S sessions connect at `StartFrame` and stay connected for the call. The
  inactive one receives no audio and its output never leaves its branch.

`gemini-flash-live-3.1` additionally carries an ElevenLabs TTS used *only* to speak
the scripted opener (`harness.GREETING`). Gemini 3.1 Live will not speak until the
caller does; the model still says everything else itself.

## SIP setup (once per Daily domain)

1. Apply the workers **and** the dispatcher (`uv run python run.py --apply` with
   `MIVAS_BASE_DOMAIN` set). Dispatcher host:
   `https://pipecat-dialin.<domain>/dialin/<slug>`.
2. Register pinless URIs (overlays only the slugs you pass):

   ```bash
   export DAILY_API_KEY=...
   export MIVAS_BASE_DOMAIN=benchmarks.getbluejay.ai
   uv run python voice-agent-harnesses/pipecat/pinless_setup.py \
       pipecat-cascaded-healthcare \
       pipecat-openai-realtime-2-1-healthcare \
       pipecat-gemini-flash-live-3-1-healthcare
   ```

3. Point the Bluejay agent at that slug's `sip_uri` (`connection_type=SIP`,
   `mode=VOICE`). Do not set `connection_type=PIPECAT` or `connection_type=LIVEKIT`.
4. Forward `X-Simulation-Result-Id` on the SIP INVITE (`sipHeaders`). The worker
   stamps that onto the OTel root.

`bluejay_setup.py` reads `.daily-pinless.json` (or `DAILY_SIP_URI` for a single
shared URI).

## Env

| var | use |
|---|---|
| `DAILY_API_KEY` | worker creates the Daily room + token; `pinless_setup.py` writes the domain config |
| `DAILY_SIP_URI` / `PIPECAT_SIP_URI` | optional Bluejay `sip_uri` override |
| `MIVAS_BASE_DOMAIN` | dispatcher hostname `pipecat-dialin.<domain>` |
| `OPENAI_API_KEY` / `GOOGLE_API_KEY` / `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY` | model providers |
| `BLUEJAY_API_KEY` | OTLP export + `update-simulation-result` |
| `BLUEJAY_API_URL`, `BLUEJAY_OTLP_ENDPOINT`, `BLUEJAY_SERVICE_NAME` | defaults are the prod Bluejay endpoints / `mivas-pipecat` |
| `TOOL_SERVER_URL` | industry tool server (local `127.0.0.1:8000` is fine) |

## Commands

```bash
# tool server (shared, port 8000)
uv run python industries/control-industry/tool_server.py

# offline checks (each asserts the receptionist is never given schedule_appointment)
uv run python voice-agent-harnesses/pipecat/harness.py
uv run python voice-agent-harnesses/pipecat/cascaded/agent.py --check
uv run python voice-agent-harnesses/pipecat/openai-realtime-2.1/agent.py --check
uv run python voice-agent-harnesses/pipecat/gemini-flash-live-3.1/agent.py --check

# local dialin worker (POST http://127.0.0.1:8080/dialin, or /tools/dialin on :8000)
cd voice-agent-harnesses/pipecat
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python cascaded/agent.py dev
.venv/bin/python openai-realtime-2.1/agent.py dev
.venv/bin/python gemini-flash-live-3.1/agent.py dev
```
