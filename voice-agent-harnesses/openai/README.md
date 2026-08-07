# openai

OpenAI Realtime harness (`gpt-realtime-2.1`). Loads an industry `agent_blueprint.json` into a ready `RealtimeRunner`.

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...
python agent.py control-industry          # live session
python agent.py control-industry --check  # blueprint wiring only
```
