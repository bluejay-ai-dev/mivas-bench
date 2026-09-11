# Bluejay from the consumer side

Base URL `https://api.getbluejay.ai/v1` (`BLUEJAY_API_URL` overrides). Auth header
`X-API-Key: $BLUEJAY_API_KEY`. Public reference: https://docs.getbluejay.ai/api-reference/introduction.
The app is `https://app.getbluejay.ai`; a run lives at `/simulations/{sim}/runs/{run}`.

## Objects

| Object | Meaning here |
|---|---|
| Agent | the system under test: `connection_type=WEBSOCKET`, `websocket_url=wss://…`, `websocket_username/password` = CHIRP basic auth. Scripts key it by `external_agent_id=mivas:<slug>`. |
| Simulation | settings for a batch: `max_call_duration`, `max_concurrent`, `runs_per_digital_human`. Smoke: 4 min, concurrency 1. Full: 8 min. |
| Digital human | one test case: `intent`, `traits`, `expected_tool_calls`, `success_criteria`, `scripted_responses`, `speaks_first_config`. Generated from `industries/<I>/tasks/*/task.json` by `scripts/tasks_to_digital_humans.py`. |
| Run / result | one queue = one run; one result per conversation. `retrieve-simulation-result/{id}` carries `status`, `transcript_url`, `tool_calls[].actual`, `trace_ids`, `metrics[]`, `goal_success`. |

## Endpoints the scripts call

| Call | Path |
|---|---|
| find agent | `GET agent-by-external-id/{external_id}` |
| create / repoint agent | `POST add-agent` · `POST update-agent {agent_id, …}` |
| create simulation | `POST create-simulation {agent_id, name, max_concurrent, max_call_duration, max_call_duration_units, runs_per_digital_human, hangup_on_transfer}` |
| read / fix simulation | `GET simulation/{id}` · `PUT simulation/{id}` |
| digital humans | `GET digital-human-by-test-name/{title}` · `POST create-digital-humans {simulation_ids, digital_humans}` · `PUT update-digital-human/{id} {simulation_ids}` · `GET digital-humans-by-simulation/{sim}` |
| queue | `POST queue-simulation-run {simulation_id, digital_human_ids, runs_per_digital_human}` → `simulation_run_id` |
| poll | `GET retrieve-simulation-results/{run}` → `simulation_run`, `simulation_results[]` |
| one result | `GET retrieve-simulation-result/{result}` |
| trace | `GET traces/{trace_id}` |

## Quirks the scripts already handle

- `create-simulation` has stored `max_call_duration=4, units=minutes` as **4 seconds… or 7200**. Always GET and PUT until it reads back `minutes × 60`.
- `add-agent` may return no id: re-read via `agent-by-external-id`.
- `create-digital-humans` takes **unwrapped** digital humans plus top-level `simulation_ids`; wrapping each as `{digital_human: …}` is a 422.
- `test_name` is unique per org: the scripts look up by test name and re-attach instead of duplicating.
- Setting a digital human's `simulation_ids` **replaces** its memberships: merge, never overwrite.
- A result is not final until its status leaves `RUNNING / QUEUED / EVALUATING / CONVERSATION_ENDED`; tools are extracted after the harness posts `trace_ids`, so scoring `CONVERSATION_ENDED` undercounts.
- `NO_ANSWER` / `NO_CONNECTION` = Bluejay could not reach your `wss://` (DNS, TLS, auth, no Ready pod). Not a model failure.

## Rubric

Per call, all six required. Thresholds live in `scripts/score_rubric.py` (`--self-test`).

| # | Check | Passes when | Fails usually because |
|---|---|---|---|
| 1 | utterances | `num_turns > 0`, `transcript_url` set | socket never connected / auth |
| 2 | tool calls | some `tool_calls[].actual` non-empty | no spans, `BLUEJAY_API_KEY` missing in pod, tool wiring |
| 3 | traces | `trace_ids` non-empty | OTLP export / update-simulation-result failed |
| 4 | dead air | `max_punctuation_latency ≤ 25 s`, no "are you still there", no user→agent gap > 8 s | inbound audio gated, tool wait blocks speech |
| 5 | audio continuity | `agent_audio_dropouts == 0`, clipping ≈ 0 | sample-rate / pacing bugs, muting on echo |
| 6 | opening silence | `time_to_first_agent_utterance ≤ 3 s` | provider session created on accept instead of at boot |

`goal_success` is the LLM judge's opinion of the transcript. It passes calls that never
called the tool and fails calls that did; the scripts print it but never gate on it.

## Reading a result

```bash
uv run python .claude/skills/mivas-run/scripts/bluejay.py results --run RUN --out verify-out/smoke/RUN
uv run python .claude/skills/mivas-run/scripts/bluejay.py score verify-out/smoke/RUN
curl -s "$(jq -r .simulation_result.transcript_url verify-out/smoke/RUN/<id>.json)" | jq '.[] | {speaker, start_offset_ms, text}'
```

Transcript gate (read at least one per smoke): greeting is a full sentence; the agent
answers what the caller actually said; after a handoff the specialist continues (no cold
re-ask); dates are this year; the call ends with `end_call`, not a cut.

## Full-run scoring

Bluejay's judge is not the benchmark score. After a full run:

```bash
uv run python verifiers/verify_run.py RUN                                    # exit 1 = void results, rerun those
uv run python verifiers/verify_task_run.py RUN --industry $I --harness $H     # tools ∧ handoff ∧ final DB state
uv run python scripts/bluejay_run_to_csv.py RUN --industry $I --harness $H    # one row per conversation
```

`verify_task_run.py` pulls `<result_id>.final.json` from the snapshot store
(`MIVAS_SNAPSHOT_BUCKET`, `AWS_ENDPOINT_URL_S3` for S3-compatible) and diffs the
write-bearing tables against each task's `exp_db_state`. Without a store it prints a skip
note and scores tools + handoffs only.
