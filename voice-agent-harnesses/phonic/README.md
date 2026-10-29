# Phonic (speech-to-speech) harness

`phonic/sts@phonic-1-high`: Phonic's STS WebSocket (`wss://api.phonic.ai/v1/sts/ws`), model
`phonic-1`, intelligence level `high` (Phonic's recommended benchmark setting).

## Multi-agent, natively in Phonic

Each industry pack becomes a Phonic project `mivas-<industry>` with one Phonic agent per
blueprint stage (`client_services` becomes `client-services`). Each agent carries its stage
prompt (with the pack clock) and only its stage's tools:

- Handoff tools (`handoff: true`) are `built_in_transfer_to_agent` tools whose target is the
  stage agent. Phonic performs the transfer on the same socket: the target agent's prompt and
  tools take over and the conversation history carries over. On `transfer_to_agent` the
  bridge records the handoff span and the new stage. The target agent does not speak on its
  own (it waits for caller speech or Phonic's 45 s no-input poke), so once the transfer
  turn's speech ends the bridge sends `generate_reply` to start the target's turn, the
  same job `response.create` does after a handoff in the other harnesses.
- Every other tool is a `custom_websocket` tool. Phonic sends `tool_call`, the harness POSTs
  the industry tool server and answers `tool_call_output`. `end_call` and the human-escalation
  session tools hang up after playout.
- The entry agent opens the call with the pack greeting as its welcome message (or a generated
  welcome when the pack has none). Other agents never greet.

Benchmark settings Phonic recommended: `phonic-1`, intelligence `high`, and the built-in
`choose_not_to_respond` tool on every agent with `respond_after_sec: 3`
(`built_in_tool_configs.tool_choose_not_to_respond`), so the agent can hold its reply while
the caller is mid-thought and speaks after 3 s of silence (`assistant_chose_not_to_respond`
on the socket, no `tool_call`). `natural_conversation_ending` is not attached: the pack's
`end_call` is the scored hang-up. Industry tools are `uninterruptible`: Phonic otherwise
cancels a sync tool call when the caller speaks, after the tool server has already written,
and the model retries the write.

`harness.provision()` upserts the project, agents and tools from the pack. The CHIRP bridge
runs it at startup (`--no-provision` skips it), so the Phonic side is rebuilt from the repo.

Phonic's tool schema accepts only `type`, `description`, `enum`, `items`, `minItems`,
`properties`, `required` and `additionalProperties` (`false` is required). Other keywords
(`pattern`, `format`, `minLength`, `maxLength`, `minimum`, `maximum`, `default`,
`examples`) are folded into the field description as "Constraints: ...". The tool server
still enforces them.

## Audio and events

- 16 kHz PCM both ways (`pcm_16000`), so no resampling. Caller audio goes in 20 ms
  `audio_chunk` messages (Phonic allows up to 40 ms) and is never gated.
- Phonic streams real-time audio between turns too (all-zero PCM), so CHIRP `speech.started`
  and `speech.completed` follow `assistant_started_speaking` / `assistant_finished_speaking`,
  and the bridge drops all-zero chunks while the agent is not speaking. Forwarding them made
  one receive stall a permanent ~20 s playback lag, since Bluejay's playout queue never drained.
- `input_text` arrives word by word; the bridge and tracer join a turn's words at
  `user_finished_speaking`.
- The conversation id is on the trace root as `phonic.conversation_id`. The socket reports no
  usage; Phonic bills per conversation minute (see `s2s-model-pricing.json`).

## Run locally

```bash
uv run python industries/healthcare/tool_server.py   # :8000
INDUSTRY=healthcare uv run python voice-agent-harnesses/phonic/sts/adapters/chirp.py --industry healthcare --port 8778
uv run python voice-agent-harnesses/phonic/sts/agent.py --check healthcare   # offline
uv run python voice-agent-harnesses/phonic/harness.py                         # self-check
```

Env: `PHONIC_API_KEY` (required), `PHONIC_MODEL` (default `phonic-1`),
`PHONIC_INTELLIGENCE` (default `high`), `PHONIC_VOICE` (default `grant`), `TOOL_SERVER_URL`,
`CHIRP_PORT` (local default 8778), `CHIRP_USER` / `CHIRP_PASS`.

Plan limits matter: the Starter plan allows 10 agents and 20 concurrent conversations.
The three packs need 19 stage agents, so on Starter run one industry at a time.
