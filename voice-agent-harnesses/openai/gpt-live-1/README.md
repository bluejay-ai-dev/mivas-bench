# OpenAI GPT-Live — `gpt-live-1`

Self-contained runtime for OpenAI's GA GPT-Live model. Shares nothing with
`openai/harness.py` / `report.py` (those are Realtime-API harnesses; GPT-Live is a
different product and wire contract).

| File | Owns |
|---|---|
| `pack.py` | industry pack → live prompt, per-stage backend prompt + Responses tools |
| `live.py` | the session: `session.start`, tool loop, soft handoff, speak-first, graceful close |
| `tools.py` | `POST {TOOL_SERVER_URL}/tools/{name}` with `X-Mivas-Call-Id` |
| `tracing.py` | OTel `voice.call` tree → Bluejay OTLP, then one `update-simulation-result` POST |
| `adapters/chirp.py` | Bluejay CHIRP websocket ↔ `live.py`. All Bluejay specifics live here |
| `agent.py` | `--check`: builds every session shape offline, no network |

## Contract used

Sources: `guides/voice-websockets?api=live`, `guides/live-delegation`,
`guides/live-conversations`, `guides/live-prompting`, `models/gpt-live-1`.

- `wss://api.openai.com/v1/live/sessions`, `Authorization: Bearer $OPENAI_API_KEY`
  and nothing else — GA needs no preview/alpha header.
- first message `session.start` with the model in `session.model`; wait for
  `session.started` before sending audio.
- `audio.format` is one shared format for both directions and cannot change
  mid-session. The documented default is `{audio/pcm, 24000}`; this harness picks
  the supported `{audio/pcm, 16000}` because CHIRP is 16 kHz PCM16, so audio is
  passed through byte-for-byte with no resampling, no gating, no mute. The model
  is full duplex and owns barge-in.
- Provider output is a continuous realtime stream (100 ms deltas every ~100 ms,
  silence frames included). A 200 ms playout buffer was measured against Bluejay's
  `agent_audio_dropouts` during the preview and did not move it, so audio is
  forwarded as received and per-call stream gaps are logged instead
  (`call done … gaps>250ms=…`). Remaining dropouts are the model's own
  mid-utterance pauses while it waits on the backend.
- Responses delegation: `session.delegation.created` → `response.event{response.created}`
  → `response.event{response.output_item.done}` (completed `function_call`) → after
  `response.event{response.completed}` every collected call is answered with
  `response.item.create` then one explicit `response.create`. Deduped by `call_id`.
  `response.completed.response.output` is empty — never use it to decide pending calls.
- speak-first: one `session.instructions.append` with `delegation_id: null` after
  `session.started`. Instructions, not commentary: commentary is paraphrased and the
  pack's greeting is fixed text.
- multi-agent = soft handoff on the one session: `session.update` swaps
  `delegation.responses.{instructions,tools}` to the target stage, then
  `session.instructions.append` (≤500 tokens, chunked) tells the live model the role
  changed. Only the target stage's tools are ever registered. History is kept by
  the service; nothing is replayed.
- `end_call`: answered locally (never POSTed). The backend then finishes its summary
  turn (next `response.completed` with no function calls), the live model speaks it,
  and once that audio goes quiet the harness sends `session.close` and reads through
  `session.closed` (which carries `usage.seconds` and a `reason`). Bounded by
  `GPT_LIVE_END_CALL_MAX_S`.
- Tool results and handoffs are sent from their own tasks, never from the websocket
  read loop. Awaiting an ack (`session.updated`, `*.appended`) inside the pump blocks
  the frame that carries the ack.
- every command carries an `event_id`; acks and `error.client_event_id` are
  correlated back to it.

## Prompt mapping

The live model gets the starting stage's system prompt verbatim plus OpenAI's
prompting-guide delegation template. Its "Backend tools" list is built from the
pack's own tool descriptions with **no tool names** — the prompting guide keeps
tool names and schemas out of the live prompt. Each backend stage gets its system
prompt verbatim inside the guide's "Voice conversation context / Task instructions /
Return the result" wrapper. The harness adds only the pack clock (`Today is …`) and
the wire rule that the live model delegates instead of calling tools. No pack policy
is authored here.

A pack that wants the recommended front-end/back-end prompt split can ship
`system-prompts/<stage>.live.md`; when present it replaces the verbatim stage prompt
in the live instructions (the backend still gets `<stage>.md`).

A handoff's function result tells the backend the transfer is done and the caller's
request is still pending. Without that the backend returns an empty final answer on a
sizeable share of post-handoff continuations and the live model has nothing to say.

## Not done on purpose

- No booking-confirm inference. If the model says "booked" without calling
  `schedule_appointment`, that is model signal and the trace shows no tool.
- No upstream reconnect. A dropped provider socket ends the call and is logged with
  the reason.
- No greeting nudge, echo-mute window, or playout buffer. Anything added here has to
  move a Bluejay metric first.

## Run

```bash
uv run python voice-agent-harnesses/openai/gpt-live-1/agent.py control-industry --check
uv run pytest voice-agent-harnesses/openai/gpt-live-1/test_gpt_live.py

# local Bluejay CHIRP bridge (tool server on :8000 first)
uv run python industries/control-industry/tool_server.py
CHIRP_PORT=8769 CHIRP_USER=… CHIRP_PASS=… OPENAI_API_KEY=… BLUEJAY_API_KEY=… \
  uv run python voice-agent-harnesses/openai/gpt-live-1/adapters/chirp.py --industry control-industry
cloudflared tunnel --url http://127.0.0.1:8769 --no-autoupdate
```

Local CHIRP port: **8769**.

Env: `OPENAI_API_KEY`, `CHIRP_USER`/`CHIRP_PASS`, `CHIRP_PORT`, `TOOL_SERVER_URL`,
`BLUEJAY_API_KEY` (+ optional `BLUEJAY_OTLP_ENDPOINT`, `BLUEJAY_API_URL`, `BLUEJAY_SERVICE_NAME`),
`GPT_LIVE_BACKEND_MODEL` (default `gpt-5.6-terra`), `GPT_LIVE_VOICE` (default `gleam`),
`GPT_LIVE_SAMPLE_RATE` (default `16000`),
`GPT_LIVE_END_CALL_QUIET_S` / `GPT_LIVE_END_CALL_GRACE_S` / `GPT_LIVE_END_CALL_MAX_S`,
`GPT_LIVE_LOG_LEVEL` (`DEBUG` logs every non-audio event both ways).

## Pricing

$0.05 per minute of voice session, billed per second (`models/gpt-live-1`). The
backend Responses model is billed separately on its own tokens; the tracer records
those as `gen_ai.usage.*` on each `chat <backend model>` span.
