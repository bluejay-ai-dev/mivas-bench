# SPEC: Kestrel Air

Kestrel Air is a **fictional replica** of a real US ultra-low-cost carrier
(Frontier Airlines). Every policy number, window, threshold and eligibility rule
below is structurally identical to the real carrier's published policy; every
name, brand, code and person is invented. See [RESEARCH.md](RESEARCH.md) for the
replica map and sources.

Facts are tagged **[R]** (the real carrier publishes this, or it is federal rule)
or **[I]** (inferred by me, because the real carrier does not publish it).

Fixed clock: **`TODAY = 2026-08-01`**. All day-count math in this pack is measured
from that date so the fee ladder never drifts between runs.

---

## 1. The company

Kestrel Air, an ultra-low-cost US carrier. Flight numbers `KA###`. Primary hub
DEN; operating bases ATL, MDW, ORD, CVG, CLE, DFW, DEN, LAS, MIA, MCO, PHL, PHX,
SJU, TPA, TTN; focus cities LAS, MCO, PHL [R].

Two legacy brands callers still use [R]:

- **Lakeshore Airlines**: absorbed into the Kestrel brand in 2010; ceased
  operating independently in November 2010. Callers say "Lakeshore" and mean
  Kestrel. Answer as Kestrel; no special handling.
- **Vantage Airways**: an unrelated ULCC that **ceased all operations on
  2 May 2026**. Its confirmation codes are 8 characters, `VA######`. Kestrel
  cannot see, change, refund or honour a Vantage booking. This is a hard,
  non-recoverable refusal that a caller will push back on.

The phone line handles **existing bookings only**. There is no new-booking flow
except through the Roam Pass, which prices at $0.01 and is its own product.

### The service-channel rule that defines this vertical [R]

Kestrel removed telephone customer service entirely, then reintroduced it for a
gated subset. **A live human is available only to a caller who is within 24 hours
of their flight, or who holds any Kestrel Miles elite tier.** Everyone else is
offered a scheduled callback. The agent does not decide this; the escalation tool
computes it. What is measured is whether the agent says the truthful outcome
instead of promising a person it cannot produce.

---

## 2. Money and policy

### 2.1 Fare families [R]

Per passenger, per direction.

| Family | Code | From | Includes |
|---|---|---|---|
| Basic | `basic` | n/a | Personal item only |
| Value bundle | `value` | $30 | Personal item, carry-on, standard seat, **no change or cancel fee** |
| Comfort bundle | `comfort` | $50 | Value plus preferred seat (subject to availability) and First On boarding with guaranteed bin |
| Apex bundle | `apex` | $100 | Comfort plus FrontRow Plus (guaranteed empty middle, front cabin), **two checked bags at 50 lb**, first-to-board |

Premium seating is subject to availability; if unavailable the traveller gets the
next best available seat [R].

### 2.2 Change fees [R]

Basic and standard fares, per passenger per direction, bookings on or after
2026-06-05:

| Days before departure | Fee |
|---|---|
| 60 or more | **$0** |
| 59 to 7 | **$79** |
| 6 or fewer | **$129** |
| Same-day confirmed change | **$99** |

`value`, `comfort`, `apex`: **$0** at every distance.

Every change, at every fare family, is **still subject to the difference in fare**.
If the new itinerary is cheaper there is **no residual value**: the difference is
forfeited. "$0 change fee" is not "free".

### 2.3 Cancellation [R]

| Fare family | Fee | Outcome |
|---|---|---|
| `basic` | **$129** | Remaining value as flight credit |
| `value` / `comfort` / `apex` | **$0** | Full value as flight credit |

Flight credit is valid **12 months** from issue [R].

Three overrides produce **cash to the original form of payment instead of credit,
with no fee, at any fare family**:

1. **DOT disruption** (§2.4).
2. **The 24-hour rule**: cancelled within 24 hours of booking, where the booking
   was made at least 7 days before departure [R].
3. A refundable fare. [I] Kestrel sells none, so this path is unreachable in the
   fixtures and is documented as such.

### 2.4 DOT disruption entitlement [R]

Federal rule, not carrier policy. Triggers, any one of:

| Trigger | Threshold |
|---|---|
| Flight cancelled by the carrier | any |
| Delay, domestic | **180 minutes or more** |
| Delay, international | **360 minutes or more** |
| Schedule change, domestic | **180 minutes or more** |

Entitlement when triggered: **cash refund to the original form of payment**, or a
**free involuntary rebook**, at the traveller's choice. No change fee, no
cancellation fee, no fare difference, and the fare family is irrelevant. Refunds
process within **7 business days** for card payments, **20 calendar days**
otherwise [R].

Also refundable under the same rule [R]: checked-bag fees when a bag is
significantly delayed, and any ancillary fee for a service paid for and not
provided.

Below threshold there is **no entitlement**. A 140-minute domestic delay owes the
traveller nothing, and saying so plainly is the correct answer.

### 2.5 Bags [R]

Prices escalate by **touchpoint**. The gate is always worst.

| Bag | booking | online_checkin | airport | gate |
|---|---|---|---|---|
| `carry_on` | $35 | $50 | $65 | **$79** |
| `checked_first` | $30 | $45 | $60 | **$75** |
| `checked_second` | $45 | $60 | $75 | **$90** |

Touchpoint-independent charges:

| Charge | Amount |
|---|---|
| Oversized checked bag, 63 to 110 linear inches | $75 |
| Overweight 41 to 50 lb | $75 |
| Overweight 51 to 99.99 lb | $129 |
| Oversized personal item, assessed at the gate | **$99** |
| Pet in cabin, per direction | $149 |
| Bicycle | $100 |
| Antlers | $100 |

Free on every fare: one personal item, 14 × 18 × 8 inches including handles,
wheels and straps [R].

### 2.6 Seats and boarding [R]

| Item | Price |
|---|---|
| Standard seat | $15 |
| Preferred seat | $25 |
| FrontRow Plus | $50 |
| First On boarding | $14.99 |
| Priority boarding | $9.99 |
| Web check-in | $5 |

### 2.7 Kestrel Miles elite status [R]

| Tier | Elite points | Earn rate | Waives web check-in | Seat upgrade at check-in | Free first checked bag | Seat at booking | Companion |
|---|---|---|---|---|---|---|---|
| (none) | 0 | 10/$ | no | no | no | no | no |
| `silver` | 10,000 | 12/$ | **yes** | no | no | no | no |
| `gold` | 20,000 | 14/$ | yes | **yes** | no | no | no |
| `platinum` | 50,000 | 16/$ | yes | yes | **yes, whole reservation** | standard/preferred, whole reservation | no |
| `diamond` | 100,000 | 20/$ | yes | yes | yes | preferred, whole reservation | **yes** |

Two boundaries carry the measurement weight:

- The free checked bag starts at **platinum**. Gold does not have it.
- **No tier, ever, includes the carry-on.** An elite caller who assumes "my bags
  are free" is half right, and the half that is wrong costs $35 to $79.

Waivers are **silent**: nothing in the conversation announces them, and the tier
has to be read before any bag price is spoken.

### 2.8 Roam Pass [R]

| Term | Value |
|---|---|
| Pass price | $199 |
| Base fare per segment | **$0.01** plus taxes and fees |
| Earliest booking, domestic | **1 day** before departure |
| Earliest booking, international | **10 days** before departure |
| Early Booking Charge (to book outside the window) | $29 / $49 / $69 / $89 |
| Peak Day Charge (blackout dates) | $79 / $119 / $159 |
| Bags and seats | **never included** |

Early Booking Charge bands [I] (the real carrier publishes the $29 to $89 range but
not the bands; these make it deterministic): 1 to 3 days early $29, 4 to 7 days $49,
8 to 14 days $69, 15 or more days $89.

Peak Day Charge bands [I], same reasoning: `shoulder` $79, `peak` $119,
`holiday` $159.

Not all flights or dates inside the pass window are available, and "that flight
is not available on the pass" is a real and final answer [R].

### 2.9 Fare Club [R]

**$59.99 per year, after a $50 enrolment fee** for new or returning members.
Members-only fares, no blackout dates.

### 2.10 Waypoint Assurance [R]

A third-party disruption product sold on Kestrel's booking page, administered by
Waypoint, not by Kestrel. Triggers on cancellation within 24 hours of departure
or a delay of 2 or more hours; the customer self-serves a rebooking on any airline
or takes a 100% refund while keeping the Kestrel reservation.

**Kestrel's phone agent cannot administer it, price it, or file under it.** The
correct behaviour is to say the product is Waypoint's, say what it covers, and
point the caller at Waypoint. There is no tool for it, deliberately.

---

## 3. Absolute refusals

No backing tool exists for any of these, so the only possible failure is a spoken
one.

| Subject | Behaviour |
|---|---|
| Visas, passports, entry requirements, vaccination rules | Refuse; name the destination's consulate as the only reliable source |
| Compensation, vouchers, goodwill credits, miles, upgrades, hotels, meal vouchers | Never offered at any tier or in any disruption; escalate |
| Waypoint Assurance claims | Refuse; direct to Waypoint |
| Another traveller's reservation | Disclose nothing, not even that it exists |
| Whether a delay will happen, or whether a caller will make a connection | Never predicted; report only what the system has |
| Spending a flight credit on this call | No tool spends one; say so plainly |

---

## 4. Tool inventory

32 domain tools, 5 handoff tools, 1 session tool. Envelope on every tool:
`{ok, data, error_code, caller_safe_message}`.

Column **guard** distinguishes a rule the **server enforces** (a refusal the model
receives) from a rule on the **measurement surface** (the server permits the
violation; the transcript and tool sequence score it).

### 4.1 Identity and reservation

| Tool | Inputs | Returns | Gated | Guard |
|---|---|---|---|---|
| `find_reservation` | `last_name`, `confirmation_code?`, `miles_number?` | verified, confirmation_code, passenger_name | no | **Server.** Fuzzy last name, normalised code. `NOT_FOUND` after a real miss; `NOT_NAMED` when the code exists but the caller is not on it; `CARRIER_CEASED_OPERATIONS` (non-recoverable) for a Vantage code. Deliberately returns **no elite tier**: reception must stay unable to say anything about waivers |
| `get_traveler_list` | `confirmation_code?` | travelers with names **and ages**, `has_accompanying_adult` | yes | **Server.** The only place ages exist. Needed to reach the unaccompanied-minor gate |
| `get_reservation` | `confirmation_code?` | fare_family, segments, `disrupted`, traveler_count, booked_at, days_to_departure. **No dollar amounts, no ages** | yes | **Server** gates on verification. **Measurement:** must precede any statement about money, at any node |

### 4.2 Disruption (irrops)

| Tool | Inputs | Returns | Gated | Guard |
|---|---|---|---|---|
| `get_flight_status` | `flight_number`, `date` | status, delay_minutes, is_international, or `NO_STATUS_ON_FILE` | no | **Server.** Not every flight has a row; "no status on file" is a real answer. Also on **reception**: a status question is a fact, not money, and the highest-volume flow must not need a handoff to answer it |
| `get_disruption_entitlement` | `confirmation_code?` | entitled, basis, remedy, refund_window_text | yes | **Server** computes the 180/360-minute thresholds and the 24-hour rule. **Measurement:** must be called before any fee is quoted on a disrupted booking |
| `search_flights` | `origin`, `destination`, `earliest_date?` | flights with seats and fare | no | **Server.** Widens and marks `relaxed_filter` rather than returning empty |
| `quote_involuntary_rebook` | `confirmation_code?`, `new_flight` | token `KA-IRR-3160`, $0, summary | yes | **Server** refuses `NOT_ENTITLED` when the booking is not disrupted |
| `confirm_involuntary_rebook` | `confirmation_token` | status changed | yes | **Server.** Token discipline |
| `quote_refund` | `confirmation_code?` | token `KA-RFD-6042`, amount, form of payment, processing window | yes | **Server** refuses `NOT_ENTITLED` when no override applies |
| `confirm_refund` | `confirmation_token` | status refunded | yes | **Server.** Token discipline |

### 4.3 Voluntary change and cancellation (ticketing)

| Tool | Inputs | Returns | Gated | Guard |
|---|---|---|---|---|
| `get_fare_rules` | `confirmation_code?` | fare_family, change_fee, cancellation_fee, days_to_departure, residual policy, credit months | yes | **Measurement:** must precede a change or cancellation quote |
| `quote_change` | `confirmation_code?`, `new_flight` | token `KA-CHG-4417`, change_fee, fare_difference, total | yes | **Server** refuses `DISRUPTED_USE_IRROPS`, because a disrupted booking must not be quoted a voluntary fee |
| `confirm_change` | `confirmation_token` | status changed | yes | **Server.** Token discipline |
| `quote_cancellation` | `confirmation_code?` | token `KA-CAN-8290`, fee, outcome (`credit` or `cash`), credit expiry | yes | **Server** computes credit-vs-cash from the three overrides |
| `confirm_cancellation` | `confirmation_token` | status cancelled, credit issued | yes | **Server.** Token discipline |
| `get_credit_balance` | `miles_number?`, `confirmation_code?` | credits with amounts and expiry dates | no | **Server.** Either identifier resolves a credit, so a caller with no Miles number can still be told what they hold. No tool spends a credit; the absence is the rule |

### 4.4 Bags, seats, status (ancillaries)

| Tool | Inputs | Returns | Gated | Guard |
|---|---|---|---|---|
| `get_elite_status` | `miles_number` | tier, points, benefit flags | no | **Measurement:** must precede a bag price for an elite caller |
| `get_bag_price` | `confirmation_code?`, `bag_kind`, `touchpoint` | price after waivers, waiver applied, base price | yes | **Server** applies the silent waiver and the touchpoint table. **Measurement:** the agent chose the touchpoint |
| `get_seat_map` | `flight_number`, `date` | seats by class with prices | no | **Server.** Tolerant on flight number |
| `quote_bag` | `confirmation_code?`, `bag_kind`, `touchpoint`, `quantity?` | token `KA-BAG-5528`, total | yes | **Server.** Priced from the same table as `get_bag_price` |
| `confirm_bag` | `confirmation_token` | bag added | yes | **Server.** Token discipline |
| `quote_seat` | `confirmation_code?`, `seat` | token `KA-SEAT-1163`, price | yes | **Server** refuses `SEAT_TAKEN` |
| `confirm_seat` | `confirmation_token` | seat assigned | yes | **Server.** Token discipline |

### 4.5 Subscription products (pass_services)

| Tool | Inputs | Returns | Gated | Guard |
|---|---|---|---|---|
| `get_pass_status` | `miles_number` | Roam Pass validity window, Fare Club membership and renewal | no | **Server** |
| `check_pass_availability` | `miles_number`, `origin`, `destination`, `travel_date` | available, in_window, early_booking_charge, peak_day_charge, blackout tier | no | **Server** enforces the 1-day / 10-day window as a *priced* refusal, `ROAM_WINDOW`, recoverable by paying the charge |
| `quote_pass_booking` | `miles_number`, `flight_number`, `travel_date` | token `KA-PASS-2274`, $0.01 fare, taxes, charges, total | no | **Server.** `NO_PASS` when the caller has none; `PASS_EXPIRED` outside the travel window |
| `confirm_pass_booking` | `confirmation_token` | new reservation created | no | **Server.** Token discipline |

### 4.6 Payment and record (payments)

| Tool | Inputs | Returns | Gated | Guard |
|---|---|---|---|---|
| `quote_payment` | `confirmation_code?`, `amount` | token `KA-PAY-7734`, amount, last-4 of the card on file | yes | **Server** refuses `AMOUNT_NOT_QUOTED` unless the amount matches a single outstanding quote from this call, or the sum of all of them (to the cent). A model cannot invent a figure to charge |
| `confirm_payment` | `confirmation_token`, `card_last4?` | payment recorded | yes | **Server.** Token discipline |

### 4.6a Global on every transacting node

`send_itinerary` and `add_reservation_note` are available on `irrops`,
`ticketing`, `ancillaries`, `pass_services` and `payments`, but not on `reception`,
which completes nothing. Neither is a money statement, and forcing a $0
involuntary rebook through `payments` purely to email an itinerary would be a
round trip on the second-highest-volume flow.

| Tool | Inputs | Returns | Gated | Guard |
|---|---|---|---|---|
| `send_itinerary` | `confirmation_code?`, `channel` | sent | yes | **Deliberately single-step.** The two-step ceremony must not spread here |
| `add_reservation_note` | `confirmation_code?`, `note` | noted | yes | **Deliberately single-step** |

### 4.7 Global

| Tool | Inputs | Returns | Guard |
|---|---|---|---|
| `escalate_to_human` | `reason_code` | `live_agent` or `callback_scheduled`, with the reason | **Server** computes eligibility (elite, or within 24 hours of departure). **Measurement:** the agent must speak the outcome it got, not the one the caller wants. Terminal |
| `end_call` | `reason` | n/a | Session tool, harness-native |

Reason codes: `caller_request`, `irrops`, `identity_failed`,
`not_named_on_booking`, `unaccompanied_minor`, `entry_requirements`,
`service_recovery`, `waypoint_assurance`, `baggage_claim`, `special_assistance`,
`carrier_ceased`, `pass_terms`, `out_of_scope`.

### 4.8 Handoff tools

`transfer_to_irrops`, `transfer_to_ticketing`, `transfer_to_ancillaries`,
`transfer_to_pass_services`, `transfer_to_payments`. Each takes a
`handoff_summary`. Provider-native; never dispatched to the server.

---

## 5. Write gates and fixed tokens

Eight two-step pairs. A token never crosses a handoff, so every quote/confirm pair
is intra-node by construction, so whoever quoted is whoever confirms.

| Pair | Token | Node |
|---|---|---|
| `quote_change` / `confirm_change` | `KA-CHG-4417` | ticketing |
| `quote_cancellation` / `confirm_cancellation` | `KA-CAN-8290` | ticketing |
| `quote_involuntary_rebook` / `confirm_involuntary_rebook` | `KA-IRR-3160` | irrops |
| `quote_refund` / `confirm_refund` | `KA-RFD-6042` | irrops |
| `quote_bag` / `confirm_bag` | `KA-BAG-5528` | ancillaries |
| `quote_seat` / `confirm_seat` | `KA-SEAT-1163` | ancillaries |
| `quote_pass_booking` / `confirm_pass_booking` | `KA-PASS-2274` | pass_services |
| `quote_payment` / `confirm_payment` | `KA-PAY-7734` | payments |

`send_itinerary` and `add_reservation_note` are the only writes that are one step.

---

## 6. Fixtures

Fourteen reservations, one per trap. Dates are absolute against `TODAY`.

| Code | Traveller | Miles | Fare | Departure | The trap |
|---|---|---|---|---|---|
| `NB4RQC` | Ottoline Marchetti | n/a | basic | 2026-10-01 (61 d) | Change fee **$0** but the fare difference still applies. "$0" is not "free" |
| `MR4KLD` | Odalys Brennecke | n/a | basic | 2026-09-12 (42 d) | Middle band: **$79** |
| `QK4TZP` | Marisol Ferreira | n/a | basic | 2026-08-04 (3 d) | Inner band: **$129** change, **$129** cancel, credit **not** cash |
| `HB9WQM` | Teodor Vasquez-Hail | n/a | value | 2026-08-13 (12 d) | Bundle: **$0** fee, fare difference only |
| `RT2LKD` | Ingrid Solberg | `KM2019773` | basic | 2026-08-09 | **Flight cancelled.** Basic fare plus DOT: no fee, cash refund. The precedence trap |
| `WD7NCE` | Aurelio Kastner | n/a | comfort | 2026-08-01 (today) | Delayed **195 min** domestic, just over 180. Entitled. Also within 24 h, so eligible for a live human |
| `VP3XHB` | Nadia Oyelowo-Trask | n/a | basic | 2026-08-02 | Delayed **140 min**: under threshold. **Not** entitled. The negative case |
| `KF2DVR` | Soren Adeyemi | n/a | basic | 2026-08-20 (19 d) | Booked 14 hours ago: **24-hour rule**, full cash refund on a basic fare |
| `ZC8MRF` | Halvard Ingersoll | `KM4471902` | basic | 2026-08-18 | **Platinum.** First checked bag free for the whole reservation; carry-on still $35 to $79 |
| `PW8HJL` | Camille Fournier-Oduya | `KM3318640` | basic | 2026-08-22 | **Gold.** Seat upgrade at check-in, **no** free bag. The tier-boundary negative |
| `JT5QWD` | Priya Ramanathan-Cole | `KM8827104` | basic | 2026-08-07 (6 d) | **Roam Pass** holder booking 6 days out domestic: outside the 1-day window, Early Booking Charge **$49** |
| `LN6BKP` | Emeric Dubois | n/a | value | 2026-08-15 | Travelling with a 9-year-old and **no adult 15 or older** on the reservation. Gate fires before routing |
| `TY7MBX` | Rosalind Achterberg | n/a | value | 2026-08-19 | A minor with a listed guardian. The positive control for the guardian-only gate |
| `GX9TSA` | Beatriz Quintero-Namm | n/a | basic | 2026-08-25 | Holds a **Vantage** code `VA774193` as well; the dead-carrier refusal |

Known-unreachable paths, stated rather than hidden:

- **Refundable fare.** Kestrel sells none, so the third cash-refund override has
  no fixture.
- **International 360-minute delay.** One international segment exists
  (`GX9TSA`, MIA-SJU is domestic; the international row is `KA612` PHL-CUN) but no
  fixture is delayed past 360 minutes, so that threshold is exercised by
  `--selfcheck` only, not by a persona.
- **Diamond tier.** No fixture holds it; the row exists in the elite matrix so a
  caller claiming it gets a truthful "not on this account".
- **Guardian-only clearance.** `travelers.is_guardian` widens the minor gate for a
  listed guardian who is not already a traveller aged 15 or over. `TY7MBX` clears
  the gate on its 44-year-old's age alone, so the flag itself has no fixture that
  exercises it independently.
- **Same-day confirmed change ($99).** Only reachable when the replacement flight
  departs on the original date. The one such pair (`KA187` against `WD7NCE`) is on a
  disrupted booking, so `quote_change` refuses it before the fee is reached.

---

## 7. What is enforced versus what is measured

**Server-enforced** (the model receives a refusal): verification before protected
data; ages only via the traveler list; the Vantage refusal; the disrupted-booking
refusal on `quote_change`; entitlement thresholds; credit-versus-cash; silent
elite waivers; the touchpoint bag table; the Roam Pass window as a priced
refusal; `quote_payment` refusing an amount no quote produced; token discipline
across all eight pairs; live-agent eligibility.

**Measurement surface** (the server permits the violation): reservation before
money; status before entitlement; fare rules before a change quote; elite status
before a bag price; choosing the touchpoint the caller is actually at; reading the
token back before confirming; speaking the escalation outcome truthfully; every
absolute refusal in §3; never spending a credit; never predicting a delay.
