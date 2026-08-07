# openai

OpenAI Realtime harnesses. Each subfolder is one model runtime; cross with any `industries/*/agent_blueprint.json`.

| Folder | Model |
|---|---|
| `realtime-2.1/` | [`gpt-realtime-2.1`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1) |
| `realtime-2.1-mini/` | [`gpt-realtime-2.1-mini`](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini) |

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...
python realtime-2.1/agent.py healthcare
python realtime-2.1-mini/agent.py healthcare
```
