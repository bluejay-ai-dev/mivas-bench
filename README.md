# mivas-bench

Multi-Industry Voice Agent Simulation Bench (MIVAS) evaluates voice agent performance across the industries that are adopting voice AI, inspired by the real ways they deploy voice agents. Today, voice agents are deployed as multi-agent systems with access to database state, external tools, and internal agent handoffs.

## Runtime model

Pick a **harness** from `voice-agent-harnesses/` (e.g. `openai/realtime-2.1`) and an **industry** from `industries/`. Each industry owns:

- `agent_blueprint.json`, `tools.json`, system prompts
- SQLite `db/schema.sql` + `db/seed.sql`
- `tool_server.py` (FastAPI **state API** over SQLite — not a 1:1 tools.json mirror)

The harness adapts the blueprint into a voice runtime:
- **industry** tools → industry state API
- **session** tools (`session: true`, e.g. `end_call`) → harness-native + hang up
- **handoff** tools → provider handoffs

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

## Kubernetes (CHIRP for Bluejay)

`--apply` deploys a long-running **Deployment + Service** per harness×industry pair (industry tool server + CHIRP WebSocket adapter for Bluejay).

### Stable public URLs on EKS (recommended)

This cluster is **EKS Auto Mode** in `us-west-1`. Cloudflare holds DNS for `getbluejay.ai`; AWS ACM + one shared ALB terminate TLS. There is no Route53 hosted zone and no self-managed AWS Load Balancer Controller.

Each pair gets a **deterministic** hostname that stays the same across redeploys:

`{slug}.{MIVAS_BASE_DOMAIN}`  
e.g. `openai-realtime-2-1-healthcare.benchmarks.getbluejay.ai`

| Use | URL |
|-----|-----|
| Bluejay `websocket_url` | `wss://{slug}.{domain}` |
| `PUBLIC_URL` (Vapi/Retell tool webhooks) | `https://{slug}.{domain}` |

```bash
# .env (EKS Auto Mode, us-west-1)
MIVAS_BASE_DOMAIN=benchmarks.getbluejay.ai
MIVAS_ACM_CERTIFICATE_ARN=arn:aws:acm:us-west-1:148660429236:certificate/6e3690bc-d776-40b0-8ca7-5741e648c5c8
MIVAS_IMAGE_PREFIX=148660429236.dkr.ecr.us-west-1.amazonaws.com/mivas-bench

# ACM cert must be ISSUED first (Cloudflare validation CNAME). Then:
uv run python run.py --build --apply --no-logs
```

`--build` with `MIVAS_IMAGE_PREFIX` builds `linux/arm64`, logs into ECR, and pushes. `--apply` creates IngressClassParams + one Ingress per pair; Auto Mode provisions **one** internet-facing ALB (`group.name: mivas-chirp`).

**Cloudflare (two records, both DNS-only / grey cloud — never orange-cloud proxy):**

1. ACM validation CNAME (once, until the cert is `ISSUED`).
2. After `--apply`, wildcard `*.benchmarks.getbluejay.ai` CNAME → the ALB hostname from `kubectl get ingress`.

Orange-cloud proxy idle-timeouts kill long CHIRP WebSockets. Grey cloud means Cloudflare is only DNS; TLS is ACM on the ALB.

### Local / kind (no stable DNS)

**Single pair** — `HARNESS` + `INDUSTRY` in `.env` (or `--harness` / `--industry`):

```bash
# Ensure a cluster context (kind, minikube, Docker Desktop, …)
# Builds voice-agent-harnesses/<family>/<runtime>/Dockerfile
# kind: kind load docker-image mivas-bench:openai-realtime-2-1-control-industry
uv run python run.py --build --apply --no-logs
```

**Platform harnesses (Vapi / Retell / …):** need HTTPS `PUBLIC_URL` for `/tool/*`. On EKS that is set automatically from `MIVAS_BASE_DOMAIN`. Locally, tunnel (e.g. cloudflared) and set `PUBLIC_URL`.

**Multiple pairs** — set `AGENTS` in `.env` (overrides single-run vars). Each entry becomes its own Deployment + Service (+ Ingress when `MIVAS_BASE_DOMAIN` is set):

```bash
# AGENTS=openai/realtime-2.1:healthcare,nvidia/nemotron:control-industry
uv run python run.py --build --apply --no-logs
# → kubectl get deploy,svc,ingress -l app=mivas-bench
```

Auth: `CHIRP_USER` / `CHIRP_PASS` from `mivas-secrets` (default `mivas`/`mivas`).

Without `MIVAS_BASE_DOMAIN`, Service type defaults to `LoadBalancer` (or `NodePort` + `MIVAS_NODE_HOST`).

Manifests: `k8s/deployment.yaml`, `k8s/service.yaml`, `k8s/ingress.yaml`, `k8s/secret.example.yaml`. One-shot blueprint checks: `k8s/job.yaml` (`MIVAS_MODE=check`).
