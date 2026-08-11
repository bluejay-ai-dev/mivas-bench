# LiveKit Agents harness

Three LiveKit Agents runtimes over the shared `control-industry` blueprint, reached
by **native Bluejay `LIVEKIT` dispatch** — Bluejay creates the room and dispatches
our `agent_name` into it. There is **no CHIRP bridge, no tool webhook and no
cloudflared tunnel**: LiveKit runs our code, so every tool body executes in this
process and is wrapped directly in an `execute_tool` span.

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

## Run

The worker registers with LiveKit Cloud and waits for Bluejay to dispatch it, so it
can run locally — which is also how it reaches the industry tool server on
`127.0.0.1:8000`.

```bash
cd voice-agent-harnesses/livekit
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env      # fill in; LIVEKIT_* must be the project on the Bluejay org's LiveKit integration

# terminal A — shared industry tool server (do not restart if another harness is using it)
uv run python industries/control-industry/tool_server.py

# terminal B/C/D — one worker per runtime
.venv/bin/python cascaded/agent.py dev
.venv/bin/python openai-realtime-2.1/agent.py dev
.venv/bin/python gemini-flash-live-3.1/agent.py dev
```

Then queue a Bluejay run against an agent whose `connection_type=LIVEKIT` and
`livekit_agent_name` matches (or pass `livekit_agent_name` to
`queue-simulation-run`). **Do not** pass `livekit_metadata` at queue time: a
simulation-run-level metadata dict *replaces* the test-case one, and the test-case
one is where Bluejay injects `X-Simulation-Result-Id`.

## Telemetry

```
voice.call                      root, attr bluejay.simulation_result_id
  ├── agent.speech              AgentSession agent_state_changed → speaking
  ├── customer.speech           AgentSession user_state_changed → speaking
  └── execute_tool <name>       handoff_to_scheduler / schedule_appointment / end_call
```

All three tools are ours, so all three produce real spans — there is no
provider-internal step to leave untimed. Only `trace_ids` is POSTed
(`tool_calls` would double-count against the OTel-extracted tools), and only once,
after the simulation reaches a final status.

The exporter is attached to a **private** `TracerProvider` rather than the global
one. livekit-agents resolves its own tracer off the global provider, and setting it
would ship the framework's entire internal span tree to Bluejay as unrelated traces.

`_await_terminal_upsert` waits **600 s** here, not the 150 s the CHIRP harnesses
use. Result 710911 proved why: its evaluation took longer than 150 s, the wait
fell through to `_relink_after_final`, the link had by then been wiped, and the
relink POST appended a second copy of every tool (`handoff_to_scheduler` actual=2,
`end_call` actual=2). Since we never re-post, a fall-through is now simply a lost
link: result 712617 hit the old 300 s ceiling, POSTed during `EVALUATING`, and
evaluation wiped its `trace_ids`. The clock to beat is our hangup → `COMPLETED`,
which includes the ~2 min the simulation can linger after we hang up.

## Proof runs (control-industry)

All three `COMPLETED` with `goal_success: true`, a linked `trace_ids`, and exactly one
actual per expected tool (`handoff_to_scheduler`, `schedule_appointment`).

| runtime | agent | sim | run | result | link |
| --- | --- | --- | --- | --- | --- |
| cascaded | 30519 | 30223 | 225189 | 712620 | https://app.getbluejay.ai/simulations/30223/runs/225189 |
| openai-realtime-2.1 | 30520 | 30224 | 225198 | 712629 | https://app.getbluejay.ai/simulations/30224/runs/225198 |
| gemini-flash-live-3.1 | 30521 | 30225 | 225188 | 712619 | https://app.getbluejay.ai/simulations/30225/runs/225188 |

## Runtime notes

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
    the scheduler's `system_instruction` and only the scheduler's `tools`
    (`_build_connect_config`). The debug log shows *two* "created new realtime
    session for activity" lines and no "reusing realtime session";
  * `generate_reply()` is still rejected, so the model cannot open a turn on its
    own. An ElevenLabs TTS is attached so `session.say()` can deliver the two
    scripted lines (the call greeting and `harness.SCHEDULER_OPENER`, which is step
    1 of `scheduler.md` verbatim); every other turn is native Gemini audio.
* **OpenAI Realtime reuses the socket, by design.** Its capabilities are all
  mutable, so LiveKit keeps the WebSocket and re-pushes the scheduler's
  instructions and tool list with a `session.update` (`_start_session`, `rt_reused`
  branch). Still a real agent switch — the model is handed a different prompt and a
  different tool set — just without a reconnect.
