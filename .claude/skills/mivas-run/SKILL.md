---
name: mivas-run
description: >-
  Runs MIVAS Bench end to end from a consumer seat: prerequisites (Bluejay API key,
  provider key), a harness (existing or self-built), a Kubernetes deployment (local,
  EKS, Baseten, or any cluster), then a gated ladder of smoke tests, three Bluejay
  smoke calls, harness + benchmark integrity, and finally a full industry run.
  Use when someone says /mivas-run, "run MIVAS myself", "set up mivas-bench",
  "deploy a harness for MIVAS", "smoke my harness on Bluejay", "benchmark my voice
  model on healthcare/legal/customer-support", or asks how to plug their own voice
  agent into MIVAS.
argument-hint: "family/runtime industry [target: local|eks|baseten|k8s]"
---

# MIVAS run (consumer)

Everything here uses the public Bluejay REST API with **your** `BLUEJAY_API_KEY`
and **your** cluster. No Bluejay internals, no shared infrastructure.

## Prerequisites (stop if any is missing)

1. Bluejay account + API key (Bluejay app → API Keys) → `BLUEJAY_API_KEY` in `.env`.
2. Provider key for the harness (`OPENAI_API_KEY`, `GOOGLE_API_KEY`, …). Table: [HARNESS.md](HARNESS.md#existing-harnesses).
3. A harness you fully control: an existing `voice-agent-harnesses/<family>/<runtime>/`, or your own built per [HARNESS.md](HARNESS.md#build-your-own).
4. Somewhere to run it that Bluejay can dial over public `wss://`: [DEPLOY.md](DEPLOY.md) (local + tunnel, EKS, Baseten, any Kubernetes).
5. A snapshot store for per-call final state, or accept that state scoring is skipped: [DEPLOY.md → Storage](DEPLOY.md#storage).

```bash
git clone https://github.com/bluejay-ai-dev/mivas-bench && cd mivas-bench && uv sync && cp .env.example .env
```

## Ladder (each rung gates the next)

Track it:

```
- [ ] 0 preflight      scripts/preflight.py            (keys, pair on disk, --check, store)
- [ ] 1 deploy         DEPLOY.md target → pods Ready → preflight --k8s / --url (wss 101)
- [ ] 2 smoke calls    scripts/bluejay.py smoke  → 3 calls, rubric PASS each
- [ ] 3 integrity      scripts/verify_integrity.py → INTEGRITY PASS
- [ ] 4 full industry  scripts/bluejay.py full   → verify_task_run + CSV
```

Commands (from the repo root; `H=family/runtime`, `I=industry`):

```bash
uv run python .claude/skills/mivas-run/scripts/preflight.py --harness $H --industry $I
uv run python run.py --harness $H --industry $I --build --apply --no-logs   # or per DEPLOY.md
uv run python .claude/skills/mivas-run/scripts/preflight.py --harness $H --industry $I --k8s   # or --url wss://…
uv run python .claude/skills/mivas-run/scripts/bluejay.py smoke --harness $H --industry $I --url wss://HOST
uv run python .claude/skills/mivas-run/scripts/verify_integrity.py --harness $H --industry $I --smoke-dir verify-out/smoke/<slug>/<run> --k8s
uv run python .claude/skills/mivas-run/scripts/bluejay.py full --harness $H --industry healthcare --runs 5
```

## Rules

- **Rung 0 first, always.** Never `--apply` with a missing provider key or `BLUEJAY_API_KEY`: the pod syncs `mivas-secrets` from `.env`, and a missing Bluejay key means no traces, which fails the rubric.
- **Start with `control-industry`.** It is the wiring smoke (reception → scheduler → `schedule_appointment`). Only when it passes rung 2 do you point the same harness at `healthcare`, `legal`, or `customer-support`.
- **Rubric, not `goal_success`.** A call passes when all six checks pass: utterances, actual tool calls, trace ids, dead air ≤ 25 s, zero dropouts/clipping, first agent audio ≤ 3 s. `goal_success` is informational. Details and the fix loop: [BLUEJAY.md → Rubric](BLUEJAY.md#rubric).
- **Read one transcript** per smoke even when the rubric passes (greeting complete, handoff continues the intent, booking date plausible).
- **Never redeploy mid-run.** In-flight CHIRP sockets die on pod restart. Wait for the run to be final.
- **Concurrency** is `max_concurrent ≤ MIVAS_REPLICAS × 2–4` for CHIRP families. Smoke defaults to 1.
- **Storage before rung 4.** Without `MIVAS_SNAPSHOT_BUCKET` (AWS S3 or any S3-compatible endpoint), `verify_task_run.py` skips the final-state check and Pass@1 / Pass^k are not reportable.
- **Secrets stay out of the tree.** Report ids and URLs in chat; never commit `.env`, hostnames, or keys.

## Rung 4: full industry

`bluejay.py full` converts `industries/<I>/tasks/*/task.json` to digital humans on a fresh
simulation (via `scripts/tasks_to_digital_humans.py --push`), queues `k` runs per task, and
prints the verifier + CSV commands. Budget: 72 tasks × k calls of up to 8 minutes; with
`--max-concurrent 3` and `k=5` expect several hours. `--no-wait` returns the run id;
resume with `bluejay.py poll --run RUN`. Score with `verifiers/verify_task_run.py` (tools ∧
handoff ∧ final state), export with `scripts/bluejay_run_to_csv.py`. Do not quote a number
from a run with void results (`verifiers/verify_run.py` exit 1).

## Files

| File | When |
|---|---|
| [DEPLOY.md](DEPLOY.md) | rung 1: storage model, registry, local/EKS/Baseten/any-k8s recipes, DNS+TLS |
| [HARNESS.md](HARNESS.md) | picking a harness, env keys, building and registering your own |
| [BLUEJAY.md](BLUEJAY.md) | REST calls the scripts make, API quirks, rubric, reading results, fix loop |
| `scripts/preflight.py` | rung 0/1 gate |
| `scripts/bluejay.py` | `ensure-agent` · `smoke` · `queue` · `poll` · `results` · `score` · `full` |
| `scripts/verify_integrity.py` | rung 3 |
| `scripts/score_rubric.py` | six-check rubric (stdlib; `--self-test`) |
| `assets/` | `minio.yaml` (in-cluster S3), `baseten/config.yaml`, `baseten/auth-proxy-worker.js` |
