# LiveKit Agents harness

Three LiveKit Agents runtimes over the shared `control-industry` blueprint, reached
by **Bluejay `connection_type=SIP`**. Bluejay dials this project's LiveKit SIP
host; an inbound trunk plus dispatch rule create the room and dispatch our
`agent_name`. Audio is the stock LiveKit SIP mix — no CHIRP bridge, no RoomIO
patching, no tool webhook.

| runtime | stack | LiveKit `agent_name` |
| --- | --- | --- |
| `cascaded/` | Deepgram Flux `flux-general-en` STT + OpenAI `gpt-4.1` + ElevenLabs `eleven_flash_v2_5` | `mivas-livekit-cascaded` |
| `openai-realtime-2.1/` | OpenAI Realtime `gpt-realtime-2.1` (S2S) | `mivas-livekit-openai-realtime` |
| `gemini-flash-live-3.1/` | Google `gemini-3.1-flash-live-preview` (S2S) | `mivas-livekit-gemini-live` |

`cascaded` is STT/LLM/TTS-matched to the Vapi and Cartesia cascaded harnesses, so
the framework is the only variable.

## Layout

```
harness.py     blueprint load, Receptionist/Scheduler agents, run_tool, serve()
report.py      OTel → Bluejay OTLP + the single post-final update-simulation-result
<runtime>/agent.py   plugin wiring for one stack; everything else comes from harness.py
```

## SIP setup (once per LiveKit project)

On the **same** LiveKit Cloud project the worker registers with:

1. Inbound trunk whose `numbers` is a routing key (any E.164, e.g. `+15551230000`).
2. Dispatch rule on that trunk: `roomPrefix: sip-`, `roomConfig.agents[0].agentName`
   matching the worker (`mivas-livekit-cascaded` locally, `mivas-{slug}` on k8s).
3. Forward `X-Simulation-Result-Id` on the trunk (`headers_to_attributes` or
   `include_headers: SIP_X_HEADERS`). The worker also reads it via
   `lk.sip.GetRemoteHeaders`.

Bluejay agent: `connection_type=SIP`, `mode=VOICE`,

```
sip:+15551230000@<project-sip-id>.sip.livekit.cloud
```

The number must match the trunk. The SIP host id is not the wss project name —
set `LIVEKIT_SIP_HOST` / `LIVEKIT_SIP_NUMBER` in `.env`.

## Run

The worker registers with LiveKit Cloud and waits for a SIP-dispatched room, so it
can run locally — which is also how it reaches the industry tool server on
`127.0.0.1:8000`.

```bash
cd voice-agent-harnesses/livekit
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env      # LIVEKIT_* + LIVEKIT_SIP_HOST / LIVEKIT_SIP_NUMBER

# terminal A — shared industry tool server (do not restart if another harness is using it)
uv run python industries/control-industry/tool_server.py

# terminal B/C/D — one worker per runtime
.venv/bin/python cascaded/agent.py dev
.venv/bin/python openai-realtime-2.1/agent.py dev
.venv/bin/python gemini-flash-live-3.1/agent.py dev
```

Then queue a Bluejay run against the SIP agent. Do not set `connection_type=LIVEKIT`.

## Telemetry

```
voice.call                      root, attr bluejay.simulation_result_id
  ├── agent.speech              AgentSession agent_state_changed → speaking
  ├── customer.speech           AgentSession user_state_changed → speaking
  └── execute_tool <name>       handoff_to_scheduler / schedule_appointment / end_call
```

All three tools are ours, so all three produce real spans. Only `trace_ids` is
POSTed (`tool_calls` would double-count against the OTel-extracted tools), and
only once, after the simulation reaches a final status.

The exporter is attached to a **private** `TracerProvider` rather than the global
one. livekit-agents resolves its own tracer off the global provider, and setting it
would ship the framework's entire internal span tree to Bluejay as unrelated traces.

`_await_terminal_upsert` waits **600 s**. The clock to beat is hangup →
`COMPLETED`, which includes the time the simulation can linger after we hang up.

## Runtime notes

* **Hanging up waits for silence, not a fixed delay.** `end_call` only marks the
  *intent* to hang up; the goodbye is still playing. `await_farewell` polls
  `AgentSession.agent_state` and deletes the room after `HANGUP_QUIET_S` (4 s) of
  neither `speaking` nor `thinking`, capped at `HANGUP_MAX_WAIT_S` (20 s).
* **Job executor is THREAD, not PROCESS.** `spawn` pickles the entrypoint by
  reference and ours is a closure over the runtime's session/agent factories.
* **The trace-linking POST lives in the entrypoint**, not a shutdown callback:
  waiting for a final status can take ~1 min and the entrypoint gets
  `session_end_timeout` (300 s) versus a shutdown callback's
  `shutdown_process_timeout` (10 s).
* **All three runtimes do a real two-agent handoff.** `handoff_to_scheduler` is a
  `@function_tool` on `Receptionist` that returns a `Scheduler` *instance*, so
  LiveKit swaps the active `Agent`. The two agents never share a prompt or a tool
  set: the receptionist is physically unable to call `schedule_appointment` and the
  scheduler is unable to call `handoff_to_scheduler` (asserted by
  `test_harness.test_real_handoff`).
* **Gemini 3.1 Live gets one model instance per agent.**
  `livekit/plugins/google/realtime/realtime_api.py` sets
  `mutable = "3.1" not in model`, turning off `mutable_instructions` and
  `mutable_chat_context` (`mutable_tools` is off for every Gemini Live model).
  Those flags only forbid *mutating a live session*, not running two of them:
  * `AgentActivity._detach_reusable_resources` carries the realtime session across
    a handoff only when `self.llm is new_activity.llm`. This runtime gives the
    `Receptionist` and the `Scheduler` their own `RealtimeModel`, so the handoff
    falls through to `llm.session()` and a second Gemini Live socket is opened with
    the scheduler's `system_instruction` and only the scheduler's `tools`;
  * `generate_reply()` is still rejected, so the model cannot open a turn on its
    own. An ElevenLabs TTS is attached so `session.say()` can deliver the two
    scripted lines: the call greeting, and (on handoff) a first line derived from
    the target agent's own prompt via `harness._derive_opener`; every other turn is
    native Gemini audio.
* **OpenAI Realtime reuses the socket, by design.** Its capabilities are all
  mutable, so LiveKit keeps the WebSocket and re-pushes the scheduler's
  instructions and tool list with a `session.update`. Still a real agent switch —
  the model is handed a different prompt and a different tool set — just without a
  reconnect.
