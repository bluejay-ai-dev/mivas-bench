# Bland

MIVAS harness for [Bland](https://bland.ai). One runtime, `base`, on CHIRP port **8772**.

Bland is fully in-house — their own STT, LLM and TTS — so unlike the Retell/Vapi harnesses
there are no third-party components to pin. The config below is Bland's best-available
setup rather than a match to the other providers'.

| knob | value | why |
|---|---|---|
| `model` | `base` | supports every pathway feature; `turbo` is faster but feature-limited |
| `voice` | `Jordan` (BTTS_V3) | Bland's newest in-house TTS tier; their own copy calls it warm/friendly at a moderate pace, for customer support |
| `interruption_threshold` | `150` | Bland's default is 500 and their docs recommend 50–200; at 500 the agent waits so long after the digital human stops that turns collide |
| `max_duration` | 5 (minutes) | benchmark calls run ~50 s; this is a backstop |
| multi-agent | Conversational Pathway | Bland's native node graph — see below |

Override any of them with `BLAND_MODEL`, `BLAND_VOICE`, `BLAND_INTERRUPTION_THRESHOLD`,
`BLAND_MAX_DURATION_MIN`.

## Multi-agent = pathway, not prompt

`harness.pathway_graph` compiles `agent_blueprint.json` into a Bland pathway:

```
receptionist (Default, isStart)
  │  edge: "caller wants to schedule a repair appointment"
  ▼
handoff_to_scheduler (Webhook → PUBLIC_URL/tool/handoff_to_scheduler)
  │  responsePathways: Default/Webhook Completion
  ▼
scheduler (Default)
  │  edge: "a concrete calendar date has been agreed"
  ▼
schedule_appointment (Webhook → PUBLIC_URL/tool/schedule_appointment, extractVars date)
  │  responsePathways: Default/Webhook Completion
  ▼
End Call  (confirms {{booked_date}}, says goodbye, hangs up)
```

Both blueprint tools are Webhook nodes, so both are real provider-side HTTP calls into
this harness and both get timed `execute_tool` spans. The handoff is a node rather than a
bare edge for exactly that reason: it puts the receptionist → scheduler transition on the
Bluejay timeline.

Three things about the pathway API are not in Bland's docs and cost a run each:

- **Edge routing fields must be top-level on the edge** (`{id, source, target, label,
  description}`). Anything nested under `edge.data` — which is the shape the docs example
  shows, and the shape a `GET` returns — is silently dropped, and an edge with no
  `description` never fires. The symptom is a pathway that greets correctly and then never
  leaves the start node.
- **`description` is the routing criterion; `label` is only the editor's display name.**
- **Webhook nodes do not leave via edges.** They route through
  `responsePathways: [["Default/Webhook Completion", "", "", {id, name}]]`.

Iterate on routing with Bland's text-chat endpoint (`POST /v1/pathway/chat/create`, then
`POST /v1/pathway/chat/{chat_id}` with `{message}`) — it returns `current_node_id`, so a
routing bug is one HTTP call away instead of one voice call away.

The industry prompts tell the agent to *call* `handoff_to_scheduler` /
`schedule_appointment`, but a Default node has no tools bound — the tools are the Webhook
nodes and the routing is the edge descriptions. The only way the model can obey a "call
this tool" instruction is to put the call in the one channel it does have, its dialogue,
and Bland's TTS then reads that out: bare `handoff_to_scheduler`, or
`<tool_call>schedule_appointment</tool_call>` mid-sentence after "I'll go ahead and book
that for you now" (heard in run 713481, transcribed back by Bluejay's STT as
"ToolappointmentToolAccall"). So `harness._strip_tool_instructions` drops every prompt
sentence that names a blueprint tool, along with any heading left empty, before
`PATHWAY_NOTE` is appended; the note now only covers the surrounding narration. Nothing is
lost — the sentences it removes are duplicated by the edge descriptions, which are what
actually route. Measured over the text-chat endpoint: 2 leaks in 3 scripted bookings
before, 0 in 8 after. `base/agent.py --check` asserts no node prompt contains a tool name.

## Transport

`wss://stream-v2.aws.dc8.bland.ai/ws/connect/blandshared?agent=…&token=…`, token from
`POST /v1/agents/{id}/authorize` (single use, one per call). Undocumented; probing
established:

- Raw binary PCM16 mono **both** directions, no JSON envelope, at **44100 Hz** — 16 kHz in
  is transcribed as noise, so `adapters/chirp.py` resamples both ways with `audioop.ratecv`.
- Interleaved JSON status frames: `{"event":"update","payload":{"type":"assistant"|"human",
  "text":…}}`, `{"event":"mark"}`, and a bare-string `{"type":"callID","payload":"…"}`.
- Agent audio is **continuous** — Bland streams silence between turns — so `agent.speech`
  is bracketed by an RMS gate (`BLAND_AGENT_RMS_ON`, `BLAND_AGENT_SILENCE_S`), not by a
  silence-gap-on-frames heuristic.

## Files

```
harness.py            blueprint → pathway graph, agent bootstrap, run_tool
report.py             OTel → Bluejay OTLP (voice.call / agent.speech / customer.speech / execute_tool)
adapters/chirp.py     FastAPI: "/" CHIRP websocket + "/tool/{name}" pathway webhook
base/agent.py         --check (offline graph assertions) / live silence smoke
base/adapters/chirp.py  shim → adapters.chirp.main(model="base")
```

`.agents.json` caches the pathway + agent IDs per industry (gitignored). The graph is
re-pushed on every boot because the webhook nodes carry the run's ephemeral tunnel URL.

## Env

`BLAND_API_KEY`, `PUBLIC_URL` (the https cloudflared URL — required, Bland calls tools back
over it), plus the shared `TOOL_SERVER_URL`, `INDUSTRY`, `CHIRP_USER`/`CHIRP_PASS`,
`CHIRP_PORT`, `BLUEJAY_API_KEY`, `BLUEJAY_API_URL`, `BLUEJAY_OTLP_ENDPOINT`,
`BLUEJAY_SERVICE_NAME=mivas-bland`.

## Run

```bash
uv run python industries/control-industry/tool_server.py            # terminal A (shared, :8000)
cloudflared tunnel --url http://127.0.0.1:8772 --no-autoupdate      # terminal B

set -a && source .env && set +a
export BLUEJAY_API_URL=https://api.getbluejay.ai/v1
export BLUEJAY_OTLP_ENDPOINT=https://otlp.getbluejay.ai/v1/traces
export BLUEJAY_SERVICE_NAME=mivas-bland
export CHIRP_USER=mivas CHIRP_PASS=mivas
export INDUSTRY=control-industry TOOL_SERVER_URL=http://127.0.0.1:8000
export CHIRP_PORT=8772 PYTHONUNBUFFERED=1
export PUBLIC_URL=https://<tunnel>.trycloudflare.com
uv run python voice-agent-harnesses/bland/base/adapters/chirp.py    # terminal C
```

Bluejay dials `wss://<tunnel>.trycloudflare.com` with Basic `mivas:mivas`.

Smoke without Bluejay: `uv run python voice-agent-harnesses/bland/base/agent.py --check`
(offline) or drop `--check` for a live silence call that prints the agent's turns.

## Proof run

control-industry, agent **30387** / sim **30211** / DH **194138** →
result **713651** at
[simulations/30211/runs/225457](https://app.getbluejay.ai/simulations/30211/runs/225457) — `COMPLETED`,
`goal_success: true`, both tools once each and both matched against the DH's
`expected_tool_calls` (`handoff_to_scheduler` @16.1 s, `schedule_appointment`
`{date: "08/18/2026"}` @38.2 s), no tool syntax anywhere in the transcript,
0 interruptions either way, `pronunciation: 5`, `agent_audio_clarity: 100`.

The start node greets, which is what the industry prompt requires, so this assumes a
digital human that does **not** speak first. With `speaks_first` the two openers collide
(measured 2.4 s overlap in run 224638 — recoverable, `goal_success` still true). Bland can
wait if that ever becomes the default: a `Wait for Response` start node ahead of the
receptionist keeps the agent silent (verified — 14 s of silence on a probe call vs a
greeting at 1.4 s without it). Not wired in, since it trades that collision for dead air
in the normal case. `wait_for_greeting` is a `/v1/calls` field only; web agents drop it.
