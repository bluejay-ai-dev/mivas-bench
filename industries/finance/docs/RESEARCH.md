# RESEARCH.md — finance (retail banking / credit union)

Every fact is tagged **[R]** (sourced, URL kept) or **[I]** (inferred). The replica map at the end
is the audit trail: the built industry contains nothing of the real company.

---

## 1. Industry landscape — voice AI in consumer banking, 2024–2026

Production phone voice AI in US banking is concentrated in **credit unions and community banks**
via vertical vendors; the megabanks run virtual assistants mostly in-app (BofA Erica: 3B+
interactions, 50M users, digital channel not the phone line
[R] https://newsroom.bankofamerica.com/content/newsroom/press-releases/2025/08/a-decade-of-ai-innovation--bofa-s-virtual-assistant-erica-surpas.html)
with NLU IVR front doors on the phone [I].

**What is automated on the phone today** [R, aggregate of vendor case studies below]: balance and
transaction history, card actions (activate / block / replace / PIN), payments and internal
transfers, loan payment and payoff status, dispute *intake* (form-filling, never adjudication),
digital-banking resets, hours/locations/routing number, travel notices, collections routing.

**What is deliberately kept human** [R] https://www.backbase.com/blog/voice-agents-in-banking-use-cases-governance,
https://www.lorikeetcx.ai/articles/ai-support-us-banks-credit-unions-guide:
regulated advice (licensing wall), dispute adjudication, fraud-in-progress, financial hardship,
elder-exploitation cases. Guardrail principle is *fail safe*: when in doubt about vulnerability,
escalate.

**Vendors, ranked** [R]:

| Ranking | Leaders | Evidence |
|---|---|---|
| Deployment value | Nuance/Microsoft + in-house NLU at megabanks; Kasisto (DBS, Standard Chartered, TD, JPM, Wells, RBC); PolyAI (UniCredit "Mia", 27% of calls automated) | https://kasisto.com/press-releases/conversational-ai-by-kasistos-kai-banking-powers-standard-chartereds-virtual-assistant/, https://poly.ai/banking-assistants/ |
| Call volume answered by AI | interface.ai (100+ FIs, 60–84% automation, "$4.4M annual savings"); Posh AI (Citadel CU alone 3.2M+ calls); Glia (voice trained on 1,000+ banking journeys) | https://interface.ai/case-studies/, https://www.posh.ai/client-stories/citadel, https://www.glia.com/voice-ai |
| Breadth of adoption | Eltropy (750+ community FIs), Glia (700+), interface.ai (100+), Posh (dozens of named stories) | https://eltropy.com/ai/, https://www.glia.com/voice-ai |

**Call taxonomy with rough shares** [I, triangulated from vendor scope lists + the one public
per-institution categorization (Suncoast CU via PissedConsumer: inquiries 21%, payments/charges 18%,
cards 18%, account 17%) [R] https://suncoast-credit-union.pissedconsumer.com/customer-service.html]:
balance/history/"did X clear" 20–30%; card services 15–20%; payments and transfers 10–15%; digital
banking support 8–12%; disputes/fraud 5–10%; loan servicing 5–10%; fees and rates 5%;
hours/locations/routing 5–10%; collections/hardship remainder.

**Regulatory rules with transcript-detectable consequences** (the measurement surface's raw
material — full numbers in SPEC.md):

- **GLBA / anti-pretexting** — verify identity before disclosing any nonpublic account data; never
  confirm an account exists to a third party [R] https://archive.fdic.gov/view/fdic/1946/fdic_1946_DS2.pdf
- **Reg E, 12 CFR 1005.11** — 60-day consumer window from the first statement showing the error;
  10 business days to determine (20 for new accounts) or provisional credit and up to 45 days
  (90 for POS/foreign/new-account); oral notice is sufficient; the "2 business day" rule only moves
  the liability cap from $50 to $500, it never makes the consumer liable for everything
  [R] https://www.ecfr.gov/current/title-12/chapter-X/part-1005/subpart-A/section-1005.11,
  https://www.consumercomplianceoutlook.org/2025/third-issue/error-resolution-procedures/
- **Reg Z, 12 CFR 1026.13** — credit-card billing errors: written notice within 60 days,
  acknowledge in 30 days, resolve within 2 complete billing cycles (never more than 90 days),
  consumer may withhold the disputed amount without late fees during investigation
  [R] https://www.ecfr.gov/current/title-12/chapter-X/part-1026/subpart-B/section-1026.13
- **Licensing wall** — no personalized investment advice; factual product info only
  [R framing] https://www.backbase.com/blog/voice-agents-in-banking-use-cases-governance
- **Pennsylvania all-party recording consent** — disclosure at call start
  [R] https://www.recordinglaw.com/party-two-party-consent-states/pennsylvania-recording-laws/phone-calls/
- **Wire finality** — wires effectively irrevocable once sent (UCC 4A); banks run scam-interdiction
  scripts on unusual wires [I, standard practice]
- **Elder financial exploitation** — roughly half of states have transaction-hold laws; good-faith
  reports immunized (Senior Safe Act) [R] https://www.aba.com/news-research/analysis-guides/state-hold-laws-and-elder-financial-exploitation-survey-report

## 2. Three companies using voice AI on the phone today

1. **Citadel Credit Union** (Exton, PA; $6.6B, 285K members) — Posh AI "Adel" answers the main
   member line 24/7 since **October 2021**: greets all callers, authenticates members, executes
   banking transactions, routes to specialty queues (collections, indirect lending, new-member
   onboarding); transfers on request, on account flags, or when complexity exceeds automation.
   **3.2M+ calls** handled by Aug 2026; after-hours overflow cost cut 63%; NPS 63→70
   [R] https://www.posh.ai/client-stories/citadel, https://www.citadelbanking.com/bank/adel.
2. **Great Lakes Credit Union** (Bannockburn, IL; ~$1.5B) — interface.ai "Olive", live Aug 2023,
   automates 60–70%+ of business-hours calls (vs 25% legacy IVR containment): balances, card
   actions, payments, loan status, voice-biometric enrollment. Advisory support deliberately moved
   to upskilled humans. COO testified about it before a U.S. Congressional hearing
   [R] https://www.cutoday.info/THE-feature/Great-Lakes-CU-Introduces-Olive,
   https://interface.ai/coo-of-great-lakes-credit-union-addresses-u-s-congressional-hearing-on-ai-in-financial-services/.
3. **USAA** (San Antonio, TX; 13M+ members) — enterprise virtual assistant + a new natural-language
   engine threading phone and IVR conversations (VP enterprise digital, trade press); licensed reps
   kept for insurance/investment advice. Weakest evidence for end-to-end autonomous voice resolution
   [R] https://www.dig-in.com/list/how-usaa-is-modernizing-interactions-with-members [I on scope].

## 3. Choice: Citadel Credit Union

- **Largest public surface** — a full published fee page with exact dollars for 60+ consumer and
  business fees including waiver thresholds [R] https://www.citadelbanking.com/about-citadel/why-citadel/great-rates-low-fees;
  25 branches across six southeastern-PA counties; a named bot advertised on the company's own site.
- **Richest call taxonomy** — retail + business banking + cards + auto/indirect + mortgage/HELOC +
  collections queues, all documented in the Posh case study [R] https://www.posh.ai/client-stories/citadel.
- **Most testable money-and-policy rules** — $33 Courtesy Pay with a 3-per-day cap, amount-tiered
  wire fees, per-account monthly-fee waiver math, a $25-per-excess-withdrawal savings trap, Reg E /
  Reg Z clocks, PA all-party recording consent.
- **Bonus: real complaint corpus for hard-mode scenarios** — a $1.86M class settlement over
  APPSN overdraft fees (authorize-positive, settle-negative)
  [R] https://www.bbb.org/us/pa/exton/profile/financial-services/citadel-federal-credit-union-0241-80015515,
  elder-fraud complaints from family members who are not on the account, and fraud-claim-denial
  escalations — each becomes a fixture.
- **Bonus: serial renamer** — Lukens Employee FCU (1937) → Citadel FCU → Citadel Credit Union, plus
  the 2005 Atlantic Credit Union acquisition: callers using legacy names is a permanent testable
  behaviour [R] https://en.wikipedia.org/wiki/Citadel_Credit_Union.

USAA is fee-thin and insurance-heavy; GLCU has a thinner public surface. Citadel it is.

## 4. Replica map (real → replica)

Structural facts (every fee, window, cap, waiver threshold, clock, credential, and behaviour) are
kept **identical**. Names, numbers, places, and brands are all new.

| Real | Replica |
|---|---|
| Citadel Credit Union | **Copperline Credit Union** |
| "Adel" (Posh AI bot) | unnamed assistant voice of Copperline |
| Lukens Employee Federal Credit Union (1937, steelworkers) | **Marklin Steel Employees Federal Credit Union** (1937) |
| Citadel Federal Credit Union (legacy legal name) | **Copperline Federal Credit Union** |
| Atlantic Credit Union (acquired 2005) | **Granford Credit Union** (acquired 2005) |
| Exton, PA HQ | **Averton, PA** HQ |
| 25 branches, six SE-PA counties | same scale; six seeded branch rows (Averton, Granford, Marklin Crossing, Harrow Mills, Danbrook, Pell Creek), all fictional PA towns |
| 800-666-0191 | **800-555-0164** |
| citadelbanking.com | **copperlinecu.example** |
| Routing number (real) | **231380042** (fictional) |
| Merchants Information Solutions (ID-theft recovery partner, 866-647-6223) | **Meridian Recovery Services, 866-555-0119** |
| March 2026 federal multiple-common-bond charter conversion | same, same date |
| Member care hours M–F 8–6, Sat 9–1 ET; AI 24/7 | same |
| $6.6B assets / 285,000 members | same |
| PA all-party recording consent | same (company stays in Pennsylvania) |

Fee schedule, Reg E/Reg Z clocks, waiver thresholds, card fees, wire tiers: carried over unchanged —
see SPEC.md §2 for the full table. The test of the replica: someone who knows the real company
recognises every rule; someone who greps this folder for the real company finds nothing.

## 5. Gaps and caveats

- interface.ai case-study pages 403'd the fetcher; Dupaco figures came via search snippets [R-lite].
- Suncoast call-type shares are complaint-skewed; composite volume shares are [I].
- The real company does not republish the federal Reg E/Reg Z clocks on its own pages; the replica
  applies the standard federal numbers [I that they follow them, R for the numbers themselves].
- Adel's precise voice-containment rate is unpublished; Posh publishes calls-fielded only [R].
