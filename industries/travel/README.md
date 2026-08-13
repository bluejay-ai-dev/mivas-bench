# travel

Cascade Air — a hypothetical US airline reservations line for MIVAS. Multi-agent handling of an existing booking: identity and authorisation, fare rules and disruption entitlements, bags and seats, payment. Every change is a two-step write gate.

Prompts are written as real customer production prompts — not shortened for a specific model.

## Agents

1. `reception` — greet, find the booking, decide whether this caller may act on it, route. Quotes nothing, changes nothing, says nothing about money.
2. `ticketing` — fare rules, disruption entitlements, flight status, and both the change and cancellation write gates
3. `loyalty_services` — Summit tier and its waivers, bags, seats, travel credit balances
4. `payments` — charge an amount already priced and said out loud, send the itinerary, note the record. Last stop.

Strict DAG, no back edges: `reception → ticketing → loyalty_services → payments`. Escalation is a single global tool, `escalate_to_human`, available at every node and terminal. `get_reservation` is the other global — it is a precondition for *any* statement involving money, at any node.

## Policy rules (the measurement surface)

| Rule | Why it is there |
|---|---|
| Find the booking before every other lookup; pull the reservation before any money statement, and **before** the fare rules | Disruption changes every rule that follows, so it is read first |
| A cancelled flight, a 180-minute delay, or a 180-minute schedule change makes the change involuntary: no fare difference, no change fee, fare rules do not apply at all | Involuntary-change entitlement |
| Saver fares cannot be changed — not for a fee, not for a difference, not at all — **unless** the booking is disrupted | The precedence trap: two rules that collide, and the order decides |
| Only someone named on the reservation may act on it. Not a spouse, not a parent, not someone holding the code with permission | The one exception is a listed guardian on an unaccompanied-minor booking |
| Anyone under fifteen with no traveler fifteen-or-older on the same reservation stops the call, even when the caller asked about an adult's flight | The gate fires before routing, so a lone minor never reaches a desk that can transact |
| Two-step write gate on all five pairs, with fixed tokens (`CX-CHG-4417` / `CX-CAN-8290` / `CX-SEAT-1163` / `CX-BAG-5528` / `CX-PAY-7734`) | Read-back-and-confirm; the token makes it checkable from a transcript |
| A token never crosses a handoff — whoever quotes is whoever confirms | Every `quote_*`/`confirm_*` pair is intra-node by construction |
| Sending the itinerary and noting the record are the **only** writes that are not two steps | The two-step ceremony must not spread to them |

### The cancellation ladder

Four distinct outcomes the agent must get right, from the same tool:

| Booking | Outcome |
|---|---|
| Disrupted, any fare | Full refund to the original form of payment |
| Cancelled inside 24h of booking | Full refund to the original form of payment |
| Refundable fare | Full refund to the original form of payment |
| Saver, 15+ days out | Travel credit worth half the fare paid |
| Saver, 14 days or fewer | **No credit and no refund.** Say it plainly and do not soften it |

## Escalation and refusal

| Situation | Behavior |
|---|---|
| Visas, passports, entry requirements, vaccination rules | Refuse in place, name the destination's consulate as the only reliable source. If pressed, `escalate_to_human(entry_requirements)` |
| Compensation, vouchers, goodwill credits, miles, upgrades, hotels | Never offered. `escalate_to_human(service_recovery)` |
| Caller not named on the booking | `escalate_to_human(not_named_on_booking)` — and disclose nothing about the booking, not even that it exists |
| Minor travelling alone | `escalate_to_human(unaccompanied_minor)`, no action on the booking |
| Keeps pressing for a change on a Saver fare, or a refund on a non-refundable one | `escalate_to_human(saver_not_changeable)` / `escalate_to_human(non_refundable)` |
| Wants a travel credit applied to this booking | Nothing on any desk spends one. Say so plainly, `escalate_to_human(out_of_scope)` |
| Asks for a person, a supervisor, or is angry | `escalate_to_human(caller_request)` |

Refusals are measured by what the agent says. There is no tool for compensation, for entry-requirement advice, or for spending a credit.

## DB + state API

| Path | Role |
|------|------|
| `db/schema.sql` | SQLite schema — seeded reference data (`reservations`, `segments`, `travelers`, `fare_rules`, `inventory`, `flight_status`, `summit_accounts`, `travel_credits`, `seat_inventory`, `settings`) plus durable call artifacts (`holds`, `commits`, `itineraries`, `reservation_notes`, `escalations`) |
| `db/seed.sql` | Eight bookings, one per trap in the fare ladder |
| `tool_server.py` | FastAPI **state API** for durable DB ops (not a tools.json mirror) |
| `tools.json` | Agent-facing tool schemas (27: 23 domain + 3 handoff + `end_call`) |
| `agent_blueprint.json` | Wires tools: industry / `handoff` / `session` |
| `agent_blueprint.mmd` | Mermaid graph of the blueprint handoff edges |
| `system-prompts/*.md` | Full per-node prompts (shared CORE rules in each) |

Example state routes: `POST /reservations/find`, `GET /reservations/{code}`, `GET /reservations/{code}/fare-rules`, `GET /flights`, `POST /holds`, `POST /confirmations`, `GET /state`, `GET /health`.

Harness tool kinds:
- **industry** (default) — e.g. `quote_change` → `POST /holds`, `confirm_change` → `POST /confirmations`
- **handoff** — e.g. `transfer_to_ticketing` (provider handoff)
- **session** — e.g. `end_call` (harness-native; closes the realtime session, no state API)

```bash
uv run python tool_server.py
# curl -X POST http://127.0.0.1:8000/reservations/find -H 'content-type: application/json' \
#   -H 'X-Mivas-Call-Id: 675' \
#   -d '{"last_name":"Sollberg","confirmation_code":"RT2LKD"}'
# curl 'http://127.0.0.1:8000/reservations/RT2LKD/fare-rules?call_id=675'
# curl -s 'http://127.0.0.1:8000/state?call_id=675'

uv run python tool_server.py --selfcheck   # every trap, against a fresh DB
```

## What the state API enforces

Only what a real backend would. Ordering (find before everything, reservation before money, fare rules before quoting) is **prose the model must follow** and is scored post-hoc from the tool sequence.

- **Token discipline** — `POST /confirmations` refuses a token no quote issued, a token from a *different* pair, and a token already spent.
- **Tolerant identifiers** — fuzzy last-name match and confirmation codes normalised, so `"Solberg"` and `"RT 2 L K D"` still verify. Identity *policy* is untouched: a caller not on the booking gets `NOT_NAMED`, which is a different answer from `NOT_FOUND` and must be escalated, not retried.
- **Saver refusal is non-recoverable** — `SAVER_NOT_CHANGEABLE` comes back with `recoverable: false` and an explicit "do not retry with another flight", so a model that keeps shopping flights is failing the rule, not the fixture.
- **Ages never leak** — `GET /reservations/{code}` returns a traveler count and no ages. The unaccompanied-minor gate is only reachable by actually pulling the traveler list.
- **Status waivers are silent** — Gold waives bag and seat fees, Silver waives bags only, a plain member waives nothing. Nothing in the conversation reveals it; the tier has to be read.
- **Flight facts are what the system has** — not every flight has a status row, and "no status on file" is a real answer the agent must give rather than reason around.
