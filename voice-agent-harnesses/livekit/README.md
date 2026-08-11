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
harness.py     blueprint load, Receptionist/Scheduler/Combined agents, run_tool, serve()
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

`_await_terminal_upsert` waits **300 s** here, not the 150 s the CHIRP harnesses
use. Result 710911 proved why: its evaluation took longer than 150 s, the wait
fell through to `_relink_after_final`, the link had by then been wiped, and the
relink POST appended a second copy of every tool (`handoff_to_scheduler` actual=2,
`end_call` actual=2). The relink net still double-counts whenever it actually
fires — the fix is to make the wait long enough that it does not.

## Proof runs (control-industry)

| runtime | agent | sim | run | result | link |
| --- | --- | --- | --- | --- | --- |
| cascaded | 30519 | 30223 | 224783 | 710922 | https://app.getbluejay.ai/simulations/30223/runs/224783 |
| openai-realtime-2.1 | 30520 | 30224 | 224784 | 710923 | https://app.getbluejay.ai/simulations/30224/runs/224784 |
| gemini-flash-live-3.1 | 30521 | 30225 | 224785 | 710924 | https://app.getbluejay.ai/simulations/30225/runs/224785 |

## Runtime notes

* **Job executor is THREAD, not PROCESS.** `spawn` pickles the entrypoint by
  reference and ours is a closure over the runtime's session/agent factories.
* **The trace-linking POST lives in the entrypoint**, not a shutdown callback:
  waiting for a final status can take ~1 min and the entrypoint gets
  `session_end_timeout` (300 s) versus a shutdown callback's
  `shutdown_process_timeout` (10 s).
* **Gemini 3.1 Live is degraded by plugin design.**
  `livekit/plugins/google/realtime/realtime_api.py` sets
  `mutable = "3.1" not in model`, which turns off `mutable_instructions`,
  `mutable_chat_context` and `mutable_tools`. Consequences:
  * no in-framework Agent handoff (a swapped Agent's prompt and tools would never
    reach the session) — this runtime uses `harness.Combined`: one agent, both
    blueprint prompts, `handoff_to_scheduler` still runs and still gets a span;
  * `generate_reply()` is rejected, so the agent cannot open the call from the
    model. An ElevenLabs TTS is attached purely so `session.say()` can deliver the
    scripted greeting; every later turn is native Gemini audio.
