# nvidia

Two NVIDIA runtimes over industry `agent_blueprint.json` packs, same MIVAS interface
(CHIRP + tool server + OTel) as the other harnesses.

| Folder | Stack | Multi-agent |
|---|---|---|
| `nemotron/` | Cascaded: Nemotron ASR → `nemotron-3-nano-30b-a3b` → Magpie TTS ([voice-agent blueprint](https://github.com/NVIDIA-AI-Blueprints/nemotron-voice-agent)) | Pipecat Flows (hard handoff) |
| `nemotron-voicechat/` | Full-duplex S2S: [`nvidia/nemotron-voicechat`](https://build.nvidia.com/nvidia/nemotron-voicechat) / [HF 11B](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-VoiceChat-11B) | One WS for the call; handoff = `session.update` (keeps history) |

Shared: `harness.py` (blueprint + industry tools), `report.py` (OTel → Bluejay).

## nemotron (cascaded)

Pipecat + NIM. Cloud NVCF is the default. See `bot.py` / `adapters/chirp.py`.

```bash
uv pip install -r voice-agent-harnesses/nvidia/requirements.txt
export NVIDIA_API_KEY=nvapi-...
uv run python run.py --harness nvidia/nemotron --mode check
uv run python run.py --harness nvidia/nemotron --mode chirp
```

Self-hosted NIM (one GPU ≥72 GB, NVIDIA workstation compose) — point the same harness at the box:

```bash
export NEMOTRON_LLM_BASE_URL=http://<gpu-host>:18000/v1
export NEMOTRON_LLM_MODEL=nvidia/nemotron-3-nano
export NEMOTRON_ASR_SERVER=<gpu-host>:50152
export NEMOTRON_TTS_SERVER=<gpu-host>:50151
export NEMOTRON_USE_SSL=false
export NEMOTRON_ASR_FUNCTION_ID=
# NVIDIA_API_KEY is optional when USE_SSL=false
# Workstation NIM serves nvidia/nemotron-3-nano, not the cloud catalog id.
```

On the GPU host, only the three sidecars are required (not the blueprint's python app):

```bash
git clone https://github.com/NVIDIA-AI-Blueprints/nemotron-voice-agent
cd nemotron-voice-agent
# NGC key in .env as NVIDIA_API_KEY
docker compose --profile generic-assistant/workstation up -d \
  nvidia-llm tts-service nemotron-asr-streaming-english
```

Host ports are 18000 (LLM HTTP), 50152 (ASR gRPC), 50151 (TTS gRPC). First pull + TRT compile is 30–60 minutes.

## nemotron-voicechat (full duplex) (WIP)

OpenAI Realtime–compatible WebSocket. Default is the hosted NVCF endpoint
(`wss://grpc.nvcf.nvidia.com/v1/realtime`, function `ai-nemotron-voicechat`) —
needs `NVIDIA_API_KEY` only (no local GPU).

**Multi-agent:** one VoiceChat WS for the whole call. The start agent gets that
agent’s pack instructions + tools only (industry-agnostic; no harness prompt hacks).
Handoff sends `session.update` with the target pack on the **same** socket so
conversation history stays — a cold second session + `response.create` is what
caused mid-call “Hello, how can I help?”.

**Speak-first:** VoiceChat is full-duplex — it only speaks while input audio flows.
Zero-PCM alone yields near-silent frames. With `VOICECHAT_SPEAKS_FIRST=true` (default),
**call open** sends a short speech-shaped kick (`assets/speak_first_kick.wav`) + trail
silence. After handoff the bridge waits for the DH to go quiet, then nudges with
`response.create` (history already on the socket — no greeting seed / drop hacks).

`agent.speech` / `customer.speech` are exclusive siblings under `voice.call`. Agent
spans open on transcript or sustained RMS (not kick glitches); customer spans coalesce
CHIRP VAD blips (`VOICECHAT_CUSTOMER_HANG_S`). Audio-transcript deltas are preferred
over text deltas so spans don't double words.

Idle CHIRP PCM is **not** forwarded to VoiceChat (it would barge-in on noise). Agent
audio is muted only on **real** barge-in: loud inbound RMS outside the echo-suppress
window after agent TTS, or NVIDIA `input_audio_buffer.speech_started`. Bluejay
`speech.started` alone is ignored — CHIRP VAD hearing Magpie/VoiceChat echo used to
chop every agent turn (`barge_in:chirp`).

**Tools:** the active session advertises that agent’s catalog tools. Hosted NVCF does not
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
| `NVIDIA_API_KEY` | cascaded NIM cloud + hosted VoiceChat NVCF. Optional when `NEMOTRON_USE_SSL=false` |
| `NEMOTRON_LLM_BASE_URL` | LLM HTTP base (default `https://integrate.api.nvidia.com/v1`; local NIM `:18000/v1`) |
| `NEMOTRON_LLM_MODEL` | LLM id (default `nvidia/nemotron-3-nano-30b-a3b` on NVCF; local NIM is `nvidia/nemotron-3-nano`) |
| `NEMOTRON_ASR_SERVER` | ASR gRPC host:port (default `grpc.nvcf.nvidia.com:443`; local NIM `:50152`) |
| `NEMOTRON_TTS_SERVER` | TTS gRPC host:port (default `grpc.nvcf.nvidia.com:443`; local NIM `:50151`) |
| `NEMOTRON_USE_SSL` | `true`/`false`. Unset: TLS if the ASR host is NVCF or `:443` |
| `NEMOTRON_ASR_FUNCTION_ID` | NVCF function id. Empty / omitted on a local NIM |
| `VOICECHAT_WS_URL` | VoiceChat Realtime WS (default `wss://grpc.nvcf.nvidia.com/v1/realtime`) |
| `VOICECHAT_FUNCTION_ID` | NVCF function id (default `42c86b5f-545a-4b2f-a83b-90fd71da9912`) |
| `VOICECHAT_SPEAKS_FIRST` | `true`/`false` (default `true`) — speech-kick at call open only |
| `VOICECHAT_SPEAK_FIRST_KICK_WAV` | optional override path for the speak-first kick (mono pcm16) |
| `VOICECHAT_AGENT_RMS_ON` | RMS gate for `agent.speech` (default `500`) |
| `VOICECHAT_USER_RMS_ON` | RMS gate for inbound CHIRP → VoiceChat (default `350`) |
| `VOICECHAT_CUSTOMER_HANG_S` | Coalesce CHIRP VAD blips into one `customer.speech` (default `2.0`) |
| `VOICECHAT_KICK_TRAIL_SILENCE_S` | Max trail silence after speak-first kick (default `3.5`) |
| `TOOL_SERVER_URL` | industry state API |
| `BLUEJAY_API_KEY` | OTel + `update-simulation-result` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | Bluejay websocket auth |
