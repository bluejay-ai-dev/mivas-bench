# SPEC_TRACE: spec to flow verification

Two full re-read passes of [SPEC.md](SPEC.md) against the agent graph, before any
code was written. This records the trace and what each pass changed.

---

## 1. The split rule

Specialists are split by **money-and-policy boundary**, not by topic. Same script,
same money, same disclosure → same agent. Different money, different disclosure,
or a different refusal discipline → different agent.

| Agent | The money it owns | Why it cannot merge with its neighbour |
|---|---|---|
| `reception` | **None.** Quotes nothing, changes nothing, says nothing about money | It is the only node that greets and the only node that can refuse a caller *before* any desk that transacts sees them |
| `irrops` | Federal entitlement money: $0 fees, cash to the original form of payment, thresholds in minutes | Its disclosure is the DOT rule and its fee schedule is "none". Merging it with `ticketing` would put the fee ladder and the rule that erases the fee ladder in one instruction set |
| `ticketing` | Carrier-policy money: the $0/$79/$129/$99 ladder, fare difference, forfeited residual, $129 cancellation, 12-month credit | Different money, and its refusals are carrier policy rather than federal rule |
| `ancillaries` | Fee-table money: bags by touchpoint, seats by class, silent elite waivers | Different money again, and the only node where a *silent* waiver changes the number spoken |
| `pass_services` | Subscription money: $0.01 base fares, $29 to $89 early booking, $79 to $159 peak day, $59.99 + $50 membership | An entirely different pricing model. A $0.01 fare and a $129 change fee cannot share a prompt without one contaminating the other |
| `payments` | The card | The only node that touches a payment instrument, and the only one whose discipline is "charge an amount already priced and said out loud" |

Six agents, inside the 4 to 7 landing zone the skill describes (legal has 5,
healthcare 7).

---

## 2. Spec → agent → tool trace

Every intent in [SPEC.md §1 and RESEARCH.md §1](RESEARCH.md), with its owning
agent and the tools that serve it. Exactly one owner each.

| Intent | Share | Owner | Tools |
|---|---|---|---|
| Flight status, on-time flight | ~20% | `reception` | `get_flight_status` |
| Flight status, disrupted flight | (part of above) | `reception` → `irrops` | `get_flight_status`, `get_disruption_entitlement` |
| Involuntary rebooking | ~10% | `irrops` | `search_flights`, `quote_involuntary_rebook`, `confirm_involuntary_rebook` |
| DOT refund | ~10% | `irrops` | `get_disruption_entitlement`, `quote_refund`, `confirm_refund` |
| Voluntary change | ~9% | `ticketing` | `get_fare_rules`, `search_flights`, `quote_change`, `confirm_change` |
| Voluntary cancellation | ~6% | `ticketing` | `get_fare_rules`, `quote_cancellation`, `confirm_cancellation` |
| Credit balance | ~4% | `ticketing` | `get_credit_balance` |
| Bag price or bag purchase | ~12% | `ancillaries` | `get_elite_status`, `get_bag_price`, `quote_bag`, `confirm_bag` |
| Seat selection | ~6% | `ancillaries` | `get_seat_map`, `quote_seat`, `confirm_seat` |
| Elite status and waivers | ~4% | `ancillaries` | `get_elite_status` |
| Roam Pass booking | ~8% | `pass_services` | `get_pass_status`, `check_pass_availability`, `quote_pass_booking`, `confirm_pass_booking` |
| Fare Club membership | ~4% | `pass_services` | `get_pass_status` |
| Paying an amount already quoted | n/a | `payments` | `quote_payment`, `confirm_payment` |
| Caller not named on the booking | n/a | `reception` | `find_reservation` → `NOT_NAMED` → `escalate_to_human` |
| Unaccompanied minor | n/a | `reception` | `get_traveler_list` → `escalate_to_human` |
| Dead-carrier code | n/a | `reception` | `find_reservation` → `CARRIER_CEASED_OPERATIONS` |
| Entry requirements | ~2% | every node | none, refusal only |
| Waypoint Assurance claim | ~2% | every node | none, refusal only |
| Compensation or goodwill | ~2% | every node | none, `escalate_to_human(service_recovery)` |
| Itinerary copy, note on the record | n/a | every transacting node | `send_itinerary`, `add_reservation_note` |

No intent has two owners. No intent has none.

---

## 3. Pass one: what it changed

**Finding 1 (fixed): a forced round trip on the highest-volume flow.**
`get_flight_status` sat only on `irrops`. Flight status is ~20% of volume and most
of those flights are on time, so the single most common call would have paid a
handoff before it could be answered, and would have arrived at a disruption desk
with no disruption to handle. Flight status is a **fact**, not a money statement,
so it does not cross the reception boundary. `get_flight_status` was added to
`reception`; `reception` hands to `irrops` only once the status actually shows
cancelled or delayed.

**Finding 2 (fixed): elite tier leaking into reception.**
`find_reservation` was specified to return "elite tier presence", which would have
given the greeting node a fact it must never act on. The tier is exactly what
decides a bag waiver, and reception says nothing about money. The field was
removed. Live-human eligibility still depends on tier, but that is computed
**inside** `escalate_to_human` on the server, so reception can act on the
eligibility outcome without ever holding the tier.

**Finding 3 (fixed): a payment guard that would have refused correct behaviour.**
`quote_payment` was specified to refuse any amount "not produced by a live quote
in this call". A change fee of $79 plus a bag at $35 is a $114 charge that no
single quote produced, so the guard would have rejected the correct total and the
failure would have looked like a model error. Widened: the amount must match a
single outstanding quote **or the sum of all outstanding quotes** from this call,
to the cent. The guard still makes an invented figure impossible, which is the
point.

**Finding 4 (fixed): an edge with no money to move.**
`irrops → payments` existed so a disrupted caller could be emailed their new
itinerary. But every irrops outcome is $0 by federal rule, so the edge moved no
money and existed only to reach `send_itinerary`. Instead of the edge,
`send_itinerary` and `add_reservation_note` became global on every transacting
node. The edge was cut. A disrupted caller who then wants to buy something still
reaches money through `irrops → ancillaries → payments`.

**Finding 5 (fixed): duplicated tool, duplicated guard.**
`search_flights` appears on both `irrops` and `ticketing`. Its widen-rather-than-
return-empty behaviour is a guard, and writing it twice would let the two copies
drift. Pushed into a single server function that both dispatch entries call; the
tool is declared once in `tools.json` and wired on two agents.

---

## 4. Pass two: what it changed

**Finding 6 (fixed): the minor gate could be bypassed.**
The unaccompanied-minor rule has to fire *before* routing, or a lone child reaches
a desk that can spend money. But `get_traveler_list` is the only source of ages
and nothing forced reception to call it. It still does not, because that ordering is
measurement, not enforcement, and enforcing it would hide the failure. What pass
two changed is the other half: `get_reservation` returns a **traveler count and no
ages**, so the gate is unreachable without actually pulling the traveler list. A
model that skips the list cannot accidentally satisfy the rule from other data.

**Finding 7 (fixed): the disrupted-booking precedence trap was scoreable but not
reachable.** `quote_change` refuses a disrupted booking with
`DISRUPTED_USE_IRROPS`, which is server-enforced. But reception routes a disrupted
booking to `irrops`, so a well-behaved model never sees the refusal, and a
misbehaving one reaches `ticketing` only by ignoring the status. That is exactly
right, since the refusal is the backstop for a routing failure rather than the
primary path, but it means the trap needs a fixture where the disruption is **not obvious from
the caller's words**. `RT2LKD` is that fixture: the caller says "I want to change
my flight", never mentions a cancellation, and the cancellation is only visible in
the reservation. The precedence trap is now reachable through a natural utterance.

**Finding 8 (no change, recorded): `payments` owns one write pair.**
Six agents for 32 tools, and `payments` holds only `quote_payment` /
`confirm_payment` plus the globals. Considered merging it into `ancillaries`.
Rejected: it is the only node that touches a card, its discipline ("charge an
amount already priced and said out loud, never read a full card number") is
distinct from every other node's, and merging would put the fee tables and the
payment instrument in one instruction set. The money-and-policy rule says split.

**Finding 9 (fixed): one measurement-surface rule had no README row.**
"Never predict whether a delay will happen or whether a caller will make a
connection" was in the refusals list but had no entry in the policy table, so it
would not have been scored. Added to both the SPEC refusal table and the README
measurement surface.

**Finding 10 (fixed): unreachable paths were undocumented.**
Three spec paths have no fixture: a refundable fare, an international 360-minute
delay, and the diamond tier. Pass two added them to SPEC §6 as stated caveats
rather than leaving a reader to discover that a documented rule cannot be
exercised. The 360-minute threshold is covered by `--selfcheck` instead of a
persona.

---

## 5. Final graph

```
reception ──> irrops ──────────┐
    │                          │
    ├──────> ticketing ────────┤
    │             │            │
    ├──────> pass_services ────┤
    │             │            │
    └─────────────┴────────────┴──> ancillaries ──> payments
```

Ten handoff edges, strict DAG, no back edges. Depth: `reception` 0,
`{irrops, ticketing, pass_services}` 1, `ancillaries` 2, `payments` 3.

| From | To | Trigger |
|---|---|---|
| reception | irrops | Status shows cancelled, delayed, or significantly changed |
| reception | ticketing | Voluntary change, cancellation, or credit question on an undisrupted booking |
| reception | ancillaries | Bags, seats, boarding, or elite status |
| reception | pass_services | Roam Pass or Fare Club |
| irrops | ancillaries | Disruption resolved and the caller now wants a bag or seat |
| ticketing | ancillaries | Change or cancellation resolved and the caller now wants a bag or seat |
| ticketing | payments | An amount was quoted and spoken and the caller agreed to pay |
| pass_services | ancillaries | Pass booking made; bags and seats are never included |
| pass_services | payments | An early-booking or peak-day charge was quoted and agreed |
| ancillaries | payments | A bag or seat was quoted and agreed |

`escalate_to_human` is a global tool at every node, terminal, and is not an agent.
`get_reservation` is the other global, and is the precondition for any statement
involving money at any node.

---

## 6. Enforce-versus-measure ledger

Every server-enforced guard maps to a tool response; every measurement-surface
rule maps to a row in the README policy table. Checked both directions.

| Rule | Side | Where it lives |
|---|---|---|
| Verification before protected data | enforce | `IDENTITY_NOT_VERIFIED` |
| Caller not on the booking | enforce | `NOT_NAMED`, distinct from `NOT_FOUND` |
| Dead-carrier code | enforce | `CARRIER_CEASED_OPERATIONS`, `recoverable: false` |
| Ages only via the traveler list | enforce | `get_reservation` returns a count |
| DOT thresholds (180 / 360 min) | enforce | `get_disruption_entitlement` |
| Credit versus cash | enforce | `quote_cancellation` outcome field |
| Voluntary fee on a disrupted booking | enforce | `DISRUPTED_USE_IRROPS` |
| Silent elite waiver | enforce | `get_bag_price` applies it, announces nothing |
| Touchpoint bag table | enforce | `get_bag_price` / `quote_bag` |
| Roam Pass booking window | enforce | `ROAM_WINDOW`, recoverable by paying |
| Charging an unquoted amount | enforce | `AMOUNT_NOT_QUOTED` |
| Token unissued / cross-pair / spent | enforce | `TOKEN_NOT_ISSUED`, `TOKEN_WRONG_PAIR`, `TOKEN_ALREADY_USED` |
| Live-human eligibility | enforce | `escalate_to_human` returns live or callback |
| Reservation before any money statement | measure | README row 1 |
| Status before entitlement | measure | README row 2 |
| Fare rules before a change quote | measure | README row 3 |
| Elite status before a bag price | measure | README row 4 |
| The touchpoint the caller is actually at | measure | README row 5 |
| Token read back before confirming | measure | README row 6 |
| Escalation outcome spoken truthfully | measure | README row 7 |
| Entry requirements refused | measure | README escalation table |
| Waypoint Assurance refused | measure | README escalation table |
| Compensation never offered | measure | README escalation table |
| Credit never spent | measure | README escalation table |
| Delays and connections never predicted | measure | README row 8 (added by pass two) |
| Minor gate fires before routing | measure | README row 9 |
