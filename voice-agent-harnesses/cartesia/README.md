# Cartesia Line

Cartesia's [Line](https://github.com/cartesia-ai/line) is code-first: the agent is a Python
program you deploy to Cartesia, not a JSON assistant config. So this harness has two halves:

| half | where it runs | what it is |
|---|---|---|
| `line_agent/` | Cartesia | the Line agent (`VoiceAgentApp`), deployed with the `cartesia` CLI |
| `adapters/chirp.py` | here | CHIRP ↔ Cartesia stream bridge **and** the tool webhook, one uvicorn port |

`line/` is the runtime folder (`line/agent.py` smoke, `line/adapters/chirp.py` shim).

## Model stack

Line's own stack, not a third-party pipeline: Cartesia **Ink** STT and **Sonic** TTS are the
platform's and are not selectable from the SDK. Only the LLM is ours (Line routes through
LiteLLM), pinned to `gpt-4.1` to match the Vapi/Retell harnesses.

| piece | value | override |
|---|---|---|
| STT | Cartesia Ink (platform default) | — |
| TTS | Cartesia Sonic (platform default voice) | `CARTESIA_VOICE_ID` |
| LLM | `gpt-4.1` via LiteLLM | `MIVAS_MODEL` |
| greeting | `Welcome to Bluejay's Repair Services!` | `MIVAS_GREETING` |

**Known: Bluejay scores this harness ~36–40 `agent_audio_clarity` vs ~100 for ElevenLabs/Deepgram.**
Investigated and traced to Sonic's own spectrum, not the bridge — Sonic's agent audio sits ~10 dB
lower than ElevenLabs/Deepgram TTS in every band above 1 kHz, and the same tilt is present in the
raw stream before the bridge touches it. Ruled out: dropped/truncated frames, decode errors,
sample-rate mismatch, and voice choice (pinning `Skylar` scored 36.2 vs 40.3 for the default).
`output_format` is accepted by the stream API but ignored — output is always pcm_16000.

Multi-agent is native: the blueprint's `handoff_to_scheduler` becomes a Line
`agent_as_handoff` tool, so the scheduler takes over inside Cartesia with no bridge-side
routing. `end_call` is Line's built-in.

## Tools

`schedule_appointment` is a Line `http_server_tool` pointed at `{PUBLIC_URL}/tool/schedule_appointment`,
i.e. back at `adapters/chirp.py`. Tools execute provider-side, so this round trip is what puts the
appointment in the industry tool server *and* produces the `execute_tool` span. `TOOL_BASE_URL` is
pushed to the deployed agent with `cartesia env set` and read per call (`get_agent` runs per call),
so a new cloudflared tunnel needs no redeploy. Handoff and `end_call` never leave Cartesia — those
spans are reconstructed from `turn_ended.tool_calls`.

## Transport

`wss://api.cartesia.ai/agents/stream/{agent_id}?cartesia_version=2026-03-01`, header
`Authorization: Bearer $CARTESIA_API_KEY`. All JSON text frames, base64 audio inside:

- client → `{"event":"start","config":{"input_format":"pcm_16000"}}`, then
  `{"event":"media_input","media":{"payload":"<b64>"}}`
- server → `ack`, `media_output`, `turn_started`, `turn_output_text_delta`, `turn_ended`
  (with `text` and `tool_calls`), `clear`

Both directions are pcm_16000 (`media_output` arrives paced at ~33 kB/s ≈ 16 kHz × 2 B), so the
bridge does no resampling. Turn structure is on the wire, so `agent.speech` spans are bracketed by
`turn_started`/`turn_ended` rather than a silence-gap heuristic. `websockets` keeps the socket
alive with its default 20 s ping (the server idles out at 180 s).

## Setup

```bash
curl -fsSL https://cartesia.sh | sh          # installs ~/.cartesia/bin/cartesia
cartesia auth login "$CARTESIA_API_KEY"
```

Env: `CARTESIA_API_KEY`, `OPENAI_API_KEY`, `PUBLIC_URL`, plus the usual
`BLUEJAY_*` / `CHIRP_*` / `INDUSTRY` / `TOOL_SERVER_URL`.
Optional: `CARTESIA_VOICE_ID`, `CARTESIA_AGENT_ID` (skip the `.agents.json` cache),
`CARTESIA_REDEPLOY=1` (force a deploy), `CARTESIA_VERSION`, `CARTESIA_CLI`,
`CARTESIA_DEPLOY_TIMEOUT`.

## Run

```bash
# terminal A — industry state (shared; skip if :8000 already answers /health)
uv run python industries/control-industry/tool_server.py

# terminal B — smoke the deployed agent (deploys on first run, ~2 min)
set -a && source .env && set +a
uv run python voice-agent-harnesses/cartesia/line/agent.py control-industry

# terminal C — tunnel, then the bridge (websocket + tool webhook on one port)
cloudflared tunnel --url http://127.0.0.1:8773 --no-autoupdate
export PUBLIC_URL=https://<tunnel>.trycloudflare.com
export CHIRP_USER=mivas CHIRP_PASS=mivas CHIRP_PORT=8773
export INDUSTRY=control-industry TOOL_SERVER_URL=http://127.0.0.1:8000
export BLUEJAY_API_KEY=… BLUEJAY_SERVICE_NAME=mivas-cartesia
uv run python voice-agent-harnesses/cartesia/line/adapters/chirp.py
```

Bluejay dials `wss://<tunnel>.trycloudflare.com` with basic auth `mivas:mivas`.

`ensure_agent()` does the provider-side work on every boot: bakes the industry prompts into
`line_agent/blueprint.json`, creates the agent on first run (`cartesia init --new`), pushes
changed env, deploys, and blocks until `cartesia status <agent_id>` reports `Ready` — a call
against a still-building version fails.

Deploy by hand:

```bash
cartesia deploy --agent-id <agent_id> voice-agent-harnesses/cartesia/line_agent
cartesia status <agent_id>
```

## Proof run

control-industry, Bluejay agent 30383 / simulation 30210 / DH 194137 (agent speaks first,
`expected_tool_calls` set), Cartesia agent `agent_5xoxqQDosbz2PbpWvX21gx`:

**simulation_result 710644** — https://app.getbluejay.ai/simulations/30210/runs/224730

`COMPLETED`, 74 s, `goal_success=true`, trace `f28a675fd40855a843c83168b4eb5078` linked on the
first POST at `terminal=COMPLETED` (no relink). Audio `in=2087680 out=752640` bytes.
Both DH-expected tools paired with exactly one actual each — `handoff_to_scheduler` @28915 ms,
`schedule_appointment` @52085 ms (`08/18/2026`, tool-server row id 48).

Greeting → handoff → date confirmation → booking → sign-off, no talk-over:

```
[ 4880] Agent:  Welcome to Blue Jays Repair Services.
[ 9779] caller: Hi, I'd like to schedule a repair appointment for next Tuesday afternoon…
[17135] Agent:  Hey, when do you want to schedule your repair appointment?
[28990] Agent:  Just to confirm, could you let me know the exact date for next Tuesday?
[38742] caller: Sure, that would be August eighteenth, twenty twenty six, in the afternoon.
[46845] Agent:  Your repair appointment is scheduled for 08/18/2026. …
```

Agent latency runs 1535–2330 ms avg across seven runs of this sim; treat a single run's figure as
noise, not signal.
