# control-industry

The Control Industry is a setup-and-validation check for MIVAS benchmark runs. When fully configured, it is an extremely simple multi-agent system — not a realistic customer voice-agent design.

It models a hypothetical business called **Bluejay's Repair Services**. The only thing callers can do is schedule a repair appointment.

## Flow

1. Starts with the **receptionist** agent
2. Hands off to the **scheduler** agent
3. The scheduler books a repair appointment using a single tool: **Schedule Appointment**

## Purpose

Use the Control Industry for every agent you want to benchmark with this repo. Spin it up to confirm you can get that agent to book an appointment. If that works, your MIVAS bench setup is wired correctly.

This industry does **not** mirror how customers build voice agents. It is a control test for validating that MIVAS is set up properly.

## Expected outcome

Your agent schedules a generic repair appointment, and evaluations show database state reflecting a scheduled appointment (`GET /state` on the state API).

## DB + state API

| Path | Role |
|------|------|
| `db/schema.sql` | SQLite schema (`appointments`) |
| `db/seed.sql` | Initial empty baseline |
| `tool_server.py` | FastAPI **state API** for durable DB ops (not a tools.json mirror) |
| `tools.json` | Agent-facing tool schemas |
| `agent_blueprint.json` | Wires tools: industry / `handoff` / `session` |

Example state routes: `POST /appointments`, `GET /appointments`, `GET /state`, `GET /health`.

Harness tool kinds:
- **industry** (default) — e.g. `schedule_appointment` → `POST /appointments`
- **handoff** — e.g. `handoff_to_scheduler` (provider handoff)
- **session** — e.g. `end_call` (harness-native; closes the realtime session, no state API)

```bash
uv run python tool_server.py
# curl -X POST http://127.0.0.1:8000/appointments -H 'content-type: application/json' -d '{"date":"08/07/2026"}'
# curl http://127.0.0.1:8000/state
```
