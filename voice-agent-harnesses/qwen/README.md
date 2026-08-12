# qwen

Qwen Omni Realtime Speech-to-Speech harnesses. Each subfolder is one model runtime.

| Folder | Model |
|---|---|
| `omni-realtime/` | [`qwen3.5-omni-flash-realtime`](https://docs.qwencloud.com/developer-guides/speech/realtime-multimodal-speech) (WS function calling) |

Shared builder: `harness.py`. Tracing/reporting: `report.py` (`BLUEJAY_SERVICE_NAME=mivas-qwen`, provider `dashscope`).

**Architecture:** soft handoff on one Omni WebSocket. On `handoff_to_scheduler` the harness returns the function-call output, then `session.update`s scheduler instructions + tools only (history stays). Dual-session is not used — Omni cannot seed prior USER turns via `conversation.item.create`.

- Industry tools → `POST {TOOL_SERVER_URL}/tools/{name}`
- Session tools (`session: true`, e.g. `end_call`) → harness-local + delayed close
- Handoffs → soft `session.update` on the same WS
- Speak-first: bare `response.create` after `session.updated` (pack owns greeting text)
- Audio: Omni PCM 16 kHz in / 24 kHz out ↔ CHIRP 16 kHz `pcm_s16le`
- Barge-in: Omni `input_audio_buffer.speech_started` (never mute on CHIRP VAD alone)
- Clock: `Today is …` injected into every session instructions
- Booking inference: verbal “booking confirmed” → `schedule_appointment` if no FC
- Tracing → Bluejay OTel `voice.call`; Chirp stamps `X-Simulation-Result-Id`

```bash
uv sync
# DASHSCOPE_API_KEY in root .env
uv run python run.py --harness qwen/omni-realtime --mode check
uv run python industries/control-industry/tool_server.py
CHIRP_PORT=8769 CHIRP_USER=mivas CHIRP_PASS=mivas \
  BLUEJAY_SERVICE_NAME=mivas-qwen \
  uv run python run.py --harness qwen/omni-realtime --mode chirp
cloudflared tunnel --url http://127.0.0.1:8769 --no-autoupdate
```

## Env

| var | use |
|---|---|
| `DASHSCOPE_API_KEY` | DashScope API key (`Authorization: Bearer …`) |
| `QWEN_API_KEY` | alias for `DASHSCOPE_API_KEY` |
| `QWEN_OMNI_MODEL` | default `qwen3.5-omni-flash-realtime` |
| `QWEN_OMNI_VOICE` | session voice (default `Tina`) |
| `QWEN_WS_URL` | override WS base (default `wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime`) |
| `TOOL_SERVER_URL` | industry state API |
| `BLUEJAY_API_KEY` | OTel + `update-simulation-result` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | Bluejay websocket auth (default port `8769`) |

## Bluejay

| field | value |
|---|---|
| CHIRP port | `8769` |
| agent_id | `32120` (`control-industry:qwen omni-realtime`) |
| simulation_id | `30308` (`MIVAS control — qwen omni-realtime`) |
| booker DH | `194577` (intent: next Tuesday afternoon) |
| tunnel (live) | `wss://unless-incidence-literally-seen.trycloudflare.com` — re-`update_agent` after every `cloudflared` restart |
| creds | `CHIRP_USER`/`CHIRP_PASS`=`mivas` |
| last smoke | blocked on `DASHSCOPE_API_KEY` (CHIRP + tunnel up; Omni connect fails without key) |

## Pipecat

Not used. Pipecat only has text `QwenLLMService` / cascaded DashScope STT→LLM→TTS — no Omni Realtime S2S service.
