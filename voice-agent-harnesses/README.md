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
remain for evals and debugging — harnesses only speak `/tools/{name}`.

Platform harnesses (vapi/retell/bland/cartesia) receive tool invocations as
provider webhooks on `{PUBLIC_URL}/tool/{name}` and forward them to
`POST {TOOL_SERVER_URL}/tools/{name}` unchanged.
