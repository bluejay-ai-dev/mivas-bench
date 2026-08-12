# nvidia

Two NVIDIA runtimes over industry `agent_blueprint.json` packs, same MIVAS interface
(CHIRP + tool server + OTel) as the other harnesses.

| Folder | Stack | Multi-agent |
|---|---|---|
| `nemotron/` | Cascaded: Nemotron ASR → `nemotron-3-nano-30b-a3b` → Magpie TTS ([voice-agent blueprint](https://github.com/NVIDIA-AI-Blueprints/nemotron-voice-agent)) | Pipecat Flows (hard handoff) |
| `nemotron-voicechat/` | Full-duplex S2S: [`nvidia/nemotron-voicechat`](https://build.nvidia.com/nvidia/nemotron-voicechat) / [HF 11B](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-VoiceChat-11B) | Dual-session switch (one WS per blueprint agent; idle gets no audio) |

Shared: `harness.py` (blueprint + industry tools), `report.py` (OTel → Bluejay).

## nemotron (cascaded)

Pipecat + cloud NIM. See `bot.py` / `adapters/chirp.py`.

```bash
uv pip install -r voice-agent-harnesses/nvidia/requirements.txt
export NVIDIA_API_KEY=nvapi-...
uv run python run.py --harness nvidia/nemotron --mode check
uv run python run.py --harness nvidia/nemotron --mode chirp
```

## nemotron-voicechat (full duplex)

OpenAI Realtime–compatible WebSocket. Default is the hosted NVCF endpoint
(`wss://grpc.nvcf.nvidia.com/v1/realtime`, function `ai-nemotron-voicechat`) —
needs `NVIDIA_API_KEY` only (no local GPU).

**Multi-agent:** opens one VoiceChat session per blueprint agent at call start. Each
session gets that agent’s pack instructions + tools only (industry-agnostic; no
harness prompt hacks). Handoff rewires CHIRP audio to the target session; idle
sessions receive no input. Uses N NVCF sessions for an N-agent pack.

**Speak-first:** VoiceChat is full-duplex — it only speaks while input audio flows.
Zero-PCM alone yields near-silent frames. With `VOICECHAT_SPEAKS_FIRST=true` (default),
**call open** sends a short speech-shaped kick (`assets/speak_first_kick.wav`) + trail
silence. **Handoff** must not kick — a cold specialist session would open-greet mid-call
("Hello, how can I help?"). Instead the bridge primes the target with a transfer notice
and nudges via `response.create` (same idea as Grok).

`agent.speech` / `customer.speech` are exclusive siblings under `voice.call`. Agent
spans open on transcript or sustained RMS (not kick glitches); customer spans coalesce
CHIRP VAD blips (`VOICECHAT_CUSTOMER_HANG_S`). Audio-transcript deltas are preferred
over text deltas so spans don't double words.

Idle CHIRP PCM is **not** forwarded to VoiceChat (it would barge-in on noise). Agent
audio is muted the moment the DH is live so both sides hear a real gap.

**Tools:** each session advertises that agent’s catalog tools. Hosted NVCF does not
run the local NIM jinja, so the harness also appends NVIDIA’s trained
`<AVAILABLE_TOOLS>` / `<TOOLCALL>` protocol from those same decls (not pack policy).
Tool spans come from `response.function_call_arguments.done`, parsed `<TOOLCALL>`,
unique `ack_messages` (handoff: “One moment.”), or booking-confirm + date inference
for `schedule_appointment` when the model speaks the confirmation without a FC event.

```bash
export NVIDIA_API_KEY=nvapi-...
# optional overrides:
# export VOICECHAT_WS_URL=wss://grpc.nvcf.nvidia.com/v1/realtime
# export VOICECHAT_FUNCTION_ID=42c86b5f-545a-4b2f-a83b-90fd71da9912
# export VOICECHAT_SPEAKS_FIRST=true   # agent opens (needed when DH speaks_first=false)

uv run python industries/control-industry/tool_server.py
uv run python run.py --harness nvidia/nemotron-voicechat --mode check
CHIRP_PORT=8765 uv run python run.py --harness nvidia/nemotron-voicechat --mode chirp
```

Local NIM (GPU ≥ 80 GB) instead:

```bash
docker run -it --rm --name=nemotron-labs-voicechat \
  --runtime=nvidia --gpus '"device=0"' --shm-size=8GB \
  -e NIM_HTTP_API_PORT=9000 -p 9000:9000 \
  -v $(pwd)/nemotron-labs-voicechat_v1.0.0:/data/models \
  --entrypoint /s2s/run_s2s_server.sh \
  nvcr.io/nim/nvidia/nemotron-labs-voicechat:latest
export VOICECHAT_WS_URL=ws://127.0.0.1:9000/v1/realtime
```

Wire format is 24 kHz PCM both ways (server resamples internally); CHIRP stays 16 kHz.

## Env

| var | use |
|---|---|
| `NVIDIA_API_KEY` | cascaded NIM cloud + hosted VoiceChat NVCF |
| `VOICECHAT_WS_URL` | VoiceChat Realtime WS (default `wss://grpc.nvcf.nvidia.com/v1/realtime`) |
| `VOICECHAT_FUNCTION_ID` | NVCF function id (default `ai-nemotron-voicechat`) |
| `VOICECHAT_SPEAKS_FIRST` | `true`/`false` (default `true`) — speech-kick active agent at start/handoff |
| `VOICECHAT_SPEAK_FIRST_KICK_WAV` | optional override path for the speak-first kick (mono pcm16) |
| `VOICECHAT_AGENT_RMS_ON` | RMS gate for `agent.speech` (default `400`) |
| `VOICECHAT_USER_RMS_ON` | RMS gate for inbound CHIRP → VoiceChat (default `350`) |
| `VOICECHAT_CUSTOMER_HANG_S` | Coalesce CHIRP VAD blips into one `customer.speech` (default `1.25`) |
| `VOICECHAT_KICK_TRAIL_SILENCE_S` | Max trail silence after speak-first kick (default `3.5`) |
| `TOOL_SERVER_URL` | industry state API |
| `BLUEJAY_API_KEY` | OTel + `update-simulation-result` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | Bluejay websocket auth |
