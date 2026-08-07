# mivas-bench

Multi-Industry Voice Agent Simulation Bench (MIVAS) evaluates voice agent performance across the industries that are adopting voice AI, inspired by the real ways they deploy voice agents. Today, voice agents are deployed as multi-agent systems with access to database state, external tools, and internal agent handoffs.

## Runtime model

Pick a **harness** from `voice-agent-harnesses/` (e.g. `openai/realtime-2.1`) and an **industry** from `industries/`. Each industry owns:

- `agent_blueprint.json`, `tools.json`, system prompts
- SQLite `db/schema.sql` + `db/seed.sql`
- `tool_server.py` (FastAPI routes matching non-handoff tools)

The harness adapts that pack into a voice runtime and proxies tool calls to the industry tool server. One container packs harness + industry + DB + tool server.

## Quick start (local)

Requires [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env   # set VOICE_AGENT=openai/realtime-2.1, INDUSTRY, OPENAI_API_KEY
uv sync

uv run python run.py              # reads .env, starts tool server + agent
uv run python run.py --check      # blueprint wiring only
uv run python tests/converse.py   # speak to the agent (mic + speakers)
```

## Kubernetes

Each harness × industry run is one Job / one image (`mivas-bench:$harness-$industry`). Image builds install deps with `uv`.

```bash
# Ensure a cluster context (kind, minikube, remote, …)
uv run python run.py --build --apply
```

See `k8s/secret.example.yaml` and `k8s/job.yaml`. Bluejay eval invocation is still TBD.
