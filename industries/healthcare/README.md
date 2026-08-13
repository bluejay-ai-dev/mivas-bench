# healthcare

Straus Dermatology ("Robin")-style dermatology front-desk multi-agent industry for MIVAS, adapted from [straus-voice-agent](https://github.com/bluejay-ai-dev/straus-voice-agent).

Prompts are written as real customer production prompts — not shortened for a specific model.

## Agents

1. `reception` — greet, AI disclosure once, intent, KB, route
2. `identity` — PHI gate (name + DOB); only path to `billing` and `clinical`
3. `scheduling` — book / reschedule / cancel / waitlist / allergy
4. `coverage` — carrier × plan × office × provider
5. `cosmetic` — approved-table quotes, deposit + 72h policy before booking
6. `billing` — balance, charge explainers, payment link / financing / fee waiver
7. `clinical` — results status only, refills never approved, nurse messages, portal

English only for this industry. There is **no safety agent**. Escalation is a
single global tool, `transfer_to_human`. Handoffs between specialists are
invisible: every `transfer_to_*` takes a `handoff_summary`, and the receiving
agent continues mid-call (never re-greets).

## Escalation and refusal

| Situation | Behavior |
|---|---|
| Off-rails / horrible / jailbreak-like request | Say "Sorry, I can't help with that." Do not transfer. |
| Caller asks for a human | Call `transfer_to_human`. |
| Clinical emergency | Tell them to call 911, say "I'm transferring you to a human now," then `transfer_to_human`. |

## DB + state API

| Path | Role |
|------|------|
| `db/schema.sql` | SQLite schema (`locations`, `providers`, `patients`, `appointments`, `waitlist`) |
| `db/seed.sql` | Baseline offices + 2 patients + 1 upcoming visit |
| `tool_server.py` | FastAPI **state API** for durable DB ops (not a tools.json mirror) |
| `tools.json` | Agent-facing tool schemas |
| `agent_blueprint.json` | Wires tools: industry / `handoff` / `session` |
| `agent_blueprint.mmd` | Mermaid graph of the blueprint handoff edges |
| `system-prompts/*.md` | Full per-node prompts (shared CORE rules in each) |

Example state routes: `GET /patients`, `POST /appointments`, `PATCH /appointments/{id}`, `POST /waitlist`, `GET /state`, `GET /health`.

Harness tool kinds:
- **industry** (default) — e.g. `book_appointment` → `POST /appointments`
- **handoff** — e.g. `transfer_to_scheduling` (provider handoff)
- **session** — e.g. `end_call` (harness-native; closes the realtime session, no state API)

```bash
uv run python tool_server.py
# curl http://127.0.0.1:8000/state?call_id=675
# curl -X POST http://127.0.0.1:8000/appointments -H 'content-type: application/json' \
#   -H 'X-Mivas-Call-Id: 675' \
#   -d '{"location_id":"loc_park_ave","provider_id":"prov_chen","appointment_type_code":"MED_NEW","start":"2026-09-01T09:00:00","end":"2026-09-01T09:30:00","description":"New patient visit"}'
```
