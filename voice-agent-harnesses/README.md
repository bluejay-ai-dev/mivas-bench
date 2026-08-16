# voice-agent-harnesses

Harnesses that load an industry `agent_blueprint.json` into a runnable multi-agent voice runtime.

## Tool dispatch contract (industry-agnostic)

Harnesses are dumb pipes for industry tools. Each one reads the industry's
`tools.json` + `agent_blueprint.json`, registers every non-handoff, non-session
tool generically, and on invocation POSTs

```
POST {TOOL_SERVER_URL}/tools/{name}
{"arguments": { ...model-supplied args... }}
```

to the industry tool server, returning the JSON response to the model verbatim.
The server answers with the envelope its `tools.json` `outputSchema` declares
(e.g. `{"ok": bool, "data": ..., "error_code": ..., "<caller|patient>_safe_message": ...}`;
control-industry uses `{"success": ..., ...}`). An unknown tool name is a 404.

### Per-call database (conversation id)

Every industry tool call is namespaced to **one Bluejay simulation result id**
(the same value Bluejay already puts on the CHIRP WebSocket upgrade as
`X-Simulation-Result-Id`, or on the SIP INVITE for LiveKit/Pipecat workers).
That id is the conversation key for SQLite, traces, and evals — not a provider
call id (Vapi `call.id`, Retell `call_id`, …).

1. CHIRP (or the LiveKit worker) reads `X-Simulation-Result-Id`. If Bluejay
   omitted it, the harness mints `call_{uuid}` and logs; it never sends an
   empty id.
2. Every `POST {TOOL_SERVER_URL}/tools/{name}` includes
   `X-Mivas-Call-Id: <simulation_result_id>`.
3. The tool server `DBService` uses that header as the DB key. First touch
   for an id copies `db/schema.sql` + `db/seed.sql` into
   `{MIVAS_DB_PATH.parent}/calls/{id}.db`; later tools reuse the file.
4. `GET /health` is global (no id). `GET /state` and domain REST routes take
   the same header or `?call_id=` so evals dump **that** conversation.
5. Missing `X-Mivas-Call-Id` on `POST /tools/*` is **400**, except when
   `MIVAS_DB_SHARED=1` (local `--check` / `tests/converse.py`), which keeps a
   single debug DB.
6. Session / handoff tools still never hit the tool server.

Platform harnesses (vapi/retell/bland/cartesia) receive tools as a webhook on
`{PUBLIC_URL}/tool/{name}`. That request does not inherit the CHIRP
connection: the adapter must map the provider call id back to the Bluejay
simulation result id and send **that** as `X-Mivas-Call-Id`.

Tool kinds, from the blueprint entry's flags:

- **industry** (default): dispatched to `POST /tools/{name}` as above. No
  per-tool handler code in any harness.
- **handoff** (`handoff: true`): harness/provider-native (LiveKit agent switch,
  Vapi squad handoff, Retell state edge, ElevenLabs `transfer_to_agent`, soft
  prompt handoff on fixed-config sessions). Never hits the tool server.
- **session** (`session: true`, e.g. `end_call`): harness-native; ends the
  session with the harness's delayed-close/farewell behavior. Never hits the
  tool server.

The industry's REST routes (`GET /health`, `GET /state`, the domain routes)
remain for evals and debugging **on that pod** — harnesses only speak `/tools/{name}`.
After each call the harness freezes `GET /state` plus the SQLite file to S3
(`s3://$MIVAS_SNAPSHOT_BUCKET/mivas/{slug}/{id}.final.json`). Evals load S3.
Do not `GET` the public hostname for `/state` or `/snapshot`.

Concurrency on EKS is `max_concurrent ≈ MIVAS_REPLICAS × in_process_ws_limit`
(default replicas `1`; start `in_process_ws_limit` at 2–4). Raising replicas
is how overlapping Bluejay calls stay on isolated SQLite files without sharing
one process. Do not set `MIVAS_REPLICAS>1` on vapi/retell/bland/cartesia —
those webhooks hit a random replica. See
[docs/per-call-db-and-replicas.md](../docs/per-call-db-and-replicas.md).

Platform harnesses (vapi/retell/bland/cartesia) receive tool invocations as
provider webhooks on `{PUBLIC_URL}/tool/{name}` and forward them to
`POST {TOOL_SERVER_URL}/tools/{name}` unchanged.
