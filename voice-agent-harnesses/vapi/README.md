# vapi

Vapi harness via the REST + websocket-transport API — **native multi-agent** (a squad, not a soft handoff). Each blueprint agent is a persisted Vapi assistant; the receptionist hands off to the scheduler with a `handoff` tool whose destination is the scheduler assistant, server-side. `end_call` is Vapi's built-in `endCall` tool. Tools run on Vapi's side too, so `schedule_appointment` comes back to us as an HTTPS POST to `/tool/schedule_appointment`, which forwards to `TOOL_SERVER_URL`.

| Folder | Runtime |
|---|---|
| `flux-gpt4.1-flash2.5/` | `vapi-flux-gpt4.1-flash2.5` |

| Layer | Setting |
|---|---|
| STT | Deepgram Flux — `{provider: "deepgram", model: "flux-general-en", language: "en"}` |
| LLM | OpenAI `gpt-4.1` |
| TTS | ElevenLabs Flash v2.5 — `{provider: "11labs", model: "eleven_flash_v2_5", voiceId: "21m00Tcm4TlvDq8ikWAM"}` |
| Transport | `vapi.websocket`, `pcm_s16le` raw @ 16 kHz both ways (same as CHIRP — no resampling) |

Plain `"flux"` as the transcriber model is accepted by `POST /assistant` but fails at call time with `error-vapifault-deepgram-transcriber-failed`; use `flux-general-en`.

Shared: `harness.py` (`ensure_squad` creates or refreshes the assistant pair + squad, `start_websocket_call` opens a call). Tracing: `report.py` (GenAI-native OTel → Bluejay OTLP, `gen_ai.provider.name=vapi`).

## Env

| Var | Default |
|---|---|
| `VAPI_API_KEY` | required |
| `PUBLIC_URL` | required — https base the provider calls tools on (cloudflared tunnel for this run) |
| `VAPI_VOICE_ID` | `21m00Tcm4TlvDq8ikWAM` (Rachel) |
| `VAPI_LLM_MODEL` / `VAPI_STT_MODEL` / `VAPI_TTS_MODEL` | `gpt-4.1` / `flux-general-en` / `eleven_flash_v2_5` |
| `VAPI_GREETING` | `Welcome to Bluejay's Repair Services!` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | — / — / `8770` |
| `TOOL_SERVER_URL`, `INDUSTRY` | `http://127.0.0.1:8000`, `control-industry` |
| `BLUEJAY_API_KEY`, `BLUEJAY_OTLP_ENDPOINT`, `BLUEJAY_API_URL`, `BLUEJAY_SERVICE_NAME` | traces off without the key; service name `mivas-vapi` |

Assistant/squad IDs are cached in `.agents.json` (gitignored, keyed by industry). The tunnel URL is ephemeral, so `ensure_squad` re-pushes the whole assistant config — prompts, tools, and the current `{PUBLIC_URL}/tool/<name>` webhook — on every chirp boot.

## Run

```bash
uv sync
set -a && source .env && set +a
uv run python industries/control-industry/tool_server.py          # terminal A (shared, :8000)
cloudflared tunnel --url http://127.0.0.1:8770 --no-autoupdate    # terminal B
export PUBLIC_URL=https://<tunnel>.trycloudflare.com
export CHIRP_USER=mivas CHIRP_PASS=mivas CHIRP_PORT=8770 PYTHONUNBUFFERED=1
export BLUEJAY_API_KEY=... BLUEJAY_SERVICE_NAME=mivas-vapi
uv run python voice-agent-harnesses/vapi/flux-gpt4.1-flash2.5/adapters/chirp.py   # terminal C
```

The chirp process serves both halves on one port so a single tunnel covers them: `/` is the CHIRP websocket Bluejay dials (`wss://<tunnel>`, Basic auth, `X-Simulation-Result-Id` on upgrade), `/tool/{name}` is Vapi's tool webhook.

Smoke without Bluejay: `agent.py control-industry --check` pushes the blueprint and prints the ids; without `--check` it opens a real call, feeds silence, and reports the event stream plus agent audio bytes.

## Proof run

control-industry, agent 30375 / sim 30208 / DH 194134 (`speaks_first: false`, expecting `handoff_to_scheduler` + `schedule_appointment`) → result **710641**, https://app.getbluejay.ai/simulations/30208/runs/224727

`COMPLETED`, 56 s, `goal_success=true`, trace `2940ff35a05416945a1f19fb8b2fd01d`. Both DH-expected tools fired and paired against `actual`: `handoff_to_scheduler` @11 374 ms, `schedule_appointment` @31 666 ms `{date: "08/18/2026"}` (tool-server row id 47), `endCall` @52 131 ms. Audio in 1 633 920 B / out 1 673 600 B.

Provider-side ids for that run: squad `1e3742c2-2f88-4cc0-8b1b-f6a14e587fe5`, receptionist `834b8862-a0b8-430e-abcb-750e36c2a473`, scheduler `f0fbc0f4-dc03-4513-9b01-f961c0599b38`.

The digital human must not speak first. With `speaks_first: true` the caller's opener and the assistant's `firstMessage` start within ~30 ms of each other and talk over one another; Vapi's `firstMessageMode: "assistant-waits-for-user"` would fix that (the greeting still comes from the receptionist prompt, just after the caller), but it is deliberately **not** set — the shipped config assumes the caller waits.

## Spans

Outbound agent PCM is paced at realtime 20 ms frames so ElevenLabs Flash bursts do not record as choppy speech. Turn boundaries still cannot use a silence-gap heuristic. `agent.speech` is bracketed by `speech-update` (`role=assistant`, `started`/`stopped`) instead; `customer.speech` comes from Bluejay's inbound `speech.started`/`speech.completed`. `handoff_to_scheduler` and `endCall` never touch the webhook — they are read off `conversation-update` and emitted as zero-width `execute_tool` spans so the whole flow lands on the timeline.

`report.py` waits for a *final* simulation status before its single `update-simulation-result` POST. Posting during `EVALUATING` also works, but eval then wipes `trace_ids` and the relink POST re-extracts the `execute_tool` spans on top of the first POST's, putting every tool on the conversation timeline twice. `_relink_after_final` remains as the safety net for when that wait times out.
