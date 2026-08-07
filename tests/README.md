# tests

## Automated

```bash
uv sync
uv run python tests/test_tool_server.py
```

## Speak to the agent (default)

Mic in, speakers out. Needs `OPENAI_API_KEY` in root `.env`, plus mic/speaker permissions.

```bash
uv sync
uv run python tests/converse.py
uv run python tests/converse.py --harness openai/realtime-2.1-mini
```

Say you want to schedule a repair, then give a date. Watch for `handoff>` / `tool_end>` in the terminal.

Ctrl+C to quit.

## Text fallback

```bash
uv run python tests/converse.py --text
```
