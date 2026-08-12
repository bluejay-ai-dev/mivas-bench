# Twilio ConversationRelay harness (SIP)

Twilio [ConversationRelay](https://www.twilio.com/docs/voice/conversationrelay) with
**OpenAI GPT-4.1**, reached over **Programmable Voice SIP** (not PSTN PHONE).

Deviation from the [Mistral ConversationRelay blog](https://www.twilio.com/en-us/blog/developers/tutorials/product/ai-agent-conversationrelay-voice-mistral):
Bluejay dials a Twilio **SIP Domain** instead of a phone number. Same CR + GPT-4.1
stack on answer.

| Runtime | LLM | STT | TTS | Port |
|---|---|---|---|---|
| `conversationrelay-gpt4.1` | `gpt-4.1` | ConversationRelay (Deepgram default) | ConversationRelay (Google Journey default) | **8773** |

## Shape

```
Bluejay DH (connection_type=SIP)
  ──SIP INVITE──▶ sip:mivas@mivas-conversationrelay.sip.twilio.com
                       │ VoiceUrl webhook (POST /)
                       ▼
            adapters/chirp.py  (TwiML <ConversationRelay>)
                       │ wss://…/ws?simulation_result_id=…
                       ▼
            GPT-4.1 chat.completions (+ tools)
                       │
            industries/*/tool_server.py
```

Bluejay’s SIP path uses digest auth (`sip_username` / `sip_password`) against the
domain’s Credential List. Twilio-to-Twilio SIP is routed via Bluejay’s SIP proxy
when the destination host ends in `twilio.com`.

- **Soft handoff**: one OpenAI conversation; `handoff_to_scheduler` swaps system
  prompt + tool archive to the scheduler while keeping history (plus a mid-call notice).
- **Speak-first**: `welcomeGreeting` on `<ConversationRelay>` (pack-defaultable via env).
- **Clock**: `Today is {Weekday}, {Month} {D}, {YYYY}.` appended to every agent system prompt.
- **Booking inference**: verbal “booking confirmed …” without a function call still
  hits `schedule_appointment` + OTel (deduped via `state["scheduled"]`).
- **OTel**: `voice.call` / `agent.speech` / `customer.speech` / `execute_tool`.
  SIP Domain webhooks forward Bluejay’s `X-Simulation-Result-Id` as
  `SipHeader_X-Simulation-Result-Id`; TwiML appends it to the CR WebSocket URL and
  as a `<Parameter>` so tool actuals / `trace_ids` can link.

## Env

| var | default | note |
|---|---|---|
| `OPENAI_API_KEY` | — | required |
| `PUBLIC_URL` / `HOST` | — | required for live calls; https tunnel base used in TwiML |
| `TWILIO_LLM_MODEL` | `gpt-4.1` | |
| `TWILIO_WELCOME_GREETING` | `Welcome to Bluejay's Repair Services!` | ConversationRelay speak-first |
| `TWILIO_TTS_PROVIDER` | `google` | |
| `TWILIO_TTS_VOICE` | `en-US-Journey-O` | |
| `TWILIO_TRANSCRIPTION_PROVIDER` | `deepgram` | |
| `TWILIO_LANGUAGE` | `en-US` | |
| `CHIRP_PORT` | `8773` | local listen port |
| `TOOL_SERVER_URL` | `http://127.0.0.1:8000` | |
| `INDUSTRY` | `control-industry` | |
| `BLUEJAY_API_KEY` / `BLUEJAY_OTLP_ENDPOINT` / `BLUEJAY_SERVICE_NAME` | — | OTel (`mivas-twilio`) |

Twilio Console / API (not env): SIP Domain VoiceUrl → `https://<tunnel>/` (POST).
ConversationRelay onboarding must be complete on the account.

## Run

```bash
# A — industry tool server
uv run python industries/control-industry/tool_server.py

# B — public tunnel → local ConversationRelay server
cloudflared tunnel --url http://127.0.0.1:8773 --no-autoupdate
# → export PUBLIC_URL=https://….trycloudflare.com

# C — ConversationRelay + GPT-4.1
set -a && source .env && set +a
export PUBLIC_URL=https://<tunnel>.trycloudflare.com
export INDUSTRY=control-industry TOOL_SERVER_URL=http://127.0.0.1:8000
export CHIRP_PORT=8773 BLUEJAY_SERVICE_NAME=mivas-twilio PYTHONUNBUFFERED=1
uv run python voice-agent-harnesses/twilio/conversationrelay-gpt4.1/adapters/chirp.py
```

After every cloudflared restart, update the SIP Domain `VoiceUrl` to the new tunnel
(and keep `PUBLIC_URL` in sync).

Bluejay agent: `connection_type=SIP`, `mode=VOICE`,
`sip_uri=sip:mivas@mivas-conversationrelay.sip.twilio.com`,
plus matching `sip_username` / `sip_password` from the domain Credential List.

## Check

```bash
uv run python run.py --harness twilio/conversationrelay-gpt4.1 --mode check
# or
uv run python voice-agent-harnesses/twilio/conversationrelay-gpt4.1/agent.py control-industry --check
```

## Bluejay / Twilio ids

| | |
|---|---|
| `VOICE_AGENT` | `twilio/conversationrelay-gpt4.1` |
| CR port | `8773` |
| `agent_id` | `32132` |
| `simulation_id` | `30314` |
| booker DH | `194589` (clone of `194508`) |
| SIP Domain | `mivas-conversationrelay.sip.twilio.com` (`SD0fca4b2e1eeddb091a1852e7f7de8070`) |
| Credential List | `mivas-conversationrelay-creds` (`CL4cce80ea0c40d4a12fa9af919dc225d4`) |
| SIP URI | `sip:mivas@mivas-conversationrelay.sip.twilio.com` |
| Username | `mivas` |
| Legacy PSTN number | `+15054776173` — no longer the agent connection (kept on account) |

## Smoke status

| Gate | Status |
|---|---|
| Offline `--check` | pass |
| Local CR protocol smoke (`smoke_ws.py`) | pass — `handoff_to_scheduler` → `schedule_appointment{08/18/2026}` → `end_call` |
| SIP Domain + Bluejay agent wired | pass — agent `32132` is `connection_type=SIP` |
| Bluejay SIP ≥3 (`run 227413` / `716977–716979`) | **pass** — all `COMPLETED`, `goal_success=true`, `08/18/2026`. Prod DB: exactly **1** `trace_id` on `test_results` + `sim_conversations`, **3** `tool_call_logs` (handoff/schedule/end once each), API `actual` counts = 1. CR logged 3× `update-simulation-result ok once` (no double POST). |

Local protocol smoke (no SIP):

```bash
# CR server already on :8773, tool server on :8000
uv run python voice-agent-harnesses/twilio/conversationrelay-gpt4.1/smoke_ws.py
```

## Gotchas

- ConversationRelay access requires Twilio Console onboarding (not instant on new accounts).
- Tunnel URL churn: update SIP Domain VoiceUrl after every cloudflared restart.
- Soft handoff keeps history — do **not** also cold-open the scheduler with a blind re-ask seed.
- Audio barge-in/mute rules from PCM CHIRP harnesses do not apply; Twilio handles interrupts via `interrupt` messages (we truncate the last assistant turn).
- Keep the industry tool server current — older `:8000` processes without `POST /tools/{name}` return FastAPI `{"detail":"Not Found"}` and the model may verbally “confirm” a booking that never landed.
- Do not attach this SIP Domain’s traffic to Elastic SIP Trunking if you need Programmable Voice / ConversationRelay TwiML on answer.
