# aws

Amazon Nova 2 Sonic harnesses. Each subfolder is one model runtime.

| Folder | Model |
|---|---|
| `nova-sonic-2/` | [`amazon.nova-2-sonic-v1:0`](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-getting-started.html) (Bedrock `InvokeModelWithBidirectionalStream`) |

Shared builder: `harness.py`. Tracing: `report.py` (`BLUEJAY_SERVICE_NAME=mivas-aws`, provider `aws.bedrock`).

- Industry tools → `POST {TOOL_SERVER_URL}/tools/{name}`
- Session tools (`session: true`, e.g. `end_call`) → harness-local + delayed close
- Handoffs → new Bedrock stream for the target agent (tools are fixed at `promptStart`); seed prior ASR so the specialist does not cold-open
- Speak-first: open USER audio stream, feed silent PCM keepalive, then interactive USER text (pack owns greeting text)
- Audio: Nova PCM 16 kHz in / 24 kHz out ↔ CHIRP 16 kHz `pcm_s16le`
- Barge-in: provider interrupted event (never mute on CHIRP VAD alone)
- Clock: pack `TODAY` (not the wall clock) injected as `Today is …` on every session
- Tracing → LangSmith-shaped Bluejay OTel `realtime_session → turn → {user_message, model, execute_tool}`; the `model` span carries Nova's `usageEvent` token breakdown (speech/text, per-turn delta) + time-to-first-token. Chirp stamps `X-Simulation-Result-Id`
- Concurrency: the CHIRP parent accepts TCP and starts one Python process per call. Amazon's Bedrock streaming library shares state across a whole process, so two calls in one interpreter cancel each other. One container still takes many calls; CPU and memory bound the count.

Nova will not speak from silence alone (the stream stays up but only emits usage events). The keepalive is the same constraint as hosted VoiceChat duplex — zero-PCM keeps the input channel alive; the opening nudge is interactive text, not a kick WAV.

```bash
uv sync
uv run python run.py --harness aws/nova-sonic-2 --mode check
uv run python industries/control-industry/tool_server.py
CHIRP_PORT=8774 CHIRP_USER=mivas CHIRP_PASS=mivas \
  BLUEJAY_SERVICE_NAME=mivas-aws \
  uv run python run.py --harness aws/nova-sonic-2 --mode chirp
```

## Env

| var | use |
|---|---|
| `NOVA_SONIC_MODEL` | default `amazon.nova-2-sonic-v1:0` |
| `NOVA_SONIC_REGION` | Bedrock region (default `us-east-1`; do not reuse `AWS_DEFAULT_REGION` if that is the snapshot region) |
| `NOVA_SONIC_VOICE` | default `matthew` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | optional; otherwise boto3 default chain (profile / IRSA) is copied into env for the Bedrock SDK |
| `TOOL_SERVER_URL` | industry state API |
| `BLUEJAY_API_KEY` | OTel + `update-simulation-result` |
| `CHIRP_USER` / `CHIRP_PASS` / `CHIRP_PORT` | websocket auth (default port `8774`) |
