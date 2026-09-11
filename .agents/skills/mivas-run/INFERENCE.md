# Rung 1: get the model served

This is its own setup step, finished and verified **before** any container or cluster work.
Two mutually exclusive paths. Pick from the interview answer to question 2.

| Path | When | Ends with |
|---|---|---|
| **A — vendor API** | the model is one of the providers already wired up | a provider API key in `.env` |
| **B — your own endpoint** | your own weights, or an open-weight model you want to benchmark | a base URL + key, proven with `curl`, reachable from the container |

## Path A — vendor API

One key per family. Put it in `.env` at the repo root; `run.py --apply` and the Railway and
local flows all read from there.

| `family/runtime` | Where the key comes from | `.env` |
|---|---|---|
| `openai/realtime-2.1`, `openai/realtime-2.1-mini` | platform.openai.com → API keys | `OPENAI_API_KEY` |
| `openai/gpt-live-1` | same key, but the account needs GPT-Live access | `OPENAI_API_KEY` |
| `gemini/flash-live-3.1`, `gemini/2.5-flash-native-audio` | Google AI Studio key **plus** a LiveKit Cloud project (these are SIP workers, not CHIRP) | `GOOGLE_API_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| `aws/nova-sonic-2` | AWS account with Bedrock access to Nova Sonic 2 in the chosen region | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`, `NOVA_SONIC_REGION` |
| `grok/voice` | console.x.ai | `GROK_API_KEY` (alias `XAI_API_KEY`) |
| `qwen/audio-realtime` | Alibaba Model Studio / DashScope | `DASHSCOPE_API_KEY`, `QWEN_WORKSPACE_ID`, `QWEN_REGION` |
| `nvidia/nemotron`, `nvidia/nemotron-voicechat` | build.nvidia.com — hosted NVCF serves the LLM, ASR and TTS together | `NVIDIA_API_KEY` |
| `livekit/cascaded` | four vendors: Deepgram (STT), OpenAI (LLM), ElevenLabs (TTS), LiveKit Cloud | `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `LIVEKIT_*` |

Every container also needs `BLUEJAY_API_KEY` (trace upload and result relink) and
`CHIRP_USER` / `CHIRP_PASS`. A missing Bluejay key means no traces, and no traces fails
rubric checks 2 and 3 even when the call sounds perfect.

Verify a key before moving on, for example:

```bash
curl -s https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" | head -5
```

Path A ends here. Go to [HOSTING.md](HOSTING.md).

## Path B — your own model on an inference provider

MIVAS never runs model weights inside the harness container. The container is a voice
bridge; the model is a separate service it calls. So: deploy the model somewhere that gives
an **OpenAI-compatible HTTPS endpoint**, then point a cascaded harness at it.

Providers that do this: Baseten, Modal, Together, Fireworks, RunPod, or anything else that
serves `/v1/chat/completions`. Baseten is the default recommendation because it has both a
ready-made catalog and custom deployments.

**Baseten, two options.**

- *Model APIs* — an already-served catalog model (Nemotron, Qwen, GLM, DeepSeek and others).
  Base URL `https://inference.baseten.co/v1`, model name = the catalog slug. Nothing to deploy.
- *Dedicated deployment* — your own weights, pushed with Truss (`pip install truss`, then
  `truss push`). Base URL `https://model-{id}.api.baseten.co/environments/production/sync/v1`.
  Chat needs the `/sync/v1` form.

Either way the key is your Baseten API key.

**Prove it before wiring anything:**

```bash
curl -s $BASE_URL/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"<model>","messages":[{"role":"user","content":"say ok"}],"max_tokens":5}'
```

A non-200, a cold-start of tens of seconds, or a wrong model name here will surface later as
dead air in the rubric and look like a model failure. Fix it now.

### Wiring the endpoint into a harness

Only cascaded harnesses have a separable LLM leg. The speech-to-speech families talk one
proprietary protocol and cannot be repointed.

**`nvidia/nemotron`** reads the LLM endpoint from the environment:

```dotenv
NEMOTRON_LLM_BASE_URL=https://inference.baseten.co/v1
NEMOTRON_LLM_MODEL=<the model name that endpoint serves>
NVIDIA_API_KEY=<your Baseten key>      # see the constraint below
```

> **Constraint, verified in `voice-agent-harnesses/nvidia/harness.py`.** One variable,
> `NVIDIA_API_KEY`, authenticates *both* the LLM client and the Riva ASR/TTS gRPC calls. So
> **LLM on Baseten while ASR and TTS stay on NVIDIA NVCF is not possible** without a code
> change. Two workable setups: everything on NVIDIA (path A, recommended), or the LLM on your
> provider **and** your own Riva ASR/TTS NIMs via `NEMOTRON_ASR_SERVER`, `NEMOTRON_TTS_SERVER`,
> `NEMOTRON_USE_SSL=false`, where the shared key value is ignored by your own servers.

**`livekit/cascaded`** builds its LLM leg with the OpenAI SDK, which honours `OPENAI_BASE_URL`.
Setting `OPENAI_BASE_URL` and `OPENAI_API_KEY` to your provider should send only the LLM leg
there, leaving Deepgram for STT and ElevenLabs for TTS. **This path is untested in this repo** —
verify on `control-industry` before trusting a scored run.

If the model you want to benchmark is speech-to-speech and has no harness, that is a harness
build, not a configuration change: see [HARNESS.md](HARNESS.md#build-your-own).

### Rules for path B

- **Same region as the container.** Voice is latency-bound; the rubric fails a call whose
  first agent audio takes over 3 seconds or that goes quiet for 25.
- **Keep the endpoint warm.** Scale-to-zero cold starts read as dead air. Set a minimum
  replica, or make one warm-up call right before queueing a run.
- **No laptop endpoints for a hosted container.** `localhost` and LAN addresses are only valid
  when the container also runs locally.
- **Record what you ran.** Endpoint, model name and revision belong in the run notes; a MIVAS
  score is meaningless without them.

Path B ends with a curl-verified base URL, a key and a model name. Go to [HOSTING.md](HOSTING.md).
