# openai

OpenAI Realtime harnesses. Each subfolder is one model runtime; tools go through the industry `tool_server.py`.

| Folder | Model |
|---|---|
| `realtime-2.1/` | [`gpt-realtime-2.1`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1) |
| `realtime-2.1-mini/` | [`gpt-realtime-2.1-mini`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini) |

Shared builder: `runtime.py` (HTTP proxy → `TOOL_SERVER_URL`).

```bash
uv sync
# set VOICE_AGENT=openai/realtime-2.1 in root .env
uv run python run.py --check
uv run python tests/converse.py
```
