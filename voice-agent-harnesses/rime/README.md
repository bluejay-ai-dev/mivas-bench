# rime

Rime Prism harness. One model runtime.

| Folder | Model |
|---|---|
| `prism/` | [Prism](https://docs.rime.ai/docs/prism/overview), Rime's speech-to-speech model (`wss://prism.api.rime.ai/v1/realtime`) |

Frontend-only: no ACP coding-agent backend. Shared builder: `harness.py`. Tracing: `report.py` (`BLUEJAY_SERVICE_NAME=mivas-rime`, provider `rime`). Realtime pacing: `pcm.py`.

- Industry tools → `POST {TOOL_SERVER_URL}/tools/{name}` with `X-Mivas-Call-Id`
- Session tools (`session: true`): `end_call` is harness-local; `transfer_to_human` / `escalate_to_human` POST so the pack records the row. Either way the agent gets one tool continuation for its farewell, then the socket closes (cap `MIVAS_FAREWELL_MAX_S`, default 20 s)
- Handoffs → `{"success": true, "role": <agent>}` recorded as an `execute_tool` span, then a mid-call `session.update` swapping instructions + tools on the same socket (history stays), then the tool continuation. Handoff tools carry `prism_filler: none` so the hop is silent
- Tool round, exactly as Prism documents it: `response.function_call_arguments.done` → run → `conversation.item.create` (`function_call_output`) → wait for the parent `response.done` **and** one `conversation.item.created` per result → `response.create` with `prism_cause: tool_continuation` and `prism_parent_response_id`. Several calls in one reply all return before the continuation, so no second write is dropped
- Speak-first: `response.create` with `prism_cause: proactive` and a `prism_instruction` carrying the pack `greeting`, sent after `prism.typed_input.ready`, re-sent until audio arrives
- Audio: Prism PCM16 16 kHz in / 24 kHz out ↔ CHIRP 16 kHz `pcm_s16le`; replies arrive faster than realtime and are paced at 20 ms frames so the Bluejay recording is neither compressed nor choppy
- Barge-in: Prism owns turn detection (`interrupt_response: true`); on `input_audio_buffer.speech_started` the pacer drops queued audio and the harness reports `conversation.item.truncate` with the milliseconds actually sent; `prism.playback.drained` is sent when a reply has fully played
- Clock: pack `TODAY` (not the wall clock) injected as the shared `CALENDAR:` block on every session
- Tools: flat `{type: function, name, description, parameters, prism_filler}`; `RIME_TOOL_FILLER=none` (default) keeps every tool silent, `auto` lets Prism say its own "One moment." for industry tools
- Tracing → `realtime_session` → `turn` → {user_message, model (gen_ai.usage.* tokens + TTFT + reply text), execute_tool}; `bluejay.simulation_result_id` on the root; `mivas.audio.duration_s` on the root for per-minute pricing

```bash
uv sync
# RIME_API_KEY in the shell (never in a file)
uv run python run.py --harness rime/prism --industry healthcare --check
uv run python industries/healthcare/tool_server.py
CHIRP_PORT=8770 CHIRP_USER=mivas CHIRP_PASS=mivas BLUEJAY_SERVICE_NAME=mivas-rime \
  uv run python run.py --harness rime/prism --industry healthcare --mode chirp
```

## Env

| var | use |
|---|---|
| `RIME_API_KEY` | Rime API key (`Authorization: Bearer …` on the upgrade) |
| `RIME_WS_URL` | override the realtime endpoint (default `wss://prism.api.rime.ai/v1/realtime`) |
| `RIME_VOICE` | `astra` (default), `aurora`, `brando`, `wawona` |
| `RIME_EAGERNESS` | `low` (default: digital humans pause mid-sentence and Prism cut in at `auto`) / `medium` / `high` / `auto`; `default` sends nothing; locked after the first update |
| `RIME_END_OF_TURN_TIMEOUT_MS` | 1000 to 10000; Prism default 5000; locked after the first update |
| `RIME_TOOL_FILLER` | `none` (default) or `auto` |
| `TOOL_SERVER_URL` | industry state API |
| `BLUEJAY_API_KEY` | OTel + `update-simulation-result` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | Bluejay websocket auth (default port `8770` local, `8765` in-pod) |

## Prism limits that shape a call

- A caller turn ends after 55 s of continuous speech; a reply is at most 120 s of speech or 512 tokens.
- Tool results are accepted for 90 s after the parent `response.done`; the continuation for 60 s after that.
- Context 65,536 tokens, oldest turns dropped (`truncation: auto`); the session closes after 5 minutes idle (code 1000 `idle`).
- English only. Four Prism voices; the Rime TTS catalog does not apply.

## Pricing

`s2s-model-pricing.json` `per_minute_pricing.prism = 0.08` (Flex list price, https://rime.ai/pricing). `scripts/eval_costs.py` bills `mivas.audio.duration_s` from the root span; the token counts on `response.done` are informational.
