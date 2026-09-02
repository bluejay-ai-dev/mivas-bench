# OpenAI GPT-Live v3 — `gpt-live-1-diamond-alpha`

Self-contained runtime. Shares nothing with `openai/harness.py` / `report.py`
(those are Realtime-API harnesses; GPT-Live is a different product and wire contract).

| File | Owns |
|---|---|
| `pack.py` | industry pack → live prompt, per-stage backend prompt + Responses tools |
| `live.py` | the v3 session: `session.start`, tool loop, soft handoff, speak-first, graceful close |
| `tools.py` | `POST {TOOL_SERVER_URL}/tools/{name}` with `X-Mivas-Call-Id` |
| `tracing.py` | OTel `voice.call` tree → Bluejay OTLP, then one `update-simulation-result` POST |
| `adapters/chirp.py` | Bluejay CHIRP websocket ↔ `live.py`. All Bluejay specifics live here |
| `agent.py` | `--check`: builds every session shape offline, no network |

## Contract used (alpha guide, v3)

- `wss://api.openai.com/v1/live/sessions`, header `OpenAI-Alpha: quicksilver=v3`
- first message `session.start` with the model in `session.model`; wait for `session.started`
- `audio.format = {audio/pcm, 16000}` — GPT-Live is native 16 kHz, same as CHIRP, so PCM is passed through unmodified in both directions. No resampling, no gating, no mute. The model is full duplex and owns barge-in.
- Measured provider output: a continuous realtime stream (100 ms deltas every ~100 ms, silence frames included), inter-arrival jitter p99 ~130 ms, max ~530 ms, at most one gap over 250 ms mid-speech per call. A 200 ms playout buffer was tried and left Bluejay's `agent_audio_dropouts` unchanged (raw 0/13/9 vs buffered 9/12/12), so audio is forwarded as received and per-call stream gaps are logged instead. Remaining dropouts are the model's own mid-utterance pauses (it speaks, waits for the backend, resumes).
- Responses delegation: `session.delegation.created` → `response.event{response.created}` → `response.event{response.output_item.done}` (completed `function_call`) → after `response.event{response.completed}` every collected call is answered with `response.item.create` then one explicit `response.create`. Deduped by `call_id`.
- speak-first: `session.commentary.append` with `delegation_id: null` after `session.started`
- multi-agent = soft handoff on the one session: `session.update` swaps `delegation.responses.{instructions,tools}` to the target stage, then `session.instructions.append` (≤500 tokens, chunked) tells the live model the role changed. Only the target stage's tools are ever registered.
- `end_call`: answered locally (never POSTed). The backend then finishes its summary turn (next `response.completed` with no function calls), the live model speaks it, and once that audio goes quiet the harness sends `session.close` and reads through `session.closed`. Bounded by `GPT_LIVE_END_CALL_MAX_S`.
- Tool results and handoffs are sent from their own tasks, never from the websocket read loop. Awaiting an ack (`session.updated`, `*.appended`) inside the pump blocks the frame that carries the ack.
- every command carries an `event_id`; acks and `error.client_event_id` are correlated back to it

## Prompt mapping

The live model gets the starting stage's system prompt verbatim plus OpenAI's
prompting-guide delegation template. Its "Backend tools" list is built from the
pack's own tool descriptions with **no tool names** (the FEM/BEM skill keeps
names and schemas out of the live prompt). Each backend stage gets its system
prompt verbatim inside OpenAI's recommended "Voice conversation context / Task
instructions / Return the result" wrapper. The harness adds only the wall clock
(`Today is …`) and the wire rule that the live model delegates instead of calling
tools. No pack policy is authored here.

A pack that wants the recommended FEM/BEM split can ship
`system-prompts/<stage>.live.md`; when present it replaces the verbatim stage
prompt in the live instructions (the backend still gets `<stage>.md`). Use the
kit's `gpt-live-fem-bem-prompt-audit` skill to write it.

A handoff's function result tells the backend the transfer is done and the
caller's request is still pending. Without that, gpt-5.6-terra returned an empty
final answer on ~30% of post-handoff continuations and the live model sat silent
for a minute (calls 863155, 863157, 863184, 863185); with it, 0 of 6.

## Checked against the v3 starter kit

`toolkit/live.py` (reference WebSocket client): same header, `session.start`
first, 20 s heartbeat, ordered base64 chunks (it sends 200 ms chunks; CHIRP's
20 ms frames are forwarded as-is since chunk boundaries are arbitrary), and
`session.close` → wait for `session.closed`. The coding-agent reference splits
context appends at 400 UTF-8 bytes as a conservative bound; this harness splits
at 1400 chars against the documented 500-token cap and every append has been
acknowledged.

## Not done on purpose

- No booking-confirm inference. If the model says "booked" without calling `schedule_appointment`, that is model signal and the trace shows no tool.
- No upstream reconnect. A dropped provider socket ends the call and is logged with the reason.

## Run

```bash
uv run python voice-agent-harnesses/openai/gpt-live-1-diamond-alpha/agent.py control-industry --check
uv run pytest voice-agent-harnesses/openai/gpt-live-1-diamond-alpha/test_gpt_live.py

# local Bluejay CHIRP bridge (tool server on :8000 first)
uv run python industries/control-industry/tool_server.py
CHIRP_PORT=8769 CHIRP_USER=… CHIRP_PASS=… OPENAI_API_KEY=… BLUEJAY_API_KEY=… \
  uv run python voice-agent-harnesses/openai/gpt-live-1-diamond-alpha/adapters/chirp.py --industry control-industry
cloudflared tunnel --url http://127.0.0.1:8769 --no-autoupdate
```

Local CHIRP port: **8769**.

Env: `OPENAI_API_KEY`, `CHIRP_USER`/`CHIRP_PASS`, `CHIRP_PORT`, `TOOL_SERVER_URL`,
`BLUEJAY_API_KEY` (+ optional `BLUEJAY_OTLP_ENDPOINT`, `BLUEJAY_API_URL`, `BLUEJAY_SERVICE_NAME`),
`GPT_LIVE_BACKEND_MODEL` (default `gpt-5.6-terra`), `GPT_LIVE_VOICE` (default `marin`),
`GPT_LIVE_END_CALL_QUIET_S` / `GPT_LIVE_END_CALL_GRACE_S` / `GPT_LIVE_END_CALL_MAX_S`,
`GPT_LIVE_LOG_LEVEL` (`DEBUG` logs every non-audio event both ways).
