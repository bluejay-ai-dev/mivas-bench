# qwen

Qwen-Audio Realtime harnesses. Each subfolder is one model runtime.

| Folder | Model |
|---|---|
| `audio-realtime/` | [`qwen-audio-3.0-realtime-plus`](https://help.aliyun.com/en/model-studio/qwen-audio-realtime-user-guides) (DashScope / Model Studio WebSocket) |

Frontend-only: no ACP coding-agent backend. Shared builder: `harness.py`. Tracing: `report.py` (`BLUEJAY_SERVICE_NAME=mivas-qwen`, provider `dashscope`).

- Industry tools → `POST {TOOL_SERVER_URL}/tools/{name}`
- Session tools (`session: true`, e.g. `end_call`) → harness-local + delayed close
- Handoffs → soft `session.update` on the same WebSocket (history stays)
- Speak-first: seed a user `conversation.item.create`, then `response.create` (pack owns greeting text; Qwen rejects a bare create on an empty conversation)
- Audio: Qwen-Audio PCM 16 kHz in / 24 kHz out ↔ CHIRP 16 kHz `pcm_s16le`
- Barge-in: Qwen `server_vad` (never mute on CHIRP VAD alone)
- Clock: `Today is …` injected into every session instructions
- Tools: nested `{type: function, function: {name, description, parameters}}` per Model Studio docs
- Tracing → LangSmith-shaped Bluejay OTel `realtime_session` → `turn` → {user_message, model (gen_ai.usage.* tokens + TTFT), execute_tool}; Chirp stamps `X-Simulation-Result-Id`

```bash
uv sync
# DASHSCOPE_API_KEY + QWEN_WORKSPACE_ID (or QWEN_WS_URL) in root .env
uv run python run.py --harness qwen/audio-realtime --mode check
uv run python industries/control-industry/tool_server.py
CHIRP_PORT=8769 CHIRP_USER=mivas CHIRP_PASS=mivas \
  BLUEJAY_SERVICE_NAME=mivas-qwen \
  uv run python run.py --harness qwen/audio-realtime --mode chirp
```

## Env

| var | use |
|---|---|
| `DASHSCOPE_API_KEY` | Model Studio API key (`Authorization: Bearer …`) |
| `QWEN_API_KEY` | alias for `DASHSCOPE_API_KEY` |
| `QWEN_AUDIO_MODEL` | default `qwen-audio-3.0-realtime-plus` |
| `QWEN_AUDIO_VOICE` | session voice (default `longanqian`; only honored on the first `session.update`) |
| `QWEN_WORKSPACE_ID` | Model Studio workspace id (builds `wss://{id}.{region}.maas.aliyuncs.com/api-ws/v1/realtime`) |
| `QWEN_REGION` | default `us-east-1` |
| `QWEN_WS_URL` | override WS base (takes precedence over workspace id) |
| `TOOL_SERVER_URL` | industry state API |
| `BLUEJAY_API_KEY` | OTel + `update-simulation-result` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | Bluejay websocket auth (default port `8769`) |

## Bluejay

| field | value |
|---|---|
| CHIRP port | `8769` local / `8765` in-pod |
| k8s host | `wss://qwen-audio-realtime-control-industry.benchmarks.getbluejay.ai` |
| creds | `CHIRP_USER`/`CHIRP_PASS`=`mivas` |
| agent_id | `38007` (`control-industry:qwen-audio-realtime`) |
| simulation_id | `30595` |
| booker DH | `196286` |
| cloned from | OpenAI agent `28004` |
| smoke run | `236216` (results `746147` `746148` `746149`) |

`qwen-audio-3.0-realtime-plus` is Beijing + Singapore only. This deploy uses a Singapore (`ap-southeast-1`) Model Studio workspace.
