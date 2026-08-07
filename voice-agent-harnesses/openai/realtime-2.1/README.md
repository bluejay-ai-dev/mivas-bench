# OpenAI Realtime 2.1

Model: [`gpt-realtime-2.1`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)

From repo root:

```bash
uv sync
uv run python run.py --harness openai/realtime-2.1 --check
uv run python tests/converse.py --harness openai/realtime-2.1
```

Harness-only (tool server must already be up):

```bash
uv run python voice-agent-harnesses/openai/realtime-2.1/agent.py control-industry --check
```
