---
name: mivas-run
description: >-
  Walks a consumer through running MIVAS Bench end to end, interview first: what model
  they are benchmarking, where the model is served (vendor API, or their own endpoint on
  an inference provider), and where the Docker container runs (local, any Kubernetes, or
  any container hosting platform). Then a gated ladder: preflight, deploy, three Bluejay
  smoke calls, harness + benchmark integrity, full industry run. Use when someone says /mivas-run, "run MIVAS myself",
  "set up mivas-bench", "deploy a harness for MIVAS", "smoke my harness on Bluejay",
  "benchmark my voice model on healthcare/legal/customer-support", or asks how to plug
  their own voice agent or their own model into MIVAS.
argument-hint: "[model or family/runtime] [industry] [where to host]"
---

# MIVAS run (consumer)

Opinionated walkthrough. You interview the user, pick for them when they are unsure, and
refuse setups that cannot work. Everything uses the public Bluejay REST API with **their**
`BLUEJAY_API_KEY`, their inference, their hosting. Nothing internal to Bluejay.

## Step 0: interview. Do not run any command before it is done.

Ask with AskUserQuestion, one question at a time, in this order. Bracketed text is the
default you pick if they say "you choose" or do not know. Never silently assume anything else.

1. **What model are you benchmarking?**
   *(a)* a model already wired up — OpenAI Realtime 2.1 / 2.1-mini, GPT-Live 1, Gemini Live,
   Nova Sonic 2, Grok Voice, Qwen Audio Realtime, or the LiveKit cascaded baseline
   → vendor API. *(b)* **your own or an open-weight model** → it needs an inference endpoint
   you stand up first; only the cascaded harnesses can point at one. **[openai/realtime-2.1]**
   Map the answer to a `family/runtime` from [HARNESS.md](HARNESS.md); refuse a model with no harness
   and offer to build one.
2. **Where is that model served?** Vendor API (they hold a provider key) or their own
   endpoint on an inference provider. Route to [INFERENCE.md](INFERENCE.md) either way; case (b)
   above is a **separate setup step that must finish and be curl-verified before any hosting work**.
3. **Where will the container run?** Ask before deciding anything else about hosting, and
   take whatever they already use. MIVAS ships as a Docker image, so the only hard requirement
   is a platform that can **run a Linux/amd64 container** and give it **one public TLS endpoint
   that proxies WebSockets** to container port 8765. That includes their laptop, their own
   Kubernetes cluster, a managed Kubernetes service, a container platform like Railway, Fly,
   Render, Cloud Run or ECS, or a plain VM with Docker. **[Railway]** when they have no
   preference — it returns a `wss://` URL with TLS and no DNS work. Confirm the platform meets
   the two requirements in [HOSTING.md](HOSTING.md#what-any-host-must-provide); if it cannot, say which
   requirement fails and offer the nearest option that works.
4. **Which industry?** `control-industry` first, always, whatever they answer — it is the
   wiring smoke. Then `healthcare`, `legal`, or `customer-support` for scored work. **[healthcare]**
5. **How far this session?** Three smoke calls / smoke + integrity / all the way to a full
   industry run. **[smoke + integrity]** Say the cost of a full run before they pick it:
   72 tasks × k calls, several hours, and it needs snapshot storage.

Then confirm the plan back in four lines — model, inference, hosting, industry — and check
they hold: a Bluejay API key, the provider key or endpoint, a container registry if hosting
is Railway or AWS, and a snapshot store. Missing Bluejay key or provider credential is a
**stop**; missing snapshot store only downgrades scoring, so say so and continue.

## Two planes, set up in order

```
 inference plane            hosting plane                  Bluejay
 where the MODEL runs       where the CONTAINER runs
 vendor API ──────────┐     any platform that runs
 or your endpoint on  ├──▶  a Docker container     ──wss──▶ smoke calls → scoring
 an inference provider┘     (one pair per service)
```

Never collapse them. The inference endpoint must be reachable **from the container**, so a
model on the user's laptop is only valid when the container is also local.

## Ladder — each rung gates the next, no skipping

```
- [ ] 0 preflight      scripts/preflight.py              keys, pair on disk, --check, store
- [ ] 1 inference      INFERENCE.md                      vendor key, or endpoint + curl proof
- [ ] 2 hosting        HOSTING.md                        container up, public wss, 101 probe
- [ ] 3 smoke calls    scripts/bluejay.py smoke          3 calls, six-check rubric each
- [ ] 4 integrity      scripts/verify_integrity.py       INTEGRITY PASS
- [ ] 5 full industry  scripts/bluejay.py full           verify_task_run + CSV
```

From the repo root, with `H=family/runtime` and `I=industry`:

```bash
uv run python .agents/skills/mivas-run/scripts/preflight.py --harness $H --industry $I
# rungs 1-2 per INFERENCE.md then HOSTING.md, ending in a public wss:// URL
uv run python .agents/skills/mivas-run/scripts/preflight.py --harness $H --industry $I --url wss://HOST
uv run python .agents/skills/mivas-run/scripts/bluejay.py smoke --harness $H --industry $I --url wss://HOST
uv run python .agents/skills/mivas-run/scripts/verify_integrity.py --harness $H --industry $I --smoke-dir verify-out/smoke/<slug>/<run>
uv run python .agents/skills/mivas-run/scripts/bluejay.py full --harness $H --industry healthcare --runs 5
```

## Rules

- **Interview before commands.** A wrong hosting choice costs an hour of rework.
- **`control-industry` before any scored industry.** It proves greeting, handoff, tool call and state write in one two-minute call.
- **The rubric is the gate, never `goal_success`.** Six checks: utterances, actual tool calls, trace ids, dead air ≤ 25 s, zero dropouts and clipping, first agent audio ≤ 3 s. See [BLUEJAY.md](BLUEJAY.md#rubric).
- **Read one transcript per smoke** even when the rubric passes.
- **Never redeploy mid-run.** Live CHIRP sockets die with the container.
- **Concurrency** ≈ replicas × 2–4 calls. Smoke uses 1.
- **Storage before a full run.** Without a snapshot store, final-state scoring is skipped and Pass@1 / Pass^k are not reportable.
- **A slow inference endpoint fails the rubric, not the model.** Put inference in the same region as the container.
- **Secrets stay out of the tree.** Report ids and URLs in chat; never commit `.env` or keys.

## Rung 5: full industry

`bluejay.py full` turns `industries/<I>/tasks/*/task.json` into digital humans on a fresh
simulation, queues `k` runs per task, and prints the scoring commands. Budget 72 tasks × k
calls of up to 8 minutes. `--no-wait` returns the run id; resume with `bluejay.py poll --run RUN`.
Score with `verifiers/verify_task_run.py` (tools ∧ handoff ∧ final state), export with
`scripts/bluejay_run_to_csv.py`. Never quote a number from a run with void results
(`verifiers/verify_run.py` exits 1).

## Files

| File | When |
|---|---|
| [INFERENCE.md](INFERENCE.md) | rung 1: vendor keys per family, or your own model on an inference provider |
| [HOSTING.md](HOSTING.md) | rung 2: the host contract, worked recipes, storage |
| [HARNESS.md](HARNESS.md) | harness table, and building your own |
| [BLUEJAY.md](BLUEJAY.md) | REST calls, API quirks, the rubric, reading results |
| `scripts/preflight.py` | rung 0 and 2 gate |
| `scripts/bluejay.py` | `ensure-agent` · `smoke` · `queue` · `poll` · `results` · `score` · `full` |
| `scripts/verify_integrity.py` | rung 4 |
| `scripts/pair_env.py` | prints the exact `railway variables` command for a pair |
| `scripts/score_rubric.py` | six-check rubric (stdlib, `--self-test`) |
| `assets/minio.yaml` | in-cluster S3 for local or self-managed Kubernetes |
