# nvidia

Two NVIDIA runtimes over industry `agent_blueprint.json` packs, same MIVAS interface
(CHIRP + tool server + OTel) as the other harnesses.

| Folder | Stack | Multi-agent |
|---|---|---|
| `nemotron/` | Cascaded: Nemotron ASR → `nemotron-3-nano-30b-a3b` → Magpie TTS ([voice-agent blueprint](https://github.com/NVIDIA-AI-Blueprints/nemotron-voice-agent)) | Pipecat Flows (hard handoff) |
| `nemotron-voicechat/` | Full-duplex S2S: [`nvidia/nemotron-voicechat`](https://build.nvidia.com/nvidia/nemotron-voicechat) / [HF 11B](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-VoiceChat-11B) | Soft (all tools up front; handoff returns next prompt) |

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

OpenAI Realtime–compatible WebSocket to a VoiceChat NIM
(`ws://host:9000/v1/realtime`). Default points at a local container; override with
`VOICECHAT_WS_URL` for a remote host / early-access endpoint.

```bash
# terminal A — VoiceChat NIM (GPU ≥ 80 GB; see HF deploy docs)
docker run -it --rm --name=nemotron-labs-voicechat \
  --runtime=nvidia --gpus '"device=0"' --shm-size=8GB \
  -e NIM_HTTP_API_PORT=9000 -p 9000:9000 \
  -v $(pwd)/nemotron-labs-voicechat_v1.0.0:/data/models \
  --entrypoint /s2s/run_s2s_server.sh \
  nvcr.io/nim/nvidia/nemotron-labs-voicechat:latest

# terminal B — industry tool server + CHIRP
export VOICECHAT_WS_URL=ws://127.0.0.1:9000/v1/realtime
uv run python industries/control-industry/tool_server.py
uv run python voice-agent-harnesses/nvidia/nemotron-voicechat/agent.py control-industry --check
CHIRP_PORT=8765 uv run python voice-agent-harnesses/nvidia/nemotron-voicechat/adapters/chirp.py
# or: uv run python run.py --harness nvidia/nemotron-voicechat --mode chirp
```

Wire format is 24 kHz PCM both ways (server resamples internally); CHIRP stays 16 kHz.

## Env

| var | use |
|---|---|
| `NVIDIA_API_KEY` | cascaded NIM cloud (ASR/LLM/TTS) |
| `VOICECHAT_WS_URL` | VoiceChat Realtime WS (default `ws://127.0.0.1:9000/v1/realtime`) |
| `TOOL_SERVER_URL` | industry state API |
| `BLUEJAY_API_KEY` | OTel + `update-simulation-result` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | Bluejay websocket auth |
