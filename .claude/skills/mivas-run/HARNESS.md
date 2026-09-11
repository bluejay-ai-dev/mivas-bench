# Harnesses

A harness turns the industry pack (`agent_blueprint.json`, `tools.json`, prompts) into a
live multi-agent voice runtime for one provider, and bridges Bluejay's CHIRP audio to it.
You need **full control** of the harness you run: its Dockerfile, its adapter, its keys.

## Existing harnesses

| `family/runtime` | Model | Env keys | Local CHIRP port |
|---|---|---|---|
| `openai/realtime-2.1`, `openai/realtime-2.1-mini` | gpt-realtime-2.1 (mini) | `OPENAI_API_KEY` | 8765 / 8766 |
| `gemini/flash-live-3.1`, `gemini/2.5-flash-native-audio` | Gemini Live (LiveKit SIP worker) | `GOOGLE_API_KEY`, `LIVEKIT_URL/API_KEY/API_SECRET`, `LIVEKIT_SIP_HOST` | – (SIP) |
| `aws/nova-sonic-2` | Amazon Nova 2 Sonic | `AWS_ACCESS_KEY_ID`/`SECRET` (+`AWS_SESSION_TOKEN`), `NOVA_SONIC_REGION` | 8774 |
| `grok/voice` | xAI Grok voice | `GROK_API_KEY` (or `XAI_API_KEY`) | 8768 |
| `qwen/audio-realtime` | Qwen Audio Realtime | `DASHSCOPE_API_KEY`, `QWEN_WORKSPACE_ID`, `QWEN_REGION` | 8769 |
| `nvidia/nemotron`, `nvidia/nemotron-voicechat` | cascaded Nemotron / VoiceChat NIM | `NVIDIA_API_KEY` (or NIM `NEMOTRON_*` / `VOICECHAT_*` URLs) | 8766 |
| `livekit/cascaded` | Deepgram Flux → GPT-4.1 → ElevenLabs (LiveKit SIP worker) | `LIVEKIT_*`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` | – (SIP) |

Each family README documents its wire contract and gotchas. Every pod also needs
`BLUEJAY_API_KEY` (trace upload + result relink) and `CHIRP_USER`/`CHIRP_PASS`.

Offline gate for any pair: `uv run python run.py --harness $H --industry $I --check`
(builds the agents from the blueprint, no call). Talk to it locally:
`uv run python tests/converse.py --harness $H` (mic; OpenAI only) or `--text`.

## Build your own

Layout (copy the closest family; `openai/` for a soft-handoff realtime API, `grok/`
for a raw WebSocket provider, `nvidia/nemotron-voicechat` for hard dual-session):

```
voice-agent-harnesses/<family>/
  README.md            # wire contract, env keys, local port; never Bluejay ids or hosts
  requirements.txt     # pip deps for the image
  harness.py           # blueprint → provider sessions; industry tools → POST /tools/{name}
  report.py            # OTel voice.call tree → Bluejay OTLP; POST update-simulation-result {trace_ids}
  <runtime>/
    agent.py           # `python agent.py <industry> --check` must build all agents offline
    Dockerfile         # copy openai/realtime-2.1/Dockerfile; change family/runtime names
    adapters/chirp.py  # Bluejay CHIRP bridge (16 kHz pcm_s16le in/out, speech.* JSON events)
```

### Contract (what the verifiers assume)

1. **Blueprint first.** Load `agent_blueprint.json` + `tools.json` + `system-prompts/`. The first
   agent starts. Each provider session advertises only that agent's tools.
2. **Tools are a dumb pipe.** Every non-handoff, non-session tool → `POST {TOOL_SERVER_URL}/tools/{name}`
   with `{"arguments": {...}}` and header `X-Mivas-Call-Id: <simulation result id>`
   (`runtime/call_id.py`: `set_call_id`, `headers()`); return the JSON verbatim. No per-tool code.
3. **Handoffs are provider-native** (`handoff: true`): swap instructions/tools on the live
   session when the provider can (`session.update`), or open the next session and seed it
   with the prior user turns. Never hit the tool server.
4. **`end_call` is harness-native** (`session: true`): let the farewell audio drain, then close.
5. **CHIRP.** On upgrade read `Authorization: Basic` and `X-Simulation-Result-Id`. Binary
   frames are caller PCM16 @16 kHz; JSON `speech.started`/`speech.completed` are Bluejay's
   VAD. Always forward inbound audio to the provider; never mute the agent on Bluejay VAD alone
   (it fires on agent echo). Resample with kept state. Speak first: DHs are `speaks_first: false`.
6. **Clock.** Inject the pack's date line (`runtime/pack_clock.py`) into every agent's
   instructions; realtime models otherwise book in the wrong year.
7. **Tracing.** One `voice.call` root span per conversation with turn / tool spans and
   `gen_ai.usage.*`; export to `BLUEJAY_OTLP_ENDPOINT` with `X-API-Key`; POST
   `update-simulation-result {simulation_result_id, trace_ids}` (retry 429/5xx). Bluejay's
   `actual` tool calls come from these spans: no spans, no tool credit.
8. **Hangup snapshot.** Call `snapshot.capture_final(call_id)` (`runtime/snapshot.py`) when the
   socket closes; it freezes `GET /state` and uploads to the snapshot store.
9. **Health.** Pass `process_request=chirp_health.process_request` to `websockets.serve` so
   `GET /health` on the CHIRP port answers 200 (Baseten probes it).

### Register it in the repo

- `tests/test_dockerfiles.py` asserts the Dockerfile count: bump it.
- New provider env var: add to `k8s/deployment.yaml` (secretKeyRef, `optional: true`),
  `run.py` `_SECRET_ENV_KEYS` (+ `_redact_cmd`), `k8s/secret.example.yaml`, `.env.example`.
- Heavy runtimes (local VAD, cascaded STT/TTS): `run.py` `pair_resources()`.
- SIP worker instead of CHIRP: add the family to `LIVEKIT_WORKER_FAMILIES` in `run.py`
  and the `entrypoint.sh` worker branch.
- `tests/test_harness_tool_headers.py` greps every tool POST for `X-Mivas-Call-Id`.

### Local loop

```bash
uv run python run.py --harness <family>/<runtime> --industry control-industry --check
uv run python run.py --harness <family>/<runtime> --industry control-industry      # tools :8000 + CHIRP
cloudflared tunnel --url http://127.0.0.1:8765 --no-autoupdate
uv run python .claude/skills/mivas-run/scripts/bluejay.py smoke --harness <family>/<runtime> --industry control-industry --url wss://<tunnel>
```

Done only when three consecutive control-industry smoke calls pass the rubric **and** a
read transcript shows: full greeting, scheduler continues with the date the caller already
gave, `schedule_appointment` actual with a plausible `MM/DD/YYYY`, clean `end_call`.

### Fix loop (symptom → layer)

| Symptom | Layer | Look at |
|---|---|---|
| "Hey," then silence; dropouts | audio bridge | muting on Bluejay `speech.started`; echo window |
| agent re-asks what caller said | inbound path / handoff seed | forward-always; USER ASR log |
| booking said, no `schedule_appointment` actual | tool wiring / spans | tool POST + `execute_tool` span |
| wrong year | clock | pack date injection |
| `trace_ids` empty | report | `BLUEJAY_API_KEY` in pod; update-simulation-result 5xx retry |
| first agent audio > 3 s | accept path | create provider sessions at boot, not on accept |
| 401 on wss probe | creds | Bluejay agent username/password vs `CHIRP_USER`/`CHIRP_PASS` |
