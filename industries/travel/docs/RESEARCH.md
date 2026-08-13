# RESEARCH — airline voice AI, candidate carriers, and the Kestrel Air replica

Facts are tagged **[R]** (sourced, URL kept) or **[I]** (inferred by me). The tags
carry into `SPEC.md` and the README's honesty section.

Research window: August 2026.

---

## 1. The industry deep dive

### What airlines do with conversational AI today

Airline phone volume is the most disruption-shaped workload in consumer services:
baseline demand is flat and predictable, then a weather system or a technical
ground stop multiplies it inside an hour. That shape is why airlines were early
and why the automation boundary sits where it does.

- **Irregular operations (IROPS) is the flagship use case.** When weather or a
  technical issue disrupts a schedule, hundreds of flights and thousands of
  passengers are affected within hours, and call volume spikes faster than any
  human team can staff for [R]. Vendors position an agent that answers every call
  at once, states the options, and rebooks the straightforward cases immediately
  [R]. Rebooking logic is described as fare-rule-, availability-, loyalty-status-
  and preference-aware, executing itinerary changes inside stated policy
  guardrails [R].
- **Deployment sequencing is consistent across vendors:** start with high-volume,
  low-complexity intents (flight status, basic rebooking), then expand to group
  bookings and full IROPS management [R].
- **Production, not pilot, at real scale.** Lufthansa Group's Cognigy deployment
  handles up to 375,000 interactions on peak days and roughly 16 million
  conversations a year across 16+ agents and multiple channels and languages,
  including rebooking, alternative flight options, and refund processing [R].
- **What stays human.** Nothing in the vendor corpus claims automation of
  compensation and goodwill decisions, entry-requirement advice, or
  special-assistance and medical cases. [I] The pattern across the marketing
  material is that anything discretionary (a voucher, a waiver granted as
  goodwill) and anything with legal exposure outside the airline's own tariff
  (visas, passports, immigration) is routed to staff. This is the shape the
  measurement surface takes in this pack.

### Who is prominent in airline voice AI

Ranked three ways.

**Highest deployment value (revenue at stake per call).** Legacy and hybrid
carriers where a single call can carry a multi-thousand-dollar international
itinerary: Lufthansa Group (Cognigy) [R], and carriers running Twilio Flex-based
contact centres such as Philippine Airlines, which reported contact-centre wait
times under a minute and roughly 30% lower monthly service cost, targeting a
state where 80% of live-agent tasks are automated by April 2026 [R].

**Highest call volume.** Lufthansa Group, at 375,000 peak-day interactions [R].
Ultra-low-cost carriers are the other volume pole: their per-passenger ancillary
model generates a call about money on nearly every booking, and Frontier grew
15–30% annually while running customer service through an AI agent instead of a
call centre [R].

**Broadest adoption / named vendors.** Cognigy (acquired by NICE in September
2025) names Lufthansa Group and Frontier Airlines among its airline customers
[R]. Other vendors publishing airline-specific voice or agentic offerings:
ASAPP, Rasa, Ada, Kaiban, Retell AI, Telnyx [R]. Twilio Flex is the platform of
record at Philippine Airlines [R].

### Common use cases and rough volume shares

No airline publishes its intent mix, so the shares below are **[I]**, inferred
from the ordering and emphasis of the vendor corpus (which intents are named
first, which get their own product pages) and from the public review corpora
below. They set fixture priorities, nothing more.

| Intent | Share [I] | Note |
|---|---|---|
| Flight status / "where is my flight" | ~20% | Named as the first automation target everywhere [R] |
| Disruption rebooking and refunds (IROPS) | ~20% | Spiky: near zero on a clear day, dominant on a bad one [R] |
| Bags and seats (buy, price, dispute a fee) | ~18% | Dominant at ULCCs, where nothing is bundled [R] |
| Voluntary change / cancel | ~15% | Fee ladder questions, credit questions |
| Booking and fare questions | ~12% | Including subscription-fare products |
| Loyalty / status / credit balance | ~8% | "I'm elite, why was I charged" |
| Everything else (pets, minors, special assistance, entry rules) | ~7% | Mostly refuse-and-route |

### Regulatory and policy constraints that shape call handling

These are collected as **rules with consequences**, because they become the
measurement surface.

1. **DOT automatic refund rule** (final rule April 2024, compliance 28 October
   2024). A cancelled flight, or a **significant delay of 3+ hours domestic /
   6+ hours international**, entitles the passenger to a **cash refund to the
   original form of payment** — automatically, without asking, regardless of how
   restrictive the fare is. Refunds must be processed within **7 business days**
   for card payments, **20 calendar days** otherwise. The rule also requires
   prompt refund of checked-bag fees for significantly delayed bags and of
   ancillary fees for services paid for but not provided [R].
   *Consequence:* the fee ladder must not be applied to a disrupted booking. An
   agent that quotes a change fee on a cancelled flight is wrong, and the error
   is visible in the transcript.
2. **The 24-hour rule.** A reservation cancelled within 24 hours of booking, made
   at least 7 days before departure, is fully refundable [R].
   *Consequence:* a second path to cash on a non-refundable fare, which collides
   with rule 1 and with the fare ladder. Order of checks decides the answer.
3. **Entry requirements are not the airline's to advise on.** [I] No vendor
   material claims it; carriers uniformly point at the destination's consulate.
   *Consequence:* an absolute refusal with no backing tool.
4. **Ancillary fee disclosure.** ULCC fee structures are priced by touchpoint —
   the same bag costs more at online check-in than at booking, and most at the
   gate [R]. A carry-on priced $35 at booking is $79 at the gate [R].
   *Consequence:* quoting "the" bag fee without establishing which touchpoint the
   caller is at is a wrong answer that sounds right.
5. **Third-party disruption products are not the airline's to administer.**
   Frontier's "Disruption Assistance for Any Reason" is provided by HTS, a
   division of Hopper [R].
   *Consequence:* a refusal even though the product is sold on the airline's own
   booking page.

---

## 2. Three companies using voice AI today

**Lufthansa Group.** Cognigy customer [R]. 16+ AI agents across channels and
languages; up to 375,000 interactions on a peak day and ~16 million a year;
handles rebooking, travel information, alternative flight options, and refund
processing [R]. Evidence: vendor case study naming the customer, plus Lufthansa's
own co-marketing [R]. Nothing in the material claims automated compensation
decisions.

**Philippine Airlines.** Twilio Flex contact-centre deployment handling routine
tasks including flight status; average wait times fell to under a minute and
monthly customer service costs fell ~30%; publicly stated goal of a "super AI
agent state" by April 2026 in which 80% of live-agent-handled tasks are automated
[R]. Evidence: press coverage with named metrics [R].

**Frontier Airlines.** The strongest evidence of all three, because the airline
restructured its whole service channel around the AI agent rather than bolting it
onto a call centre. Frontier **eliminated telephone customer service in November
2022** and moved to 24/7 chat, social, and WhatsApp fronted by a Cognigy-built
agent [R]. The Cognigy case study describes a bot handling hundreds of thousands
of concurrent conversations, a substantial NPS increase, and a more cost-efficient
contact centre while the airline grew 15–30% annually [R]. In 2025–26, under a
programme branded "The New Frontier," it **reintroduced live phone support, but
only for customers within 24 hours of their flight or holding Elite status**, with
a callback service for everyone else [R]. What it refuses: the public complaint
corpus is full of chatbot dead ends on lost baggage, one couple receiving repeated
"Give us 24 hours and we'll let you know" replies [R].

---

## 3. Choose one: Frontier Airlines

Against the three criteria, in order:

1. **Largest public surface.** Every fee is published, because at a ULCC the fee
   table *is* the product: a full optional-services schedule with dated
   before/after amounts, a bag price checker, four fare bundles with itemised
   inclusions, a published elite matrix, a subscription fare club, and a
   subscription flight pass with its own terms [R]. There is also a large and
   specific review and litigation corpus [R]. Lufthansa is larger as a business
   but its fee and waiver rules are less legible from outside; Philippine
   Airlines has the thinnest published policy surface of the three.
2. **Richest call taxonomy.** Because nothing is bundled into a basic fare, a
   single caller can plausibly want a change quote, a bag price, a seat, a
   status-based waiver, a subscription-pass booking, and a payment — six distinct
   money conversations on one reservation. A legacy carrier collapses several of
   those into "it's included."
3. **Most testable money-and-policy rules.** This is decisive. The fee ladder is
   dated and numeric ($0 / $79 / $129 by days-out, per direction, per passenger),
   zeroed by three separate overrides (bundle, DOT disruption, 24-hour rule) that
   can collide on one booking. Bag prices escalate by touchpoint. Elite waivers
   apply to *some* bags and not others. The flight pass has a hard booking window
   with a priced exception. Every one of those is an assertion; none of it is
   culture.

**Bonus, and it is a real one.** Frontier is on both sides of an unusually rich
legacy-brand situation: Midwest Airlines was absorbed into the Frontier brand
under Republic Airways, ceasing independent operations in November 2010 [R], and
in May 2026 Spirit Airlines — the largest US ULCC, and a carrier Frontier had
twice tried to merge with — **ceased operations entirely on 2 May 2026**, with
customers told to seek refunds and rebook elsewhere and Frontier running
promotions aimed at them [R]. Callers holding a dead carrier's confirmation code
and expecting that carrier's rules is a permanent, testable behaviour, and it is
the single most realistic "the caller is wrong about which airline they are
talking to" case available in this industry right now.

---

## 4. The company treatment

The full sweep on Frontier. Everything here is structural and survives into the
replica; only the names change.

### Money and policy

**Fare families**, per passenger per direction [R]:

| Family | From | Includes |
|---|---|---|
| Basic | — | Personal item only. Carry-on, seat, checked bags, priority boarding all sold separately |
| Economy bundle | $30 | Personal item, carry-on, standard seat assignment, **no change/cancel fee** |
| Premium bundle | $50 | Above plus premium seat (subject to availability) and first-on boarding with guaranteed overhead bin |
| Business bundle | $100 | Above plus front-cabin seating with guaranteed empty middle, **two checked bags at a 50 lb allowance**, first-to-board |

Premium/front seating is "subject to availability"; if unavailable the customer
gets the next best available seat [R].

**Change fees**, basic/standard fare, per passenger per direction, bookings on or
after 5 June 2026 [R]:

| Days before departure | Fee |
|---|---|
| 60 or more | $0 |
| 59 to 7 | $79 |
| 6 or fewer | $129 |
| Same-day confirmed change | $99 |

Bundled fares: **$0** [R]. All changes remain subject to any difference in fare
and options prices, and **if the new itinerary is cheaper there is no residual
value** [R]. (Bookings before 5 June 2026 used $49 / $99 — a dated schedule
change, which is itself the kind of thing callers get wrong.)

**Cancellation fee**, basic fare: **$129** for bookings on or after 5 June 2026
(was $99 before). Bundled fares: **$0**. Value is retained as travel credit [R].

**Flight credit** validity: extended from three to **twelve months** [R].

**Bags** [R]. Priced dynamically by route and, critically, by **touchpoint** —
lowest at booking, highest at the gate. A carry-on at $35 at booking is $79 at the
gate; carry-on ranges roughly $60–$99 and checked bags roughly $30–$99 depending
on route and timing. Fixed penalties: oversized checked bag (63–110 linear inches)
**$75**; overweight 41–50 lb **$75**; overweight 51–99.99 lb **$129** for bookings
on or after 4 April 2026; bicycle **$100** on or after 29 May 2026; antlers
**$100**; pet **$149** per direction. Only a personal item of 14 × 18 × 8 inches
(including handles, wheels and straps) is free on every fare.

**Seats and boarding** [R]: seat selection from **$15** per passenger per segment;
priority boarding $0.99–$9.99; first-on boarding $2.99–$14.99; web check-in up to
$5.

**Elite status** — Frontier Miles, four tiers on elite status points [R]:

| Tier | Points | Earn rate | Key benefits |
|---|---|---|---|
| Silver | 10,000 | 12/$ | Entry benefits |
| Gold | 20,000 | 14/$ | Complimentary seat upgrades |
| Platinum | 50,000 | 16/$ | **Free first checked bag for the member and everyone on the reservation**; standard/preferred seat at booking for everyone on the reservation; premium seat at booking for the member |
| Diamond | 100,000 | 20/$ | Above plus free unlimited companion travel |

Base members earn 10 points per dollar [R]. Note the boundary that matters: the
free checked bag starts at **Platinum**, and **no tier includes the carry-on**.

**Subscription products** [R]:
- *Flight pass* ("GoWild"): $199 introductory for a multi-month unlimited-travel
  window; base fare **$0.01** plus taxes and fees; bookable no earlier than
  **1 day** before a domestic departure and **10 days** before an international
  one; booking outside that window costs an **Early Booking Charge of $29–$89**;
  travel on a blackout date costs a **Peak Day Charge of $79–$159**; not all
  flights or dates in the window are available; bags and seats are never included.
- *Fare club* ("Discount Den"): **$59.99/year after a $50 enrolment fee** for new
  or returning members; members-only fares with no blackout dates.

**Third-party disruption product** [R]: "Disruption Assistance for Any Reason,"
provided by HTS, a division of Hopper, sold at booking. Triggers on cancellation
within 24 hours of departure or a delay of 2+ hours; the customer self-serves a
rebooking on **any** airline or takes a 100% refund while keeping the original
reservation. Administered by the vendor, not the airline.

### Footprint

Primary hub Denver; operating bases at Atlanta, Chicago–Midway, Chicago–O'Hare,
Cincinnati, Cleveland, Dallas/Fort Worth, Denver, Las Vegas, Miami, Orlando,
Philadelphia, Phoenix–Sky Harbor, San Juan, Tampa and Trenton; focus cities Las
Vegas, Orlando and Philadelphia; fleet around 174–211 aircraft [R].

### People and credentials that route work differently

[I] A ULCC phone operation has no equivalent of legal's bar-admission map. The
routing credential is on the **caller** side, and it is unusually explicit here:
live phone support exists only for callers **within 24 hours of their flight or
holding Elite status**; everyone else is offered a callback [R]. That is the
credential-to-routing map for this vertical, and it is checkable.

### Call taxonomy, complaints, constraints

Complaints, Frontier-specific [R]: gate agents flagging compliant personal items
as oversized and charging **$99** on the spot, the subject of a proposed class
action, with reporting that gate staff received a **$10 bonus per bag charged**;
chatbot loops on lost baggage; confusion over which flights and dates a flight
pass can actually reach.

Constraints with clocks: the DOT 3-hour/6-hour significant-delay thresholds and
7-business-day/20-calendar-day refund windows; the 24-hour-after-booking refund
window (with its 7-days-before-departure precondition); the 60/59–7/6 day fee
boundaries; the 1-day and 10-day pass booking windows; 12-month credit expiry.

### Acquisition and legacy-brand history

Republic Airways acquired both Frontier and Midwest in 2009 and chose the
Frontier brand; Midwest ceased operating independently in November 2010 [R].
Spirit Airlines ceased all operations on 2 May 2026 after two bankruptcies, a
failed federal bailout, and failed mergers with both JetBlue and Frontier;
customers were issued refunds and told to rebook with other carriers, and Frontier
courted them with promotions [R].

---

## 5. Replica construction

Nothing of Frontier survives into the folder. Every **structural** fact above —
every fee, window, threshold, tier boundary and eligibility rule — is preserved
exactly.

### The replica map

| Real | Replica |
|---|---|
| Frontier Airlines | **Kestrel Air**, flight numbers `MD###` |
| Frontier Miles | **Kestrel Miles**, tiers silver / gold / platinum / diamond |
| GoWild All-You-Can-Fly Pass | **Roam Pass** |
| Discount Den | **Fare Club** |
| Economy / Premium / Business bundles | **value** / **comfort** / **apex** bundles |
| UpFront Plus seating | **FrontRow Plus** |
| Board First | **First On** |
| Disruption Assistance for Any Reason (HTS / Hopper) | **Waypoint Assurance** (third-party vendor, unchanged in role) |
| Midwest Airlines (absorbed 2010) | **Lakeshore Airlines** (absorbed 2010) |
| Spirit Airlines (ceased 2 May 2026) | **Vantage Airways** (ceased 2 May 2026), 8-character codes `VA######` |
| Denver hub, Las Vegas / Orlando / Philadelphia focus cities | Unchanged — IATA airport codes are public infrastructure, not carrier identity |

Structurally identical and deliberately unchanged: the four-family bundle ladder
and its inclusions; $0 / $79 / $129 / $99 change fees at the 60 / 59–7 / 6 / same-day
boundaries; $129 basic cancellation and $0 bundled; 12-month credit; touchpoint bag
escalation with the gate worst; $75 oversize, $75 and $129 overweight bands, $149
pet, $99 gate personal-item charge; $15 seat floor; the 10k/20k/50k/100k tier
points with the free checked bag starting at platinum and the carry-on never
included; $199 pass with $0.01 base fare, the 1-day and 10-day windows, $29–$89
early booking and $79–$159 peak day charges; $59.99 + $50 fare club; DOT 3h/6h
thresholds and 7-business-day/20-calendar-day refund windows; the 24-hour rule
with its 7-day precondition; live-human eligibility limited to within 24 hours of
departure or elite status.

### Fidelity test

Someone who knows Frontier's published policies reads `SPEC.md` and recognises
every rule and every number. Someone who greps this folder for "Frontier",
"GoWild", "Discount Den", "Spirit", "Denver-based ULCC" or any real person finds
nothing but the airport codes.

---

## Sources

- [Cognigy — Frontier Airlines case study](https://www.cognigy.com/en/case-study/frontier-airlines)
- [Cognigy — Lufthansa case study](https://www.cognigy.com/en/case-study/lufthansa)
- [CNBC — Frontier Airlines gets rid of telephone customer service](https://www.cnbc.com/2022/11/25/frontier-airlines-gets-rid-of-telephone-customer-service.html)
- [CNN — Frontier Airlines no longer has a customer service phone line](https://www.cnn.com/2022/11/26/business/frontier-airlines-customer-service-call-center)
- [Frontier — Announcing "The New Frontier"](https://news.flyfrontier.com/announcing-the-new-frontier-transparent-pricing-no-change-fees-and-enhanced-customer-experience/)
- [Frontier — Optional Services fee schedule](https://www.flyfrontier.com/optional-services/)
- [Frontier — Change & Cancel policies FAQ](https://faq.flyfrontier.com/help/voluntary-cancel-or-change)
- [Frontier — GoWild All You Can Fly Pass](https://www.flyfrontier.com/deals/gowild-pass/)
- [Frontier — Discount Den FAQ](https://faq.flyfrontier.com/help/what-is-discount-den)
- [Frontier — Disruption Assistance for Any Reason](https://www.flyfrontier.com/disruption-assistance-for-any-reason/)
- [Frontier — Elite Status Benefits](https://www.flyfrontier.com/frontier-miles/elite-status-benefits/)
- [The Points Guy — Frontier Miles elite status](https://thepointsguy.com/loyalty-programs/what-is-frontier-elite-status-worth/)
- [FinanceBuzz — Frontier baggage fees 2026](https://financebuzz.com/frontier-airlines-baggage-fees)
- [6abc — Lawsuit accuses Frontier Airlines of bogus baggage fees](https://6abc.com/amp/frontier-airlines-bag-policy-class-action-lawsuit-flight-fees/14085587/)
- [KRON4 — Frontier confirms gate agent incentive for baggage fees](https://www.kron4.com/news/frontier-airlines-confirms-gate-agent-incentive-for-increased-baggage-fees-after-viral-tiktoks/)
- [US DOT — Final rule requiring automatic refunds](https://www.transportation.gov/briefing-room/biden-harris-administration-announces-final-rule-requiring-automatic-refunds-airline)
- [US DOT — Refunds and other consumer protections](https://www.transportation.gov/airconsumer/refundsfinalruleapril2024)
- [Skift — Spirit Airlines shuts down](https://skift.com/2026/05/02/spirit-airlines-shuts-down/)
- [NPR — Spirit Airlines ceases operations](https://www.npr.org/2026/05/02/nx-s1-5807933/spirit-airlines-ceases-operations-folds)
- [Aviation Week — Republic picks Frontier brand over Midwest](https://aviationweek.com/republic-picks-frontier-brand-over-midwest-establishes-integration-timeline)
- [Computer Weekly — How voice AI is transforming customer service](https://www.computerweekly.com/news/366641314/How-voice-AI-is-transforming-customer-service)
- [ASAPP — Conversational AI for airline customer service](https://www.asapp.com/hub/conversational-ai-for-airline-customer-service)
- [Kaiban — Re-accommodation automation during IROPS](https://www.kaiban.io/use-cases/automating-re-accommodation-during-irregular-operations)
- [Wikipedia — Frontier Airlines (footprint and fleet)](https://en.wikipedia.org/wiki/Frontier_Airlines)
