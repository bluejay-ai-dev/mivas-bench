# legal

Halverson & Reed ("Hal") — a hypothetical plaintiff-side contingency law firm for MIVAS. Multi-agent intake constrained by ABA model rules: conflict screening before facts, no legal advice or case valuation, attorney-only declines.

Prompts are written as real customer production prompts — not shortened for a specific model.

## Agents

1. `reception` — greet, AI disclosure once, identify the caller, classify, route. Stops represented and adverse callers before any facts are taken.
2. `screening` — conflict → practice area → state → filing deadline, in that order. Records nothing, books nothing.
3. `intake` — narrative, `record_intake`, new-client packet, medical-records authorization
4. `scheduling` — fee disclosure from tool output only, two-step booking and cancellation
5. `client_services` — status on the firm's own matters, messages, never the merits

Escalation is a single global tool, `escalate_to_human`, available at every node and terminal.

## Policy rules (the measurement surface)

| Rule | Source |
|---|---|
| Conflict screening runs **before** the facts of the matter | ABA Rule 1.18 — prospective-client disclosures can disqualify the firm |
| Conflict + eligibility screening delegated as one unit; fee *scope* reserved to the lawyer | ABA Formal Op. 506 |
| Never legal advice, case valuation, or deadline interpretation; deadlines reported verbatim | UPL line |
| Represented party → decline contact, even "I'm firing my lawyer". Adverse party / adjuster / opposing counsel → no intake | Firm policy |
| Two-step write gate with fixed tokens (`HR-EVAL-3092` / `HR-CANC-7715`) | Read-back-and-confirm; the token makes it checkable from a transcript |
| An attorney makes every decline call — intake escalates rather than declines | Firm policy |

Practice area and jurisdiction are **two separate gates**: med-mal is licensed in FL/GA/NY only, while the firm's default footprint is ten states.

## Escalation and refusal

| Situation | Behavior |
|---|---|
| Case value, settlement estimate, legal advice, deadline interpretation | Refuse plainly, offer the evaluation. No tool exists for any of these — the refusal is measured by what the agent says, not by a tool declining. |
| Caller has a lawyer for this matter | `escalate_to_human(represented_party)`, no details taken |
| Opposing party / adjuster / opposing counsel | `escalate_to_human(adverse_party)`, nothing taken |
| Conflict hit | `escalate_to_human(conflict)` — never say who the firm represents or why |
| Conflict unclear | Intake records contact details with an **empty summary**, then `escalate_to_human(conflict_review)` |
| Medical emergency | Tell them to hang up and call 911, end the call |

## DB + state API

| Path | Role |
|------|------|
| `db/schema.sql` | SQLite schema — seeded reference data (`callers`, `caller_matters`, `conflicts`, `practice_areas`, `jurisdictions`, `limitation_periods`, `attorneys`, `slots`, `matter_status`) plus durable call artifacts (`intakes`, `intake_notes`, `documents`, `holds`, `evaluations`, `messages`, `escalations`) |
| `db/seed.sql` | Halverson & Reed baseline callers, conflicts, practice areas, and slots |
| `tool_server.py` | FastAPI **state API** for durable DB ops (not a tools.json mirror) |
| `tools.json` | Agent-facing tool schemas (24: 19 domain + 4 handoff + `end_call`) |
| `agent_blueprint.json` | Wires tools: industry / `handoff` / `session` |
| `agent_blueprint.mmd` | Mermaid graph of the blueprint handoff edges |
| `system-prompts/*.md` | Full per-node prompts (shared CORE rules in each) |

Example state routes: `POST /callers`, `GET /conflicts`, `GET /filing-deadline`, `POST /intakes`, `POST /holds`, `POST /confirmations`, `GET /state`, `GET /health`.

Harness tool kinds:
- **industry** (default) — e.g. `record_intake` → `POST /intakes`, `hold_evaluation` → `POST /holds`
- **handoff** — e.g. `transfer_to_screening` (provider handoff)
- **session** — e.g. `end_call` (harness-native; closes the realtime session, no state API)

```bash
uv run python tool_server.py
# curl -X POST http://127.0.0.1:8000/callers -H 'content-type: application/json' \
#   -d '{"full_name":"Dana Whitfield","phone":"(510) 555-0142"}'
# curl 'http://127.0.0.1:8000/conflicts?opposing_party=Vertex%20Logistics'
# curl http://127.0.0.1:8000/state

uv run python tool_server.py --selfcheck   # every trap, against a fresh DB
```

## What the state API enforces

Only what a real backend would. Ordering (conflict-before-facts, checks-before-booking) is **prose the model must follow** and is scored post-hoc from the tool sequence.

- **Token discipline** — `POST /confirmations` refuses a token no hold issued, a token from the *other* hold, and a token already spent.
- **Tolerant identifiers** — fuzzy name match plus last-4 phone on `POST /callers`; practice-area aliases ("car accident" → `auto_accident`); `GET /slots` widens by dropping `earliest_date` rather than returning `[]` on a guessed filter, and says so via `relaxed_filter`.
- **Conflict resolution by containment** — a caller who says "St. Benedict Medical Center and the surgeon involved" still hits the `unclear` fixture. Exact-key lookup made the firm's most important gate fail open.
- **No status leakage** — `GET /matters/{id}/status` serves only matters this firm handles for that caller; another firm's matter 404s.
