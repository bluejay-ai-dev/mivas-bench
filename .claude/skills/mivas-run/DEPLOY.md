# Deploying a MIVAS pair

One **pair** = one harness (`family/runtime`) × one industry pack, built into one image,
run as one Deployment. The container runs the industry tool server on `:8000` and the
CHIRP WebSocket bridge on `:8765` in the same pod. Bluejay dials the CHIRP port over
`wss://` with basic auth (`CHIRP_USER` / `CHIRP_PASS`, default `mivas`/`mivas`).

`run.py` renders `k8s/*.yaml` from `.env` and applies it. It also creates or refreshes
the `mivas-secrets` Secret from whichever provider / Bluejay keys are in your `.env`.

```
Bluejay ──wss──▶ ingress / LB / tunnel ──▶ Service mivas-<slug>:8765 ──▶ pod
                                                                          ├─ CHIRP bridge (harness)
                                                                          ├─ tool server :8000 (industry)
                                                                          └─ /data/calls/<result_id>.db → snapshot store
```

slug = `family-runtime-industry` lowercased with `/`, `.`, `_` → `-`
(`openai/realtime-2.1` × `healthcare` → `openai-realtime-2-1-healthcare`).

## Storage

Every Bluejay conversation gets its **own SQLite file**, created from the pack's
`schema.sql` + `seed.sql` on first tool call and keyed by the Bluejay simulation result id
(`X-Simulation-Result-Id` → `X-Mivas-Call-Id`). It lives in the pod's `emptyDir` at
`/data/calls/<id>.db`. At hangup the harness freezes `GET /state` to
`<id>.final.json` and, when `MIVAS_SNAPSHOT_BUCKET` is set, PUTs both files to
`s3://$MIVAS_SNAPSHOT_BUCKET/$MIVAS_SNAPSHOT_PREFIX/<slug>/<id>.final.json`.

The verifiers read **that object**, never the pod, because with replicas > 1 the
public hostname lands on a random pod. Pick one:

| Store | Pod env | Laptop env for verifiers | Notes |
|---|---|---|---|
| AWS S3 | `MIVAS_SNAPSHOT_BUCKET`, `AWS_DEFAULT_REGION`; creds via EKS Pod Identity / IRSA (`MIVAS_IRSA_ROLE_ARN`) or `AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY` in `.env` | same bucket, your AWS creds | the reference setup |
| Any S3-compatible (MinIO, Cloudflare R2, GCS interop, DO Spaces) | same plus `AWS_ENDPOINT_URL_S3=https://…` and that store's key pair | `AWS_ENDPOINT_URL_S3` pointing at the same store (for in-cluster MinIO: `kubectl port-forward svc/minio 9000:9000` → `http://127.0.0.1:9000`) | boto3 honours `AWS_ENDPOINT_URL_S3`; no code change |
| None | leave `MIVAS_SNAPSHOT_BUCKET` unset | – | `.final.json` stays in the pod; rung 3 checks it via `kubectl exec`; rung 4 state scoring is skipped |

In-cluster MinIO for local / own clusters: `kubectl apply -f .claude/skills/mivas-run/assets/minio.yaml`
(20 Gi PVC, bucket `mivas` created by a Job, root creds `mivas`/`mivasmivas` — change them),
then in `.env`:

```dotenv
MIVAS_SNAPSHOT_BUCKET=mivas
AWS_ENDPOINT_URL_S3=http://minio:9000
AWS_ACCESS_KEY_ID=mivas
AWS_SECRET_ACCESS_KEY=mivasmivas
AWS_DEFAULT_REGION=us-east-1
```

`run.py --apply` copies those into `mivas-secrets`. Run verifiers on the laptop with the
port-forward URL exported instead. All pods must be able to reach the store: the entrypoint
runs `snapshot.preflight()` and logs `NO AWS CREDENTIALS` if not.

## Images and registry

| Target | Build | `.env` |
|---|---|---|
| Docker Desktop k8s | `run.py --build` (host arch) | no prefix; `imagePullPolicy: IfNotPresent` |
| kind / minikube | `run.py --build` then `kind load docker-image mivas-bench:<slug>` (`minikube image load …`) | no prefix |
| Any registry | `MIVAS_IMAGE_PREFIX=ghcr.io/you/mivas-bench` (or ECR, Docker Hub, GAR) → `run.py --build` does `buildx --platform linux/amd64 --push`; `docker login` first (ECR login is automatic) | `MIVAS_IMAGE_PLATFORMS` to override arch |
| Private registry pull | `kubectl create secret docker-registry regcred …` then `kubectl patch serviceaccount mivas-bench -p '{"imagePullSecrets":[{"name":"regcred"}]}'` | – |
| AWS CodeBuild fleet | `run.py --codebuild` builds one image per `AGENTS` pair in your AWS account (creates ECR repo, S3 source bucket, CodeBuild project + roles) | `MIVAS_IMAGE_PREFIX` = your ECR |

The image copies `industries/<I>/`, `voice-agent-harnesses/<family>/` and `runtime/`.
Rebuild after touching any of those.

## Target: local Kubernetes (+ tunnel)

Bluejay dials from the internet, so a local cluster needs a public `wss://` in front.

```bash
# .env: HARNESS, INDUSTRY, provider key, BLUEJAY_API_KEY; leave MIVAS_BASE_DOMAIN empty
MIVAS_SERVICE_TYPE=LoadBalancer uv run python run.py --build --apply --no-logs   # Docker Desktop: localhost:8765
# kind/minikube: kind load docker-image mivas-bench:<slug>; MIVAS_SERVICE_TYPE=NodePort MIVAS_NODE_HOST=127.0.0.1
kubectl port-forward svc/mivas-<slug> 8765:8765        # if the LB has no address
cloudflared tunnel --url http://127.0.0.1:8765 --no-autoupdate   # prints https://xxx.trycloudflare.com
```

Use `wss://xxx.trycloudflare.com` as `--url` for preflight and `bluejay.py smoke`. The quick
tunnel URL changes on every restart; rerun `bluejay.py ensure-agent --url …`. A named
Cloudflare tunnel or ngrok domain gives a stable one. Snapshot store: MinIO above, or none.

No cluster at all? `uv run python run.py` runs the tool server + CHIRP locally on `:8765`;
the same tunnel makes it dialable. Good for harness development, not for a full run.

## Target: EKS

**Auto Mode (reference).** `k8s/ingressclass.yaml` + `k8s/ingress.yaml` create one shared
internet-facing ALB (group `mivas-chirp`, idle timeout 3600 s, least-outstanding-requests).

```dotenv
MIVAS_BASE_DOMAIN=mivas.example.com           # wildcard you control
MIVAS_ACM_CERTIFICATE_ARN=arn:aws:acm:…       # *.mivas.example.com, status ISSUED
MIVAS_IMAGE_PREFIX=<acct>.dkr.ecr.<region>.amazonaws.com/mivas-bench
AWS_DEFAULT_REGION=<region>
MIVAS_SNAPSHOT_BUCKET=<your-bucket>
```

1. `aws eks update-kubeconfig --name <cluster>`; make sure the cluster can create ALBs (Auto Mode does).
2. S3 access for pods: `aws eks create-pod-identity-association --cluster-name <c> --namespace default --service-account mivas-bench --role-arn <role with s3:PutObject/GetObject on the bucket>`; or IRSA with `MIVAS_IRSA_ROLE_ARN` (renders the annotation).
3. `uv run python run.py --build --apply --no-logs` (or `--codebuild --apply --no-logs`).
4. DNS: `kubectl get ingress -l app=mivas-bench -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}'` → wildcard `*.mivas.example.com` CNAME to it, **DNS-only** (a proxying CDN idle-times the sockets).
5. `preflight.py --k8s` must show `wss probe … 101`.

Pods set `karpenter.sh/do-not-disrupt` and roll with `maxUnavailable: 0` so consolidation
does not kill live calls. Scale with `MIVAS_REPLICAS=3` and re-apply (default 1).

**Classic EKS (AWS Load Balancer Controller, or no ALB controller).** The Auto Mode
`IngressClassParams` CRD does not exist there. Use the any-cluster path below with
`ingress-nginx` + `cert-manager` (works on EKS too), keeping Pod Identity / IRSA for S3.

## Target: Baseten

Baseten runs the same image as a **Custom Server** with a WebSocket transport. Two gaps
to bridge:

1. **Auth.** Baseten expects `Authorization: Bearer <BASETEN_API_KEY>`; Bluejay sends
   basic auth. Put `assets/baseten/auth-proxy-worker.js` in front (a Cloudflare Worker:
   checks `CHIRP_USER`/`CHIRP_PASS` basic auth, forwards the upgrade to Baseten with the
   bearer header, passes `X-Simulation-Result-Id` through). Bluejay dials the Worker URL.
   On Baseten leave `CHIRP_USER`/`CHIRP_PASS` empty so the bridge accepts the proxied socket.
2. **Health.** Baseten probes the port the socket listens on. The CHIRP bridges answer
   `GET /health` on `:8765` (`runtime/chirp_health.py`), so `readiness_endpoint: /health`
   works on `server_port: 8765`.

```bash
MIVAS_IMAGE_PREFIX=docker.io/<you>/mivas-bench uv run python run.py --build   # amd64 push
cp .claude/skills/mivas-run/assets/baseten/config.yaml ./baseten-config.yaml    # edit image, slug, env
pip install truss && truss push baseten-config.yaml --publish
```

Secrets arrive as files under `/secrets/<name>`; the template's `start_command` exports
them as env before `runtime/entrypoint.sh`. Snapshot store must be external (S3 / R2) with
static keys. One replica ≈ 2–4 concurrent calls; set Baseten `predict_concurrency`
accordingly. Worker families (`livekit`, `gemini`) do not apply here: they need SIP, not a
WebSocket ingress.

## Target: any Kubernetes (k3s, GKE, AKS, DOKS, on-prem)

Needs an ingress controller with WebSocket support and TLS. Set `MIVAS_INGRESS_CLASS` and
`run.py` renders `k8s/ingress-generic.yaml` (one Ingress per pair, 3600 s proxy timeouts)
instead of the EKS ALB pair; no ACM.

```bash
helm upgrade -i ingress-nginx ingress-nginx --repo https://kubernetes.github.io/ingress-nginx -n ingress-nginx --create-namespace
helm upgrade -i cert-manager cert-manager --repo https://charts.jetstack.io -n cert-manager --create-namespace --set crds.enabled=true
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
MIVAS_INGRESS_CLASS=nginx
MIVAS_CLUSTER_ISSUER=letsencrypt-prod      # or MIVAS_TLS_SECRET=<your wildcard TLS secret>
MIVAS_IMAGE_PREFIX=ghcr.io/you/mivas-bench
MIVAS_SNAPSHOT_BUCKET=mivas + AWS_ENDPOINT_URL_S3=http://minio:9000 (see Storage)
```

Then `run.py --build --apply --no-logs`, point wildcard DNS `*.mivas.example.com` at the
controller's external IP (`kubectl get svc -n ingress-nginx`), wait for the certificate
(`kubectl get certificate`), and `preflight.py --k8s`. HTTP-01 needs the DNS live before
the Ingress exists; otherwise use a DNS-01 solver or a bought wildcard cert via
`MIVAS_TLS_SECRET`.

Storage: MinIO from `assets/minio.yaml`, or an external S3-compatible bucket.

## After apply, before smoke

```bash
kubectl rollout status deployment/mivas-<slug> --timeout=180s
kubectl logs deployment/mivas-<slug> --tail=50      # "snapshot: preflight ok" and the CHIRP bind line ("ws↔…")
uv run python .claude/skills/mivas-run/scripts/preflight.py --harness $H --industry $I --k8s
```

Readiness probes `:8000/health` (tools), so a pod can be Ready a few seconds before CHIRP
listens; the preflight checks the log line. If `kubectl apply` says `unchanged` after a new
push on a mutable tag, `kubectl rollout restart deployment/mivas-<slug>`.

## Worker families (LiveKit SIP)

`livekit/*` and `gemini/*` register with a LiveKit Cloud project and take Bluejay SIP
inbound; they have no CHIRP ingress. Their Bluejay agent is `connection_type=LIVEKIT`, and
`bluejay.py ensure-agent` does not cover them: follow `voice-agent-harnesses/gemini/README.md`
and create the agent in the Bluejay app, then use `bluejay.py smoke --agent-id …`.
