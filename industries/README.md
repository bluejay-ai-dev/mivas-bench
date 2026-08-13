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

## Per-call database contract

Harnesses POST industry tools to `POST /tools/{name}` with
`X-Mivas-Call-Id: <Bluejay simulation result id>`. `DBService` (shared
runtime module) is the only code that picks a SQLite file:

- **Id:** Bluejay `X-Simulation-Result-Id` (e.g. `675`). Not a Vapi/Retell call id.
- **Header:** `X-Mivas-Call-Id`. Query alias `?call_id=` on `GET /state` and domain REST routes.
- **First touch:** if `{MIVAS_DB_PATH.parent}/calls/{id}.db` is missing, copy `db/schema.sql` then `db/seed.sql` into that file.
- **Later tools:** open the existing file. API handlers keep `with _db() as conn: conn.execute(...)` — they never choose a path or call `init_db()`.
- **Miss:** `POST /tools/*` without the header is **400**. `MIVAS_DB_SHARED=1` (local `--check` / converse) is the only shared-DB fallback.
- **`GET /health`:** global, no id.
- **`GET /state`:** scoped to the header / `?call_id=` so evals compare that call’s
  **final** dump to the **initial** seed (`schema.sql` + `seed.sql`). A call that
  never invoked a tool still has a defined initial state: first `GET /state` for
  that id lazy-creates the file from seed. Missing id is **400** unless
  `MIVAS_DB_SHARED=1`.

```bash
# final dump for simulation result 675 (header or query alias)
curl -s http://127.0.0.1:8000/state -H 'X-Mivas-Call-Id: 675'
curl -s 'http://127.0.0.1:8000/state?call_id=675'
# never-touched id → seed only
curl -s 'http://127.0.0.1:8000/state?call_id=676'
# frozen dump written at CHIRP teardown (eval path when replicas > 1)
curl -s http://127.0.0.1:8000/snapshot/675
```

On EKS, Ingress sends `/state` and `/snapshot` to the tools Service (one writer),
not a random CHIRP replica. Evals should prefer `GET /snapshot/{id}` so they
read the teardown freeze even if the SQLite file is later mutated or the
harness pod is gone.
