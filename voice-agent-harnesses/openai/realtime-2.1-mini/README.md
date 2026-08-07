# OpenAI Realtime 2.1 Mini

Model: [`gpt-realtime-2.1-mini`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini)

From repo root:

```bash
uv sync
uv run python run.py --harness openai/realtime-2.1-mini --check
uv run python tests/converse.py --harness openai/realtime-2.1-mini
```

Harness-only (tool server must already be up):

```bash
uv run python voice-agent-harnesses/openai/realtime-2.1-mini/agent.py control-industry --check
```
