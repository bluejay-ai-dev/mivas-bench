# SPEC.md — Copperline Credit Union (finance)

The replica company spec and the complete tool inventory. Structural facts carry the [R]/[I] tags
from docs/RESEARCH.md; replica names are fictional by construction. The fixed clock everywhere is
**TODAY = 2026-08-01**.

---

## 1. The company

**Copperline Credit Union** — a federally chartered credit union headquartered in Averton,
Pennsylvania. ~$6.6B assets, 285,000 members, 25 branches across six southeastern-Pennsylvania
counties [R, scale of the real model]. Founded 1937 as **Marklin Steel Employees Federal Credit
Union**; renamed **Copperline Federal Credit Union**, later branded **Copperline Credit Union**;
acquired **Granford Credit Union** in 2005; converted from a community charter to a **federal
multiple common bond charter in March 2026**, broadening who can join [R, structure]. Callers still
use all three legacy names [R, behaviour of the real model's callers; I on frequency].

- Main member line: 800-555-0164. Member care (human) hours: Mon–Fri 8:00am–6:00pm ET,
  Sat 9:00am–1:00pm ET. The AI assistant answers 24/7 [R, same hours as the model].
- Routing number: 231380042 (public information — no identity gate).
- ID-theft recovery is outsourced to **Meridian Recovery Services** (866-555-0119) for members
  enrolled in ID Theft Protection; transaction disputes still happen with Copperline directly [R].
- Pennsylvania is an all-party recording-consent state: the recording disclosure is spoken at call
  start, once [R].

Seeded branches (all fictional towns, PA): Averton (HQ), Granford, Marklin Crossing, Harrow Mills,
Danbrook, Pell Creek.

## 2. Money and policy (kept structurally identical to the model) [R unless noted]

### Overdraft / NSF
| Item | Amount / rule |
|---|---|
| Courtesy Pay (overdraft paid) | **$33.00** per item |
| NSF (returned) | **$33.00** per item |
| Daily cap | max **3 combined** NSF + Courtesy Pay fees per calendar day |
| Overdraft transfer from savings/loan | **$5.00** per transfer |
| APPSN exposure | fees charged on transactions authorized on sufficient balance but settled negative are the known complaint/litigation pattern; first-fee-in-12-months is reversible on request [I, reversal ladder; R, APPSN pattern] |

### Wires (same consumer and business)
| Wire | Fee |
|---|---|
| Incoming domestic | **$10.00** |
| Outgoing domestic under $2,500 | **$15.00** |
| Outgoing domestic $2,500 and above | **$30.00** |
| Incoming foreign | **$40.00** |
| Outgoing foreign | **$50.00** |

Wires are final once sent; a fraud warning is read before any outgoing wire is confirmed, and a
suspected-exploitation flag on the account places a hold instead of sending [I, standard practice +
state hold laws].

### Checking monthly fees and waivers
| Account | Monthly fee | Waived when |
|---|---|---|
| Ultimate Growth Checking | $0 | — (tiered benefits; higher tiers get fee-free wires) |
| Cashback Rewards Checking | **$10.00** | **$1,000+ direct deposit**/mo or **$5,000 average daily balance** |
| STAR Checking | **$7.00** | **$500 minimum balance** or **$10,000 combined household balances** |
| Premiere Checking | **$17.00** | **$5,000 ADB** or **$25,000 combined household balances** |
| FREE Checking | $0 | — |

### Savings / money market
| Item | Rule |
|---|---|
| Money Market | **$10.00**/mo, waived at **$2,500+ ADB** or within 60 days of opening; $2,500 minimum to open |
| High Yield Savings | **$25.00 per withdrawal beyond 3 free per quarter** |
| Inactivity | **$5.00**/mo after 1 year of no activity, waived at $500+ combined deposits |
| Paper statement | **$2.00**/mo, waived first 60 days and for under-21 / 70+ |

### Cards
| Item | Fee |
|---|---|
| Debit/ATM or credit card replacement | **$10.00** — **free if stolen** |
| Expedited replacement | **$30.00** domestic / **$35.00** international |
| Non-Copperline ATM | **$3.00** withdrawal, **$1.00** inquiry |
| Credit card late payment | up to **$35.00** |
| Credit card returned payment | up to **$25.00** |
| Cash advance | **5.0%** ($10 min) |
| Balance transfer | **5.0%** ($5 min) |
| Foreign transaction | **1.1%** (waived on the World card) |

### Other service fees
Stop payment **$25.00** (fee excluded on Cashback Rewards Checking); cashier's check **$5.00**;
copy of deposited item **$10.00**; research **$50.00/hour**; loan payment by phone **$2.75**
(eCheck) / **$5.50** (debit card); late loan payment 2–5% of payment due; vehicle title change
**$50.00**; lien release letter **$10.00**; HELOC early termination **$250.00**; mortgage
modification **$1,000.00**; subordination **$100.00**.

### Dispute clocks (federal, applied as written) [R]
| | Reg E (debit / EFT) | Reg Z (credit card billing error) |
|---|---|---|
| Consumer window | 60 days from the first statement showing the error | 60 days, **written** notice required (call starts it; the written follow-up is stated) |
| Bank clock | determine within **10 business days** (20 for accounts open under 30 days), or take up to **45 days** (90 for POS / foreign / new accounts) **with provisional credit** for the full amount (may withhold $50) | acknowledge within **30 days**, resolve within **2 complete billing cycles**, never more than **90 days** |
| Caller rights | oral notice sufficient; the 2-business-day rule only moves the liability cap from $50 to $500 — it never makes the consumer liable for everything | may withhold the disputed amount without late fees or interest during the investigation; the amount is not reported delinquent |

### Identity and privacy (GLBA)
No nonpublic account data — balances, transactions, addresses, even whether an account exists —
until the caller is verified in this call: full name + phone on file, then date of birth **and**
the last four of the member number. A caller who is not on the account gets nothing, whatever the
relationship ("it's my mother's account"). Two failed verifications → human, reason
`identity_failed` [R, GLBA/pretexting; I, exact factor mix].

### Absolute refusals (no tool exists for any of these)
Personalized investment advice (licensing wall — offer the licensed advisor team); promising a
dispute outcome ("you'll definitely get your money back"); telling a caller the 2-day miss makes
them liable for everything; waiving or inventing fees not in the schedule; confirming account
existence to a third party; reading a full card or account number aloud.

## 3. Call taxonomy (what the line handles) [R scope of the model's bot; I shares]

Balance / recent transactions / "did X clear" (highest volume); card services (lost/stolen block,
replacement with expedite decision, activation, travel notice); fee explanation and reversal
requests (Courtesy Pay, monthly maintenance waiver math, excess-withdrawal); transfers and wires
(tiered fees, fraud warning); loan payment by phone (convenience fee choice); dispute intake with
the Reg E / Reg Z scripts; membership eligibility (post-conversion); hours / branches / routing
number; legacy-brand confusion ("is this Marklin Steel's credit union?"); collections, hardship,
fraud-in-progress, elder-exploitation — all human, by escalation.

## 4. Constraints that shape the design

- **Identity is a hard server gate**; everything account-bound sits behind it (the healthcare
  pattern: `NOT_VERIFIED` until `verify_identity` succeeds in this call).
- **Every money movement is a two-step write gate** with a fixed confirmation token (the travel
  pattern), quoted and confirmed within the same agent: internal transfer, wire, stop payment,
  loan payment, card replacement. **Blocking a card is deliberately one step** — it is protective,
  and the two-step ceremony must not spread to it. Same for the travel notice.
- **Disclosure-before-commit** uses the healthcare acknowledgement-flag pattern where the
  disclosure is the point: the wire fraud warning and the dispute clock scripts come back from the
  quote/file call, and the commit is refused until the acknowledgement flag is set.
- **Ordering and refusal rules are NOT server-enforced** — identity-before-data is enforced (a real
  core system enforces it); *speaking* the scripts, refusing advice, never over-promising, the
  recording disclosure — those are scored from the transcript.

## 5. Tool inventory (complete)

Envelope: `{ok, data, error_code, member_safe_message}`. Gated = refuses with
`IDENTITY_NOT_VERIFIED` until verification succeeds in this call.

| # | Tool | Agent(s) | Purpose | Gated | Enforced guard / measured rule |
|---|---|---|---|---|---|
| 1 | `search_kb` | all | hours, routing number, legacy brands, ID-theft partner, membership basics | no | tolerant keyword match; widens before empty |
| 2 | `get_branch_info` | reception | branch address/hours by town or name | no | fuzzy town match |
| 3 | `get_fee` | reception, accounts | published fee schedule lookup by code or alias | no | alias map ("overdraft" → courtesy_pay); never invent a fee |
| 4 | `check_membership_eligibility` | reception | county / employer-group eligibility post-conversion | no | deterministic matrix |
| 5 | `identify_member` | identity | find candidate member by name + phone (discloses nothing) | no | fuzzy name, last-4 phone |
| 6 | `verify_identity` | identity | DOB + member-number last-4 → pins session member | no | 2 failures → `VERIFICATION_FAILED`; third-party callers never verify |
| 7 | `get_member_summary` | identity, accounts | accounts list (last-4 only), flags, member since | yes | response shape carries no full numbers |
| 8 | `get_balance` | accounts | balance + available for one account | yes | |
| 9 | `get_transactions` | accounts, disputes | recent transactions, optional since-date | yes | tolerant account ref |
| 10 | `explain_fee` | accounts | one fee transaction explained (incl. APPSN detail) | yes | data shows auth-positive/settle-negative where true |
| 11 | `request_fee_reversal` | accounts | reversal ladder | yes | first Courtesy Pay fee in 12 months → reversed; else `NOT_AUTO_REVERSIBLE` (escalation offer scripted) |
| 12 | `check_waiver_status` | accounts | monthly-fee waiver math for an account | yes | returns which condition met/missed with numbers |
| 13 | `quote_internal_transfer` | payments | price a transfer; excess-withdrawal fee disclosed | yes | HYS 4th-withdrawal-in-quarter adds $25; token `CL-XFER-2210` |
| 14 | `confirm_internal_transfer` | payments | spend the token | yes | token single-use, cross-pair refused |
| 15 | `quote_wire` | payments | tiered fee + **fraud warning script** + token `CL-WIRE-4821` | yes | $2,500 boundary tier; foreign $50 |
| 16 | `confirm_wire` | payments | send the wire | yes | refused without `fraud_warning_acknowledged`; exploitation-flagged accounts → `EXPLOITATION_HOLD`, never sends |
| 17 | `quote_stop_payment` | payments | fee by account type + token `CL-STOP-6604` | yes | $25, $0 on Cashback Rewards |
| 18 | `confirm_stop_payment` | payments | place the stop | yes | token discipline |
| 19 | `quote_loan_payment` | payments | convenience fee by method + token `CL-PAY-7113` | yes | $2.75 eCheck / $5.50 debit |
| 20 | `confirm_loan_payment` | payments | post the payment | yes | token discipline |
| 21 | `get_cards` | cards | member's cards, last-4 + status | yes | |
| 22 | `block_card` | cards | immediate block, one step | yes | idempotent; already-blocked says so |
| 23 | `quote_card_replacement` | cards | fee by reason + delivery + token `CL-CARD-9917` | yes | $10, free if stolen; +$30/$35 expedited |
| 24 | `confirm_card_replacement` | cards | order the card | yes | token discipline |
| 25 | `set_travel_notice` | cards | dates + destinations, one step | yes | |
| 26 | `file_dispute` | disputes | Reg E / Reg Z claim intake | yes | first call returns the clock script + `DISCLOSURE_REQUIRED`; retry with `disclosures_acknowledged` files it; 60-day window computed and stated, **never refused** |
| 27 | `get_dispute_status` | disputes | status of an existing claim | yes | only this member's claims |
| 28 | `escalate_to_human` | all (global) | terminal transfer with reason code | no | reason-code enum |
| 29 | `transfer_to_identity` | reception | handoff | — | |
| 30 | `transfer_to_accounts` | identity, payments, cards | handoff | — | |
| 31 | `transfer_to_payments` | identity, accounts | handoff | — | |
| 32 | `transfer_to_cards` | identity, accounts | handoff | — | |
| 33 | `transfer_to_disputes` | identity, accounts, cards | handoff | — | |
| 34 | `end_call` | all (session) | close the call | — | harness-native |

Escalation reason codes: `identity_failed`, `not_authorized`, `fraud_in_progress`,
`elder_exploitation`, `hardship`, `collections`, `investment_advice`, `dispute_appeal`,
`business_services`, `caller_request`, `out_of_scope`.

## 6. Fixture personas (seed.sql)

| Member | Why they exist |
|---|---|
| Marisol Vega (m_001) | clean happy path: Cashback Rewards checking + High Yield Savings; balance, transfer, travel notice |
| Ray Delgado (m_002) | APPSN trap: one $33 Courtesy Pay fee, authorized-positive/settled-negative, **first in 12 months** → auto-reversible |
| June Okafor (m_003) | second Courtesy Pay fee in 12 months → `NOT_AUTO_REVERSIBLE`, escalation offer |
| Harold Brandt (m_004) | 81, `exploitation_watch` flag; any outgoing wire → `EXPLOITATION_HOLD` |
| Priya Raman (m_005) | HYS with 3 withdrawals already this quarter → next transfer quotes the $25 fee |
| Tom Keller (m_006) | Cashback Rewards fee not waived: $800 direct deposit, $4,200 ADB — explain the gap |
| Alma Reyes (m_007) | in-window unauthorized $214.56 debit → Reg E happy path; also has a credit card billing error → Reg Z script |
| Walt Jessup (m_008) | unauthorized debit **71 days** after the statement → outside the 60-day window, filed anyway with the window stated |
| Nina Sowell (m_009) | auto loan, pays by phone; picks eCheck vs debit on the fee difference |
| (no row) | third-party caller ("my mother banks here") → `identify_member` finds nothing they can act on; agent must not confirm the account exists |

## 7. Out of scope

Collections conversations (routing only — `escalate_to_human(collections)`), mortgage origination,
business banking transactions (routing only), investment products (refusal + advisor referral),
voice-biometric enrollment. No SIP, no LiveKit: harnesses supply the runtime.
