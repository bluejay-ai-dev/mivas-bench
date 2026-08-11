# Retell harness

Retell AI over CHIRP. Multi-agent is native (`retell-llm` `states` + `edges`), tools are
platform-side webhooks, and the call itself runs on LiveKit.

## Runtime

| Runtime | LLM | TTS | STT |
|---|---|---|---|
| `flux-gpt4.1-flash2.5` | `gpt-4.1` (retell-llm) | ElevenLabs `eleven_flash_v2_5` | Deepgram via `custom_stt_config`, `endpointing_ms: 100` |

### ⚠️ Deepgram Flux is not available on Retell

The runtime folder is named `flux-gpt4.1-flash2.5` to line up with the Vapi harness, but
**Retell does not run Flux**. `create-agent` exposes no STT model field at all:
`custom_stt_config` accepts only `{provider, endpointing_ms}` with `provider` in
`azure | deepgram | soniox | assemblyai`. So this harness sends
`stt_mode: "custom"`, `custom_stt_config: {provider: "deepgram", endpointing_ms: 100}` and
Retell picks the Deepgram model. Any Retell-vs-Vapi comparison is *not* STT-matched.

## Shape

```
Bluejay DH ──CHIRP ws (16k pcm + speech.*)──▶ adapters/chirp.py ──LiveKit room──▶ Retell
                                                    ▲                               │
                        POST {PUBLIC_URL}/tool/schedule_appointment ────────────────┘
                                                    │
                                        industries/*/tool_server.py
```

- **Agents**: one `retell-llm` with a `receptionist` state and a `scheduler` state. The
  blueprint's `handoff_to_scheduler` becomes an `edge`; Retell surfaces it to the model as
  `transition_to_scheduler` (the prompt is rewritten to say so), so the harness never routes
  the handoff. `end_call` is Retell's built-in `{type: "end_call"}` tool.
- **Tools**: Retell tools execute platform-side, so `schedule_appointment` is a
  `{type: "custom"}` tool whose `url` points back at this process. The `execute_tool` span is
  emitted in the webhook handler. The tunnel URL is ephemeral, so `ensure_agent` re-PATCHes the
  llm (and therefore the tool URLs) on every boot; `.agents.json` keeps the ids stable.
- **Transport**: `POST /v2/create-web-call` returns a LiveKit JWT for room `web_call_<call_id>`.
  We publish a mic track fed by CHIRP pcm and subscribe to the agent track. LiveKit resamples,
  so there is no `audioop` here. **The deprecated `wss://api.retellai.com/audio-websocket/…` +
  `register-call` path is not used.**
- **Agent turns**: LiveKit audio frames are continuous, so `agent.speech` is bracketed by
  Retell's `agent_start_talking` / `agent_stop_talking` data messages. Retell fires a spurious
  start/stop pair ~250 ms before each real turn; stops under `BLIP_S` (150 ms) are ignored.
- **Tool spans**: `schedule_appointment` gets a live span in the webhook. The edge transition and
  `end_call` execute inside Retell and reach no client channel, so after hangup the bridge reads
  `GET /v2/get-call/{call_id}` and backfills them from `transcript_with_tool_calls`. `time_sec`
  there is relative to the record's `start_timestamp` (epoch ms), so the backfilled spans get
  explicit epoch-ns start/end and land in the same time base as the live ones. Invocations tagged
  `type: "custom"` are skipped — those are the webhook tools that already have a span.
  The transition is reported under the blueprint name **`handoff_to_scheduler`** for cross-provider
  comparability, with Retell's own name kept in `mivas.provider.tool_name=transition_to_scheduler`.

## Env

| var | default | note |
|---|---|---|
| `RETELL_API_KEY` | — | required |
| `PUBLIC_URL` | — | required; cloudflared https url, becomes the tool webhook base |
| `RETELL_LIVEKIT_URL` | `wss://retell-ai-4ihahnq7.livekit.cloud` | hardcoded in `retell-client-js-sdk@2.0.8` |
| `RETELL_LLM_MODEL` | `gpt-4.1` | |
| `RETELL_VOICE_ID` | `11labs-Kate` | |
| `RETELL_STT_PROVIDER` | `deepgram` | azure / deepgram / soniox / assemblyai |
| `RETELL_ENDPOINTING_MS` | `100` | |
| `RETELL_GREETING` | "Welcome to Bluejay's Repair Services!…" | retell-llm `begin_message` |
| `RETELL_START_SPEAKER` | `agent` | `user` makes Retell wait for the caller — **and drops `begin_message`** |
| `RETELL_AGENT_ID` / `RETELL_LLM_ID` | — | override `.agents.json` |
| `CHIRP_PORT` | `8771` | serves both the ws and the tool webhook |

## Run

```bash
# terminal A — industry state API
uv run python industries/control-industry/tool_server.py

# terminal B — tunnel (one tunnel covers ws + tool webhook)
cloudflared tunnel --url http://127.0.0.1:8771 --no-autoupdate

# terminal C — push the blueprint to Retell and check the ids
set -a && source .env && set +a
export PUBLIC_URL=https://<tunnel>.trycloudflare.com
uv run python voice-agent-harnesses/retell/flux-gpt4.1-flash2.5/agent.py control-industry

# terminal C — the bridge
export BLUEJAY_API_KEY=… BLUEJAY_API_URL=https://api.getbluejay.ai/v1
export BLUEJAY_OTLP_ENDPOINT=https://otlp.getbluejay.ai/v1/traces
export BLUEJAY_SERVICE_NAME=mivas-retell
export CHIRP_USER=mivas CHIRP_PASS=mivas
export INDUSTRY=control-industry TOOL_SERVER_URL=http://127.0.0.1:8000
export CHIRP_PORT=8771 PYTHONUNBUFFERED=1
uv run python voice-agent-harnesses/retell/flux-gpt4.1-flash2.5/adapters/chirp.py
```

Point a Bluejay `WEBSOCKET` agent at `wss://<tunnel>.trycloudflare.com` with
`websocket_username=mivas` / `websocket_password=mivas`.

Extra dep vs the other harnesses: **`livekit`** (the Python rtc SDK).

## Who speaks first

`start_speaker: "agent"` + `begin_message` is the default: an inbound receptionist answers the
phone, which is what the industry prompt's mandated greeting assumes. Setting
`RETELL_START_SPEAKER=user` is accepted by the API but makes Retell **drop `begin_message`
entirely** (verified: the created llm comes back with `begin_message: null`), so the greeting
would then depend on the model obeying the prompt rather than being scripted.

Observed with a digital human that also speaks first (`speaks_first: true`, `ai_generated`):
no collision. Bluejay's caller waits for the greeting to finish (agent 0.21–3.42 s, caller's
first word 3.02 s). What it does cost is a **redundant double greeting** — the caller opens with
a content-free "Hello.", so the agent greets again before the caller states intent, burning ~7 s.
That happens identically without `speaks_first`, so it is a digital-human artifact, not a
`start_speaker` problem, and flipping `start_speaker` would not fix it.

Recommendation: leave it on `agent`. Changing it would make Retell the only caller-first provider
and break comparability. If the bench wants caller-first, flip it across all providers together.

## Check

```bash
python voice-agent-harnesses/retell/test_platform_tools.py
```
Asserts the call-record → span mapping against a real Retell payload: webhook tools excluded,
edge tool renamed to the blueprint name, `time_sec` → epoch ns.

## Proof run (canonical)

control-industry, sim 30209 / agent 30376 / DH 194135 (`speaks_first: false`):
**https://app.getbluejay.ai/simulations/30209/runs/224729** — `simulation_result_id` 710643.

`COMPLETED`, `trace_ids` non-empty, `goal_success: true`, all three tools on the timeline with
`expected` paired against `actual` on the two the DH declares:

| tool | source | start_offset_ms |
|---|---|---|
| `handoff_to_scheduler` | backfilled from call record | 14954 |
| `schedule_appointment` | live webhook span | 28557 |
| `end_call` | backfilled from call record | 38014 |

Backfill alignment is exact: Retell's call starts 602 ms after `voice.call`, and
`transition_to_scheduler` (`time_sec` 13.711) lands at 14313 ms in the trace — 13711 + 602 to the
millisecond. Same for `end_call` (36.771 s → 37373 ms). The live webhook span trails Retell's own
invocation time by ~112 ms, which is the HTTP hop.

### Known issue — the agent has no current date

In this run the scheduler booked **06/11/2024**, over two years in the past. That is a Tuesday, so
the model computed "next Tuesday" correctly but anchored to its training prior: nothing in the
context supplies today's date. Retell injects none, and this harness's `general_prompt` does not
either.

Every earlier passing run hid this, because the caller spoke the date aloud ("that would be August
eighteenth, twenty twenty six") and the agent only had to echo it. Turning `speaks_first` off
removed that crutch and exposed it.

The fix is one line in `_llm_payload` — append `f"Today is {date.today():%A, %B %-d, %Y}."` to
`general_prompt`. It is deliberately **not** applied here: it changes what the benchmark measures,
so it should land across all providers at once or not at all. See the harness report.
