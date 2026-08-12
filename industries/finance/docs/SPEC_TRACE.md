# SPEC_TRACE.md — spec → agents → tools, and what each optimisation pass changed

## The graph

```
START → reception → identity → {accounts, payments, cards, disputes}
        accounts → {payments, cards, disputes}
        payments → accounts
        cards ↔ disputes
        escalate_to_human: global, terminal
```

Six agents. The split is by **money-and-policy boundary**, not topic:

| Agent | Boundary it owns |
|---|---|
| `reception` | public information only — no account data, no money, no verification |
| `identity` | the GLBA gate; the only inbound path to everything account-bound |
| `accounts` | read-side + fee policy: balances, transactions, fee explanation, the reversal ladder, waiver math. Explains money, never moves it |
| `payments` | every movement of money, all two-step with fixed tokens: transfer, wire (fraud-warning regime), stop payment, loan payment (convenience-fee regime) |
| `cards` | card lifecycle: one-step protective block, two-step priced replacement, travel notice |
| `disputes` | the Reg E / Reg Z disclosure regime — federal scripts with clocks, acknowledgement before filing |

## Spec → agent → tool trace

| Spec section | Rule | Agent | Tool(s) | Enforced or measured |
|---|---|---|---|---|
| §2 identity | no data before verification | identity | `identify_member`, `verify_identity`; every gated tool | **enforced** (`IDENTITY_NOT_VERIFIED`) |
| §2 identity | third party gets nothing, not even existence | identity | `verify_identity` → `NOT_AUTHORIZED` | enforced refusal; *silence about existence* measured |
| §2 overdraft | $33 Courtesy Pay, 3/day cap, APPSN detail | accounts | `explain_fee` | enforced (data) |
| §2 overdraft | reversal ladder: first fee in 12 months auto-reverses | accounts | `request_fee_reversal` | **enforced** |
| §2 checking | waiver math ($1,000 DD / $5,000 ADB etc.) | accounts | `check_waiver_status` | enforced (math); explaining the gap measured |
| §2 savings | HYS $25 beyond 3 withdrawals/quarter | payments | `quote_internal_transfer` | **enforced** (fee in quote); speaking it measured |
| §2 wires | $15/$30 at the $2,500 boundary, $50 foreign | payments | `quote_wire` | enforced (tier math) |
| §2 wires | fraud warning before send; wires are final | payments | `confirm_wire` | **enforced** (`WIRE_WARNING_REQUIRED` without ack); reading the script measured |
| §2 wires | elder-exploitation hold | payments | `confirm_wire` → `EXPLOITATION_HOLD` | **enforced**; the calm explanation measured |
| §2 fees | stop payment $25, $0 on Cashback Rewards | payments | `quote_stop_payment` | enforced |
| §2 fees | loan payment $2.75 eCheck / $5.50 debit | payments | `quote_loan_payment` | enforced; offering the cheaper option measured |
| §2 cards | $10 replacement, free if stolen, $30/$35 expedited | cards | `quote_card_replacement` | enforced |
| §2 cards | block is protective, instant | cards | `block_card` (one step) | enforced (idempotent) |
| §2 disputes | Reg E clocks + provisional credit; oral notice OK; never refused | disputes | `file_dispute` | **enforced** (`DISCLOSURE_REQUIRED` + ack flag); clocks spoken measured |
| §2 disputes | Reg Z written-notice + withhold right | disputes | `file_dispute` (card type switches script) | enforced (script); spoken measured |
| §2 disputes | 60-day window computed, filed anyway outside it | disputes | `file_dispute` | enforced (window math + `outside_window` status) |
| §2 refusals | investment advice, outcome promises, 2-day-rule scare | all | *(no tool exists)* | **measured only** |
| §1 | recording + AI disclosure once at start | reception | *(prose)* | measured |
| §1 | legacy brands, routing number, hours, ID-theft partner | reception | `search_kb`, `get_branch_info` | enforced (data) |
| §2 membership | post-conversion eligibility | reception | `check_membership_eligibility` | enforced (matrix) |
| §7 | collections / hardship / fraud-in-progress → human | all | `escalate_to_human(reason)` | routing measured |

Two-step token pairs, all intra-node: `CL-XFER-2210`, `CL-WIRE-4821`, `CL-STOP-6604`,
`CL-PAY-7113` (payments), `CL-CARD-9917` (cards). A token never crosses a handoff.

## Pass 1 (spec re-read against the graph) — changes made

1. **Collapsed the planned `loans` agent into `payments`.** Loan payment by phone has the same
   regime as every other movement (disclose a fee, quote, token, confirm) — same script, same
   agent. A seventh agent bought nothing.
2. **Gave `payments` its own `get_balance`.** The highest-frequency compound intent is "check the
   balance, then move money"; without it every transfer forced a round trip through `accounts`.
3. **`get_fee` added to `accounts`** (public schedule, no gate) so fee explanations can quote the
   published number without bouncing to reception.
4. **Dispute filing uses the acknowledgement-flag pattern, not a token.** The measurable thing is
   the disclosure (clocks, provisional credit), not read-back of an amount; the healthcare
   fee-disclosure shape fits, the travel token shape doesn't.

## Pass 2 (graph re-read against the spec) — changes made

1. **Added `cards ↔ disputes` both ways** for the fraud playbook: block the card, file the claim,
   order the replacement — in either entry order.
2. **Added `payments → accounts`** for post-payment fee questions ("what was that $5.50?").
3. **Verified no forced round trip in the top-3 flows**: balance (2 handoffs), lost card (2),
   dispute (2).
4. **Tool-name audit**: nothing named `estimate`, `advice`, `promise`, `waive_any`; the reversal
   tool is a *request* with a server-decided outcome.
5. **Guard dedup**: `get_member_summary`, `get_balance`, `get_transactions` each appear on two
   agents; the identity gate lives once, in the server session — no per-agent guard drift.
6. **Membership eligibility stays public** on reception: prospective members can never verify.

Every intent has exactly one owning agent; every server guard maps to an error code; every
measurement rule maps to a row in the README policy table.
