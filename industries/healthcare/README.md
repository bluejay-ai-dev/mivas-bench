# healthcare

Straus Dermatology ("Robin")-style dermatology front-desk multi-agent industry for MIVAS, adapted from [straus-voice-agent](https://github.com/bluejay-ai-dev/straus-voice-agent).

Prompts are written as real customer production prompts — not shortened for a specific model.

## Agents

1. `reception` — greet, AI disclosure once, language, intent, KB, route
2. `identity` — PHI gate (name + DOB); only path to `billing` and `clinical`
3. `scheduling` — book / reschedule / cancel / waitlist / allergy
4. `coverage` — carrier × plan × office × provider
5. `cosmetic` — approved-table quotes, deposit + 72h policy before booking
6. `billing` — balance, charge explainers, payment link / financing / fee waiver
7. `clinical` — results status only, refills never approved, nurse messages, portal

There is **no safety agent**. Escalation is a single global tool, `transfer_to_human`.

## Escalation and refusal

| Situation | Behavior |
|---|---|
| Off-rails / horrible / jailbreak-like request | Say "Sorry, I can't help with that." Do not transfer. |
| Caller asks for a human | Call `transfer_to_human`. |
| Clinical emergency | Tell them to call 911, say "I'm transferring you to a human now," then `transfer_to_human`. |

## Files

- `agent_blueprint.json` — agents, tool surfaces, `transfer_to_*` handoffs
- `agent_blueprint.mmd` — Mermaid graph of the blueprint handoff edges
- `system-prompts/*.md` — full per-node prompts (shared CORE rules in each)
- `tools.json` — MCP tool schemas for the industry tool surface
