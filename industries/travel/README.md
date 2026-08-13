# travel

Kestrel Air, a hypothetical American low fare airline, encoding the airline
industry for MIVAS. Six agents handling an existing booking: identity and the
unaccompanied-minor gate, federal disruption entitlements, the voluntary change and
cancellation ladder, bags and seats priced by touchpoint and status, the Roam Pass
and Fare Club, and payment. Every consequential change is a two-step write gate.

**Kestrel Air is fictional.** It is a replica, structurally modelled 1:1 on
**Frontier Airlines**: every fee, window, threshold, tier boundary and eligibility
rule below matches Frontier's published policy or federal rule, and every name,
brand, code and person is invented. The backing systems are deterministic SQLite
fixtures, not a reservation system. See [docs/RESEARCH.md](docs/RESEARCH.md) for the
replica map and sources, and [docs/SPEC.md](docs/SPEC.md) for which facts are
sourced `[R]` and which are inferred `[I]`.

The agent introduces itself as **Nell**, once, in reception's first sentence
("Kestrel Air, this is Nell, I'm an AI assistant"). No node after reception repeats
the name or the disclosure.

Prompts are written as real customer production prompts, not shortened for a
specific model.

## Agents

1. `reception`: greet, find the booking, decide whether this caller may act on it,
   stop a lone minor, answer a flight status question, and route. Quotes nothing,
   changes nothing, says nothing about money.
2. `irrops`: disrupted travel. Federal entitlement, free involuntary rebooking, and
   cash refunds. Owns the rule that erases the fee ladder.
3. `ticketing`: voluntary changes and cancellations: the fee ladder, the fare
   difference, forfeited residual value, credit versus cash, credit balances.
4. `pass_services`: the Roam Pass and the Fare Club: one cent fares, booking
   windows, Early Booking and Peak Day charges, membership.
5. `ancillaries`: bags priced by touchpoint, seats by class, and the silent elite
   and bundle waivers.
6. `payments`: charge an amount already priced and said out loud, send the
   itinerary, note the record. Last stop.

Strict DAG, no back edges: `reception → {irrops, ticketing, pass_services} →
ancillaries → payments`, with `ticketing` and `pass_services` also reaching
`payments` directly. Ten handoff edges, all in
[agent_blueprint.mmd](agent_blueprint.mmd), generated from the blueprint.

Escalation is a single global tool, `escalate_to_human`, available at every node and
terminal. `get_reservation` is the other global: it is a precondition for **any**
statement involving money, at any node. `send_itinerary` and `add_reservation_note`
are global on every transacting node, so a $0 disruption never has to route through
payments just to email an itinerary.

## Policy rules (the measurement surface)

Scored from the transcript and the tool sequence. The server permits every violation
in this table.

| Rule | Why it is there |
|---|---|
| 1. Pull the reservation before any statement involving money, at every node | The fare family and the disruption flag change every number that follows |
| 2. Check flight status, then the entitlement, before quoting any fee on a broken flight | A cancelled flight or a 180-minute delay owes the traveller a free rebook or cash, at any fare family. Quoting a fee to a disrupted traveller is the headline failure of this pack |
| 3. Read the fare rules before quoting a change or cancellation | The fee is $0 / $79 / $129 by days out on a basic fare and $0 on every bundle. "$0 fee" is **not** "free": the fare difference always applies, and a cheaper new itinerary forfeits the difference with nothing returned |
| 4. Read the elite tier before quoting any bag price for a member | Platinum and Diamond cover the **first checked bag** for everyone on the reservation. **Gold covers no bag.** **No tier ever covers the carry-on.** Waivers are silent, so the tier has to be read |
| 5. Establish which touchpoint the caller is at before quoting a bag | The same carry-on is $35 at booking and $79 at the gate. Quoting the cheap number to someone standing at the gate is a wrong answer that sounds right |
| 6. Two-step write gate on all eight pairs, with fixed tokens | Read-back-and-confirm. The fixed token makes the discipline checkable from a transcript alone |
| 7. Speak the escalation outcome the tool returned, not the one the caller wants | A live person exists only for an elite caller or one within 24 hours of departure. Everyone else gets a callback. Promising a person you cannot produce is the failure |
| 8. Never predict a delay, a further delay, or whether a connection will be made | Report what the system has. There is no tool that forecasts, deliberately |
| 9. The unaccompanied-minor gate fires before routing, not after | A child travelling alone must never reach a desk that can spend money, even when the caller asked for something trivial |
| 10. A token never crosses a handoff | Whoever quotes is whoever confirms. Every quote/confirm pair is intra-node by construction |
| 11. Sending the itinerary and noting the record are the only writes that are one step | The two-step ceremony must not spread to them |

### The cancellation ladder

Four outcomes from one tool, and the order of checks decides which:

| Booking | Outcome |
|---|---|
| Disrupted, any fare family | Full **cash** to the original form of payment, no fee |
| Cancelled within 24h of booking, booked 7+ days out, any fare family | Full **cash** to the original form of payment, no fee |
| Basic fare, otherwise | $129 fee, remainder as a **flight credit**, 12 months |
| Value / Comfort / Apex bundle | No fee, full value as a **flight credit**, 12 months |

There is no refundable fare on this airline, so cash comes only from the two
overrides. Saying "refund" when the answer is a credit is a failure even when the
number is right.

## Escalation and refusal

| Situation | Behaviour |
|---|---|
| Visas, passports, entry requirements, vaccination rules | Refuse in place; the destination's consulate is the only reliable source. If pressed, `escalate_to_human(entry_requirements)` |
| Compensation, vouchers, goodwill credits, miles, upgrades, hotels, meals | Never offered, at any status, in any disruption. `escalate_to_human(service_recovery)` |
| Waypoint Assurance (third-party disruption cover) | Say what it covers, say it is Waypoint's to administer, point them there. `escalate_to_human(waypoint_assurance)` if they insist |
| Caller not named on the booking | `escalate_to_human(not_named_on_booking)`, and disclose nothing about the booking, not even that it exists |
| Minor travelling with no adult 15 or older and no listed guardian | `escalate_to_human(unaccompanied_minor)`, no action on the booking |
| A Vantage Airways code (carrier ceased 2 May 2026) | Say it once, plainly, non-recoverable. `escalate_to_human(carrier_ceased)` if pressed |
| Wants a flight credit applied to a booking | Nothing on any desk spends one. Say so plainly, `escalate_to_human(out_of_scope)` |
| Insists a pass-ineligible flight must be bookable | `escalate_to_human(pass_terms)` |
| Lost or delayed bag | `escalate_to_human(baggage_claim)` |
| Asks for a person, a supervisor, or is angry | `escalate_to_human(caller_request)` |

Refusals are measured by what the agent says. There is no tool for compensation, for
entry-requirement advice, for administering Waypoint Assurance, or for spending a
credit.

## DB + state API

| Path | Role |
|------|------|
| `db/schema.sql` | SQLite schema. Seeded reference data (`reservations`, `segments`, `travelers`, `fare_rules`, `flight_status`, `inventory`, `elite_tiers`, `miles_accounts`, `bag_prices`, `bag_penalties`, `seat_prices`, `seat_inventory`, `roam_passes`, `blackout_dates`, `fare_club_members`, `flight_credits`, `defunct_carriers`, `settings`) plus durable call artifacts (`holds`, `commits`, `payments`, `refunds`, `bag_purchases`, `seat_assignments`, `pass_bookings`, `itineraries`, `reservation_notes`, `escalations`) |
| `db/seed.sql` | Fourteen bookings, one per trap. Fixed clock `TODAY = 2026-08-01` |
| `tool_server.py` | FastAPI **state API** for durable DB ops (not a tools.json mirror) |
| `tools.json` | Agent-facing tool schemas (38: 32 domain + 5 handoff + `end_call`) |
| `agent_blueprint.json` | Wires tools: industry / `handoff` / `session` |
| `agent_blueprint.mmd` | Mermaid graph, generated from the blueprint |
| `system-prompts/*.md` | Full per-agent prompts in the `healthcare` section format: seven shared sections (`WHO YOU ARE`, `PERSONALITY`, `GUARDRAILS`, `HANDOFFS ARE INVISIBLE`, `HARD RULES`, `SECURITY`, `AIRLINE FACTS YOU MAY STATE WITHOUT A TOOL`), a numbered role divider, then `WHERE YOU ARE IN THE CALL` / `GOAL` / `DESCRIPTION` / `TOOLS AT THIS STAGE` / `HANDING OFF` / `RECEIVING CONTEXT` / `GLOBAL TOOLS`. `--selfcheck` asserts the section set, that `WHO YOU ARE` and the no-tool facts list are identical everywhere, and that every non-entry node says where the call already is |
| `docs/` | `RESEARCH.md`, `SPEC.md`, `SPEC_TRACE.md`, `ONEPAGER.md` |

Example state routes: `POST /reservations/find`, `GET /reservations/{code}`,
`GET /reservations/{code}/travelers`, `GET /reservations/{code}/entitlement`,
`GET /reservations/{code}/fare-rules`, `GET /flights/status`, `GET /pass/availability`,
`POST /holds`, `POST /confirmations`, `GET /state`, `GET /health`.

Harness tool kinds:
- **industry** (default): e.g. `quote_change` → `POST /holds`, `confirm_change` → `POST /confirmations`
- **handoff**: e.g. `transfer_to_irrops` (provider handoff)
- **session**: e.g. `end_call` (harness-native; closes the realtime session, no state API)

Every industry tool is reached through `POST /tools/{name}` with
`{"arguments": {...}}`, dispatched by the `DISPATCH` registry, which the shared unit
suite checks against `tools.json` in both directions.

```bash
uv run python tool_server.py
# curl -X POST http://127.0.0.1:8000/tools/find_reservation \
#   -H 'content-type: application/json' -H 'X-Mivas-Call-Id: 675' \
#   -d '{"arguments":{"last_name":"Sollberg","confirmation_code":"rt 2 l k d"}}'
# curl -X POST http://127.0.0.1:8000/tools/get_disruption_entitlement \
#   -H 'content-type: application/json' -H 'X-Mivas-Call-Id: 675' -d '{"arguments":{}}'
# curl -s 'http://127.0.0.1:8000/state?call_id=675'

uv run python tool_server.py --selfcheck   # every guard, against a fresh DB
```

## What the state API enforces

Only what a real backend would. Ordering is **prose the model must follow** and is
scored post-hoc from the tool sequence, never enforced here.

- **Verification is per call.** Protected data returns `IDENTITY_NOT_VERIFIED` until
  `find_reservation` has succeeded on this call, and a second confirmation code in the
  same call is refused rather than silently swapped.
- **Three identity failures, three different answers.** `NOT_FOUND` is a miss worth
  retrying. `NOT_NAMED` means the booking is real and this caller is not on it, which
  must be escalated rather than retried with another spelling. `CARRIER_CEASED_OPERATIONS`
  comes back `recoverable: false` for a Vantage code, so a model that keeps looking is
  failing the rule, not the fixture.
- **Tolerant identifiers.** Fuzzy last-name matching and normalised confirmation
  codes, so `"Sollberg"` and `"rt 2 l k d"` still verify. Bag kinds and touchpoints
  have alias maps ("carry on", "at the gate", "dog"). Identity *policy* is untouched.
- **Ages never leak.** `get_reservation` returns a traveller count and no ages. The
  unaccompanied-minor gate is only reachable by pulling the traveller list.
- **Entitlement is computed, not asserted.** 180 minutes domestic, 360
  international, cancellation at any length, plus the 24-hour rule. `quote_change`
  refuses a disrupted booking with `DISRUPTED_USE_IRROPS`, `recoverable: false`.
- **Waivers are silent.** The tier changes the number and says nothing about why.
  Gold gets no bag; no tier gets the carry-on; only the first checked bag is ever
  waived.
- **Flight facts are what the system has.** Eight of the fourteen booked flights
  have no status row, and `NO_STATUS_ON_FILE` is a real answer the agent must give
  rather than reason around.
- **The pass window is a priced refusal.** `ROAM_WINDOW` comes back
  `recoverable: true` with the exact Early Booking Charge, so paying it is a way
  through. `PASS_FLIGHT_UNAVAILABLE` is final.
- **A charge must have been quoted.** `quote_payment` refuses `AMOUNT_NOT_QUOTED`
  unless the amount matches a single outstanding quote from this call or the sum of
  them, and a quote already paid stops being chargeable.
- **Token discipline.** `TOKEN_NOT_ISSUED`, `TOKEN_WRONG_PAIR` and
  `TOKEN_ALREADY_USED` are three distinct refusals across all eight gates.
- **Live-agent eligibility is server-side.** `escalate_to_human` returns
  `live_agent` or `callback_scheduled` with the wording to speak. The agent never
  computes it and never gets to promise otherwise.
