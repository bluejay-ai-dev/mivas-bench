# mivas-bench

Multi-Industry Voice Agent Simulation Bench (MIVAS) evaluates voice agent performance across the industries that are adopting voice AI, inspired by the real ways they deploy voice agents. Today, voice agents are deployed as multi-agent systems with access to database state, external tools, and internal agent handoffs.

## Runtime model

Pick a **harness** from `voice-agent-harnesses/` (e.g. `openai/realtime-2.1`) and an **industry** from `industries/`.

Shipped harness runtimes: `openai/realtime-2.1`, `openai/realtime-2.1-mini`,
`gemini/flash-live-3.1`, `gemini/2.5-flash-native-audio`, `aws/nova-sonic-2`,
`grok/voice`, `qwen/audio-realtime`, `livekit/cascaded` (Deepgram Flux + GPT‑4.1 +
ElevenLabs), plus `nvidia/nemotron` and `nvidia/nemotron-voicechat` (WIP).
Industry packs: `healthcare`, `legal`, `customer-support` (72 scored tasks each in
`industries/<i>/tasks/`), and `control-industry` (minimal smoke pack, the default).

Each industry owns:

- `agent_blueprint.json`, `tools.json`, system prompts
- SQLite `db/schema.sql` + `db/seed.sql`
- `tool_server.py` (FastAPI **state API** over SQLite — not a 1:1 tools.json mirror)

The harness adapts the blueprint into a voice runtime:
- **industry** tools → industry state API (`POST /tools/{name}` with `X-Mivas-Call-Id` = Bluejay `X-Simulation-Result-Id`; see [industries/README.md](industries/README.md) and [voice-agent-harnesses/README.md](voice-agent-harnesses/README.md))
- **session** tools (`session: true`, e.g. `end_call`) → harness-native + hang up
- **handoff** tools → harness-native agent switches (never hit the tool server)

One container packs harness + industry + DB + state API. Each harness runtime has its own `voice-agent-harnesses/<family>/<runtime>/Dockerfile` (industry is a `--build-arg`).

## Quick start (local)

Requires [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env   # set HARNESS=openai/realtime-2.1, INDUSTRY, OPENAI_API_KEY
uv sync

uv run python run.py              # reads .env, starts tool server + agent
uv run python run.py --check      # blueprint wiring only
uv run python tests/converse.py   # speak to the agent (mic + speakers)
```

## Kubernetes

`--apply` deploys a long-running **Deployment + Service** per harness×industry pair (industry tool server + CHIRP WebSocket adapter).

### Stable public URLs (Ingress)

Each pair gets a **deterministic** hostname that stays the same across redeploys:

`{slug}.{MIVAS_BASE_DOMAIN}`  
e.g. `openai-realtime-2-1-healthcare.chirp.example.com`

| Use | URL |
|-----|-----|
| Evaluator `websocket_url` (CHIRP pairs) | `wss://{slug}.{domain}` |
| Worker pairs' public `/tools` host | `https://{slug}.{domain}/tools` |

```bash
# .env — fill from your cluster (do not commit live values)
MIVAS_BASE_DOMAIN=chirp.example.com
MIVAS_ACM_CERTIFICATE_ARN=arn:aws:acm:REGION:ACCOUNT:certificate/ID
MIVAS_IMAGE_PREFIX=ACCOUNT.dkr.ecr.REGION.amazonaws.com/mivas-bench

# ACM cert must be ISSUED first. Then:
uv run python run.py --codebuild --apply --no-logs
```

`--codebuild` zips the repo to S3 and starts a CodeBuild batch: one `linux/amd64` child per `AGENTS` pair, registry cache per family (`:cache-<family>`), push to ECR. `--apply` waits for the batch then kubectl-applies. Local `--build` is the fallback (also amd64 by default; override with `MIVAS_IMAGE_PLATFORMS`). `--apply` creates IngressClassParams + one Ingress per pair; Auto Mode provisions **one** internet-facing ALB (`group.name: mivas-chirp`).

`MIVAS_REPLICAS` (default `1`) sets `spec.replicas` on each pair’s **one** Deployment. The industry tool server runs **in that pod** (`TOOL_SERVER_URL=http://127.0.0.1:8000`). Capacity is:

```
max_concurrent ≈ replicas × in_process_ws_limit
```

`in_process_ws_limit` is empirical per family (start conservative: 2–4). Leave unused pairs at 1. Re-apply a scaled pair with `MIVAS_REPLICAS=3` or you will scale it back to 1 and drop in-flight sockets.

At hangup the replica that owned the WebSocket PUTs `{id}.final.json` and `{id}.db` to `s3://$MIVAS_SNAPSHOT_BUCKET/mivas/{slug}/{id}…`. Evals read S3. Do not `GET https://{host}/state` or `/snapshot` — ALB would pick a random replica.

Rolling updates use `maxUnavailable: 0` / `maxSurge: 1`. An in-flight WebSocket **dies** if its pod is deleted; scale by adding replicas rather than cycling them mid-run. New dials use ALB `least_outstanding_requests`; an upgraded socket stays on that target for the TCP lifetime (idle timeout 3600s). Cookie stickiness is not used.

After `--apply`, point a wildcard DNS CNAME `*.{MIVAS_BASE_DOMAIN}` at the ALB hostname from `kubectl get ingress`. TLS terminates on the load balancer (ACM). Do not put a CDN or proxy in front of CHIRP WebSockets — idle timeouts kill long-lived connections.

### Local / kind (no stable DNS)

**Single pair** — `HARNESS` + `INDUSTRY` in `.env` (or `--harness` / `--industry`):

```bash
# Ensure a cluster context (kind, minikube, Docker Desktop, …)
# Builds voice-agent-harnesses/<family>/<runtime>/Dockerfile
# kind: kind load docker-image mivas-bench:openai-realtime-2-1-control-industry
uv run python run.py --build --apply --no-logs
```

**Multiple pairs** — set `AGENTS` in `.env` (overrides single-run vars). Each entry becomes its own Deployment + Service (+ Ingress when `MIVAS_BASE_DOMAIN` is set):

```bash
# AGENTS=openai/realtime-2.1:healthcare,nvidia/nemotron:control-industry
uv run python run.py --codebuild --apply --no-logs
# → kubectl get deploy,svc,ingress -l app=mivas-bench
```

Auth: `CHIRP_USER` / `CHIRP_PASS` from `mivas-secrets` (default `mivas`/`mivas`).

Without `MIVAS_BASE_DOMAIN`, Service type defaults to `LoadBalancer` (or `NodePort` + `MIVAS_NODE_HOST`).

Manifests (rendered by `run.py --apply` via `__PLACEHOLDER__` substitution): `k8s/deployment.yaml`, `k8s/service.yaml`, `k8s/ingress.yaml`, `k8s/ingress-tools.yaml`, `k8s/ingressclass.yaml`. `k8s/secret.example.yaml` documents the Secret that `--apply` builds from env.

## Tasks and scoring

Each scored industry carries a locked 72-case pack in `industries/<i>/tasks/<KEY>/task.json` — expected tool calls, expected handoff path, and `exp_db_state` (the exact database state a correct call leaves behind, produced by replaying the expected calls against a fresh seeded tool server). Legal and customer-support tasks are generated from `scripts/{legal,customer_support}_task_spec.py` via `scripts/encode_*_tasks.py`; `scripts/tasks_to_digital_humans.py --push` publishes the pack as Bluejay digital humans. After a run, `scripts/verify_task_run.py` scores tool adherence ∧ handoff path ∧ hangup DB state, and `scripts/bluejay_run_to_csv.py` exports one row per conversation with costs priced from `voice-agent-harnesses/s2s-model-pricing.json`.
