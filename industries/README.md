# industries

Industry-specific voice agent benchmarks for MIVAS.

| Industry | Firm / product | Agents |
|---|---|---|
| `control-industry` | Minimal control pack | reception → scheduler |
| `healthcare` | Straus Dermatology ("Robin") | reception, identity, scheduling, coverage, cosmetic, billing, clinical |
| `legal` | Halverson & Reed ("Hal") | reception, screening, intake, scheduling, client_services |
| `finance` | Finance pack | see pack README |
| `customer-support` | Support pack | see pack README |
| `travel` | Travel pack | see pack README |

Each pack owns `agent_blueprint.json`, `tools.json`, system prompts, SQLite `db/`, and a FastAPI `tool_server.py` state API.
