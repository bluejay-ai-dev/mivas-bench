# openai

OpenAI Realtime harnesses. Each subfolder is one model runtime.

| Folder | Model |
|---|---|
| `realtime-2.1/` | [`gpt-realtime-2.1`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1) |
| `realtime-2.1-mini/` | [`gpt-realtime-2.1-mini`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini) |

Shared builder: `harness.py`.

- Industry tools → industry state API (`TOOL_SERVER_URL`)
- Session tools (`session: true`, e.g. `end_call`) → harness-local + close realtime session
- Handoffs → Realtime handoffs

Callers must `context["session"] = session` after `runner.run(context=ctx)`.

```bash
uv sync
# set VOICE_AGENT=openai/realtime-2.1 in root .env
uv run python run.py --check
uv run python tests/converse.py
```
