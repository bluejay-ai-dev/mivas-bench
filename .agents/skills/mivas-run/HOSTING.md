# Rung 2: run the container

One **pair** = one harness (`family/runtime`) × one industry pack, built into one image, run
as one container. Inside it, the industry tool server listens on `:8000` and the CHIRP
WebSocket bridge on `:8765`. Bluejay dials the CHIRP port over `wss://` with basic auth
(`CHIRP_USER` / `CHIRP_PASS`, default `mivas` / `mivas`).

```
Bluejay ──wss──▶ your platform's TLS endpoint ──▶ container :8765  CHIRP bridge (harness)
                                                            :8000  tool server (industry)
                                                            /data/calls/<result_id>.db → snapshot store
```

slug = `family-runtime-industry`, lowercased, with `/`, `.` and `_` turned into `-`
(`openai/realtime-2.1` × `healthcare` → `openai-realtime-2-1-healthcare`).

## What any host must provide

Take whatever platform the user already has. It qualifies when it can do both of these:

1. **Run a Linux/amd64 Docker image** with environment variables you set, and outbound
   internet to the model provider and to Bluejay.
2. **Expose one public HTTPS/WSS endpoint** that proxies **WebSocket upgrades** to container
   port `8765`, with an **idle timeout of an hour or more**. Calls run for minutes on one
   socket; a 60-second proxy timeout kills them mid-conversation.

Nice to have, not required: a health check (point it at `/health` on the same port — the CHIRP
bridge answers it), persistent storage for `/data` (call databases are snapshotted out at
hangup, so losing the volume is survivable), and more than one replica for concurrency.

If a platform cannot do (2), it can still host the container behind a tunnel — see Local below.

## Recipes

Worked end to end here: **local**, **Railway**, **AWS (EKS)**, **any Kubernetes**. Anything
else follows the generic mapping at the bottom; the contract above is all that matters.

Common to all of them, first: build and push the image.

```bash
# local only (no registry): builds mivas-bench:<slug> for the host architecture
uv run python run.py --harness $H --industry $I --build

# anything remote: push to a registry the platform can pull
MIVAS_IMAGE_PREFIX=ghcr.io/<you>/mivas-bench \
  uv run python run.py --harness $H --industry $I --build     # buildx --platform linux/amd64 --push
```

`docker login` first for a private registry (ECR logs in automatically). The image copies
`industries/<I>/`, `voice-agent-harnesses/<family>/` and `runtime/`, so rebuild after editing
any of those. AWS users with many pairs can build them in parallel with `run.py --codebuild`.

### Local

Simplest path, and the right one for a first run or for harness development.

```bash
docker run --rm -p 8765:8765 \
  -e HARNESS=$H -e INDUSTRY=$I -e MIVAS_SLUG=<slug> \
  -e OPENAI_API_KEY -e BLUEJAY_API_KEY -e CHIRP_USER=mivas -e CHIRP_PASS=mivas \
  mivas-bench:<slug>
```

Swap `OPENAI_API_KEY` for whatever [INFERENCE.md](INFERENCE.md) gave you. Without Docker,
`uv run python run.py --harness $H --industry $I` runs the same two processes directly.

Bluejay dials from the internet, so put a tunnel in front and use its URL:

```bash
cloudflared tunnel --url http://127.0.0.1:8765 --no-autoupdate   # prints https://xxx.trycloudflare.com
```

Quick-tunnel URLs change on every restart; re-run `bluejay.py ensure-agent --url wss://…` after
each one, or use a named tunnel or an ngrok reserved domain for a stable address. Local
Kubernetes instead of plain Docker: `MIVAS_SERVICE_TYPE=LoadBalancer uv run python run.py --build --apply --no-logs`
(kind and minikube need `kind load docker-image` / `minikube image load` first), then port-forward
and tunnel the same way.

### Railway

No DNS, no certificates. Railway terminates TLS on a generated domain and proxies WebSockets.

1. Push the image to a registry (above). Make the package public, or give Railway the
   registry credentials when you create the service.
2. New project → **Deploy from Docker image** → your `…:<slug>` tag. (`railway add --help`
   shows the CLI equivalent; the dashboard flow is the one documented here.)
3. Variables: print the exact command for this pair and run it.
   ```bash
   uv run python .agents/skills/mivas-run/scripts/pair_env.py --harness $H --industry $I
   ```
4. **Settings → Networking → Generate Domain**, then set the domain's **target port to 8765**.
   Railway routes to `$PORT` when no target port is set, and this container listens on 8765.
5. Settings → **Health check path `/health`** (the CHIRP bridge answers it on 8765).
6. Your URL is `wss://<service>.up.railway.app`. Verify, then smoke:
   ```bash
   uv run python .agents/skills/mivas-run/scripts/preflight.py --harness $H --industry $I --url wss://<service>.up.railway.app
   ```

One replica handles roughly 2–4 concurrent calls; keep the simulation's `max_concurrent` at or
below that, or add replicas. Snapshot storage must be external — Cloudflare R2 or S3, below.

### AWS (EKS)

`run.py` renders the Deployment, Service and Ingress. On EKS Auto Mode the Ingress templates
create one shared internet-facing ALB (group `mivas-chirp`, idle timeout 3600 s, least
outstanding requests).

```dotenv
MIVAS_BASE_DOMAIN=mivas.example.com           # a wildcard you control
MIVAS_ACM_CERTIFICATE_ARN=arn:aws:acm:…       # *.mivas.example.com, status ISSUED
MIVAS_IMAGE_PREFIX=<acct>.dkr.ecr.<region>.amazonaws.com/mivas-bench
AWS_DEFAULT_REGION=<region>
MIVAS_SNAPSHOT_BUCKET=<your-bucket>
```

1. `aws eks update-kubeconfig --name <cluster>`.
2. Give pods S3 access for snapshots: `aws eks create-pod-identity-association --cluster-name <c>
   --namespace default --service-account mivas-bench --role-arn <role with s3:PutObject/GetObject>`,
   or use IRSA by setting `MIVAS_IRSA_ROLE_ARN` (it renders the annotation).
3. `uv run python run.py --harness $H --industry $I --build --apply --no-logs`.
4. DNS: `kubectl get ingress -l app=mivas-bench -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}'`,
   then point `*.mivas.example.com` at it as a **DNS-only** record. A proxying CDN in front will
   idle-timeout the call sockets.
5. `preflight.py --harness $H --industry $I --k8s` must report `wss probe … 101`.

Pods carry `karpenter.sh/do-not-disrupt` and roll with `maxUnavailable: 0`, so consolidation
does not kill live calls. Scale with `MIVAS_REPLICAS=3` and re-apply. Classic EKS without Auto
Mode has no `IngressClassParams` CRD — use the any-Kubernetes path below, keeping Pod Identity
or IRSA for S3.

### Any Kubernetes (k3s, GKE, AKS, DOKS, OpenShift, on-prem)

Needs an ingress controller that proxies WebSockets, plus TLS. Set `MIVAS_INGRESS_CLASS` and
`run.py` renders `k8s/ingress-generic.yaml` (one Ingress per pair, 3600-second proxy timeouts)
instead of the EKS ALB templates. No ACM involved.

```bash
helm upgrade -i ingress-nginx ingress-nginx --repo https://kubernetes.github.io/ingress-nginx \
  -n ingress-nginx --create-namespace
helm upgrade -i cert-manager cert-manager --repo https://charts.jetstack.io \
  -n cert-manager --create-namespace --set crds.enabled=true
kubectl apply -f - <<'Y'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: {name: letsencrypt-prod}
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef: {name: letsencrypt-prod}
    solvers: [{http01: {ingress: {class: nginx}}}]
Y
```

```dotenv
MIVAS_BASE_DOMAIN=mivas.example.com
MIVAS_INGRESS_CLASS=nginx                  # or traefik, or your controller's class
MIVAS_CLUSTER_ISSUER=letsencrypt-prod      # or MIVAS_TLS_SECRET=<your wildcard TLS secret>
MIVAS_IMAGE_PREFIX=ghcr.io/you/mivas-bench
```

Then `run.py --build --apply --no-logs`, point wildcard DNS at the controller's external IP
(`kubectl get svc -n ingress-nginx`), wait for `kubectl get certificate` to go Ready, and run
`preflight.py --k8s`. HTTP-01 validation needs the DNS live before the Ingress exists; a DNS-01
solver or a purchased wildcard certificate via `MIVAS_TLS_SECRET` avoids that ordering problem.

### Any other container platform

Fly, Render, Cloud Run, ECS/Fargate, Heroku, a DigitalOcean droplet, your own VM — all fine if
they meet the contract. Map these four settings and you are done:

| Setting | Value | Platform-specific trap |
|---|---|---|
| Container port | `8765` | many platforms route to `$PORT` unless you set the port explicitly |
| Protocol | HTTP/1.1 with WebSocket upgrade | some proxies need WebSockets switched on per service |
| Idle / request timeout | ≥ 3600 s | ALB defaults to 60 s; several PaaS proxies default to 60–300 s; Cloud Run caps at 60 min |
| Health check | `GET /health` on 8765 | do not TCP-probe the port and call it healthy |

Plus the environment variables for the pair (`pair_env.py --harness $H --industry $I` prints
the full list for any platform, whatever you paste it into) and, on a plain VM, a TLS reverse
proxy such as Caddy or nginx, or a named Cloudflare tunnel.

## Storage

Every conversation gets its **own SQLite file**, created from the pack's `schema.sql` and
`seed.sql` on first tool call and keyed by the Bluejay simulation result id
(`X-Simulation-Result-Id` → `X-Mivas-Call-Id`) at `/data/calls/<id>.db`. At hangup the harness
freezes `GET /state` to `<id>.final.json` and, when `MIVAS_SNAPSHOT_BUCKET` is set, uploads both
to `s3://$MIVAS_SNAPSHOT_BUCKET/$MIVAS_SNAPSHOT_PREFIX/<slug>/<id>.final.json`. The verifiers
read that object, never the container, because with several replicas the public hostname lands
on a random one.

| Store | Container env | Laptop env for the verifiers |
|---|---|---|
| AWS S3 | `MIVAS_SNAPSHOT_BUCKET`, `AWS_DEFAULT_REGION`; credentials from EKS Pod Identity / IRSA, or a key pair | same bucket, your AWS credentials |
| Any S3-compatible (Cloudflare R2, MinIO, GCS interop, DO Spaces) | same, plus `AWS_ENDPOINT_URL_S3=https://…` and that store's key pair | the same endpoint (for in-cluster MinIO: `kubectl port-forward svc/minio 9000:9000` → `http://127.0.0.1:9000`) |
| None | leave `MIVAS_SNAPSHOT_BUCKET` unset | final state stays in the container; rung 4 reads it over `kubectl exec` where it can, and full-run state scoring is skipped |

R2 is the easy answer off AWS: S3-compatible, cheap, one endpoint URL. For local or
self-managed Kubernetes, `kubectl apply -f .agents/skills/mivas-run/assets/minio.yaml` gives a
20 Gi MinIO with the bucket created and root credentials `mivas` / `mivasmivas` (change them),
then:

```dotenv
MIVAS_SNAPSHOT_BUCKET=mivas
AWS_ENDPOINT_URL_S3=http://minio:9000
AWS_ACCESS_KEY_ID=mivas
AWS_SECRET_ACCESS_KEY=mivasmivas
AWS_DEFAULT_REGION=us-east-1
```

Every replica must reach the store. The entrypoint runs a preflight and logs
`snapshot: NO AWS CREDENTIALS` when it cannot.

## Before the first smoke call

```bash
kubectl rollout status deployment/mivas-<slug> --timeout=180s     # Kubernetes
docker logs <container> | tail -50                                # anywhere else
```

Look for `snapshot: preflight ok` and the CHIRP bind line (`ws↔…`). On Kubernetes the readiness
probe watches the tool server on `:8000`, so a pod can report Ready a second or two before CHIRP
is listening; `preflight.py` checks the log line for exactly this reason. If `kubectl apply`
reports `unchanged` after pushing a new image to a mutable tag, run
`kubectl rollout restart deployment/mivas-<slug>`.

Then, whatever the platform:

```bash
uv run python .agents/skills/mivas-run/scripts/preflight.py --harness $H --industry $I --url wss://<host>
```

`wss probe … 101` is the gate. `401` means the Bluejay agent's username and password do not match
`CHIRP_USER` / `CHIRP_PASS`; anything else means the platform is not proxying the upgrade.

## SIP worker families

`livekit/*` and `gemini/*` register with a LiveKit Cloud project and take Bluejay SIP inbound
instead of serving CHIRP, so they need no public WebSocket endpoint at all — only outbound
internet. Their Bluejay agent is `connection_type=LIVEKIT`, which `bluejay.py ensure-agent` does
not create; follow `voice-agent-harnesses/gemini/README.md`, make the agent in the Bluejay app,
then run `bluejay.py smoke --agent-id <id>`.
