# MIVAS Bench: A Benchmark for Evaluating Voice AI Models in Multi-Agent Environments Across Industries

<p align="center">
  <img src="assets/mivas-benchmark-overview.svg" alt="MIVAS Bench evaluation process overview" width="1200">
</p>

## Multi-Industry Voice Agent Simulation Bench

*Measuring speech-to-speech (S2S) voice AI models in production-style multi-agent systems.*

[Benchmark comparison](#benchmark-comparison) · [Industries](#industries) · [Quick start](#quick-start) · [Methodology](#methodology)

MIVAS Bench evaluates S2S models inside stateful, multi-agent systems that follow policy, use tools, preserve state, and route work across specialists. Cascaded speech-to-text, language-model, and text-to-speech systems serve as baselines. This repository contains industry environments, production-style prompts, multi-agent blueprints, provider harnesses, evaluation cases, deterministic verifiers, and run artifacts.

## Benchmark comparison


| Benchmark            | **Multi-agent topology** | **Handoff verification** | **Conjunctive verification** | Live adaptive voice | Native S2S | Multi-industry coverage | Stateful tool execution | Deterministic final-state verifier | Tool-adherence verification | Repeated-run reliability support |
| -------------------- | ------------------------ | ------------------------ | ---------------------------- | ------------------- | ---------- | ----------------------- | ----------------------- | ---------------------------------- | --------------------------- | -------------------------------- |
| MIVAS Bench          | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> |
| EVA                  | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-partial.svg" alt="Partially supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> |
| τ-Voice              | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-partial.svg" alt="Partially supported" width="16" height="16"></div> |
| VAmoS Bench          | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-partial.svg" alt="Partially supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> |
| Full-Duplex-Bench v3 | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-partial.svg" alt="Partially supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-partial.svg" alt="Partially supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> |
| VoiceAgentBench      | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-partial.svg" alt="Partially supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-partial.svg" alt="Partially supported" width="16" height="16"></div> | <div align="center"><img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"></div> |

**Legend:** <img src="assets/benchmark-yes.svg" alt="Supported" width="16" height="16"> Yes · <img src="assets/benchmark-partial.svg" alt="Partially supported" width="16" height="16"> Partial · <img src="assets/benchmark-no.svg" alt="Not supported" width="16" height="16"> No


## Methodology

MIVAS models each industry as a network of specialists. Reception classifies requests, identity establishes authorization, and domain agents perform the work. Handoffs are scored behavior. Prompts resemble production prompts and are not shortened for a particular model.

Each case starts from known SQLite state. Industry tools access an isolated per-call database through FastAPI. Evaluation uses the transcript, tool sequence, provider-native handoff trace, and final state. Consequential actions must appear in final state.

A **harness** adapts a model or platform to the benchmark contract. An **industry pack** supplies agents, prompts, tools, policies, fixtures, and cases:

```text
voice agent harness + industry pack = benchmark runtime
```

The industry's `agent_blueprint.json` tells the harness how to compose prompts, tools, and handoffs. The runtime packages the harness, industry, database, and state service. Bluejay executes simulations and returns evaluation data; a public standalone invocation is not yet defined.

### Evaluation and verification

MIVAS evaluates complete interactions across these dimensions:


| Dimension              | What is evaluated                                                 |
| ---------------------- | ----------------------------------------------------------------- |
| Task completion        | Whether the caller's legitimate objective was achieved            |
| Tool use               | Tool selection, arguments, and required ordering                  |
| State accuracy         | Whether final state matches the authorized outcome                |
| Policy adherence       | Identity, disclosure, safety, privacy, and domain rules           |
| Handoff integrity      | Correct specialist routing with preserved context                 |
| Refusal and escalation | Safe handling of unauthorized or unsupported requests             |
| Conversation quality   | Intelligibility, responsiveness, pacing, and completeness         |
| Reliability            | Consistency across repeated runs                                  |
| Cost and latency       | Resources and response times required to complete the interaction |


Task correctness is conjunctive:

```text
database-state adherence AND handoff adherence AND tool adherence
```

- **Database-state adherence** compares final state with the expected outcome.
- **Handoff adherence** checks provider-native transfers that may produce no API call or database mutation.
- **Tool adherence** checks required industry calls, including read-only calls that final state cannot reveal.

Every applicable verifier must pass. Transcript, audio, latency, quality, and cost remain diagnostic evidence rather than substitutes for deterministic verification.

Digital Humans define caller identities, goals, constraints, and failure conditions. Cases cover ordinary and multi-step work, policy boundaries, refusals, escalation, ambiguity, and adversarial pressure. Repeated trials remain separate so reliability can be measured without hiding run-level failures.

Exports keep one conversation per row with task identity, component passes, state differences, transcript and trace data, latency, metrics, and estimated cost. Task correctness comes from the MIVAS verifier rather than Bluejay's general goal judge.

## Industries


| Industry                                         | Hypothetical organization | Principal challenge                                                            |
| ------------------------------------------------ | ------------------------- | ------------------------------------------------------------------------------ |
| [Control](industries/control-industry/)          | Bluejay's Repair Services | Minimal end-to-end wiring and appointment state                                |
| [Healthcare](industries/healthcare/)             | Straus Dermatology        | Identity, scheduling, coverage, billing, and bounded clinical support          |
| [Legal](industries/legal/)                       | Halverson & Reed          | Conflict screening, intake discipline, legal-advice boundaries, and scheduling |
| [Customer support](industries/customer-support/) | Kestrel Electronics       | Orders, returns, service, membership, fraud, and product safety                |


The three scored industries are Healthcare, Legal, and Customer Support. Control is setup-only: it verifies that a harness can receive a call, hand off, invoke a tool, and write expected state. New harnesses should pass Control before running a scored industry.

Each industry pack includes an agent blueprint, production-style prompts, tool schemas, deterministic seed data, a FastAPI tool service, and a handoff graph. Scored industries also include cases, Digital Humans, expected final state, and verification artifacts.

## Voice agent harnesses

Harnesses translate the MIVAS blueprint into provider-specific runtimes. Native S2S models are the primary systems under evaluation; cascaded systems provide baselines.


| Category                   | Harness families                                                                                                                                                                                                                              |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Realtime model APIs        | [OpenAI](voice-agent-harnesses/openai/), [Gemini](voice-agent-harnesses/gemini/), [xAI](voice-agent-harnesses/grok/), [Amazon Nova](voice-agent-harnesses/aws/), [Qwen](voice-agent-harnesses/qwen/), [NVIDIA](voice-agent-harnesses/nvidia/) |
| Voice model and agent APIs | [AssemblyAI](voice-agent-harnesses/assemblyai/), [Deepgram](voice-agent-harnesses/deepgram/), [ElevenLabs](voice-agent-harnesses/elevenlabs/)                                                                                                 |
| Voice platforms            | [Vapi](voice-agent-harnesses/vapi/), [Retell](voice-agent-harnesses/retell/), [Bland](voice-agent-harnesses/bland/), [Cartesia](voice-agent-harnesses/cartesia/), [Twilio](voice-agent-harnesses/twilio/)                                     |
| Orchestration frameworks   | [LiveKit](voice-agent-harnesses/livekit/), [Pipecat](voice-agent-harnesses/pipecat/)                                                                                                                                                          |


Support varies by runtime and industry; a listed family has an implemented runtime, but not every model or deployment mode has completed every suite. See the [harness contract](voice-agent-harnesses/README.md) for runtime requirements.

## Quick start



### Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/)
- a provider API key for the selected harness
- microphone and speaker access for local voice testing



### Install

```bash
git clone https://github.com/bluejay-ai-dev/mivas-bench.git
cd mivas-bench
uv sync
cp .env.example .env
```

Set a harness, industry, and the credentials required by that harness:

```dotenv
HARNESS=openai/realtime-2.1
INDUSTRY=control-industry
OPENAI_API_KEY=
```



### Validate the composition

```bash
uv run python run.py --check
```

This validates blueprint composition, not an official benchmark run.

### Speak to the agent

```bash
uv run python tests/converse.py
```

Begin with `control-industry`, ask to schedule a repair, and confirm the handoff and appointment state.

To run the selected tool server and agent directly:

```bash
uv run python run.py
```

See [tests/README.md](tests/README.md) for the text fallback and additional local checks.

## Deployment

Each harness and industry pair becomes a Kubernetes Deployment and Service:

```bash
uv run python run.py --build --apply --no-logs
```

For several pairs:

```dotenv
AGENTS=openai/realtime-2.1:healthcare,nvidia/nemotron:control-industry
```

```bash
uv run python run.py --codebuild --apply --no-logs
```

With `MIVAS_BASE_DOMAIN`, each pair receives:

```text
{harness-industry-slug}.{MIVAS_BASE_DOMAIN}
```

`X-Mivas-Call-Id` isolates each call. At hangup, the runtime can persist final state and SQLite data for evaluation.

Operational details, provider exceptions, ingress settings, and concurrency constraints are documented in the root `.env.example`, the [industry contract](industries/README.md), and the [harness contract](voice-agent-harnesses/README.md).

Industry packs live in `industries/`, provider adapters in `voice-agent-harnesses/`, shared state code in `runtime/`, verification utilities in `scripts/`, and generated exports in `eval_outputs/`.

## Reproducibility

MIVAS makes each score traceable:

- companies and records are fictional;
- schemas, seed data, prompts, tools, and handoff graphs are versioned;
- each call receives isolated state;
- consequential actions are checked against final state;
- repeated trials remain separate;
- exports preserve task, transcript, trace, latency, metric, and cost evidence.

Comparisons should identify the repository revision, harness, industry suite, model and provider versions, trial count, concurrency, call limit, evaluator, and any retries or exclusions.

## Limitations

Not every harness has completed every industry, and provider telemetry varies. Simulated callers and hypothetical organizations support reproducibility but do not represent every property of human speech or a sector's full operational surface. Do not compare scores across task, prompt, verifier, or runtime revisions without qualification.

## Contributing

Contributions may add harnesses, industries, policy cases, deterministic checks, measurements, or benchmark corrections.

New harnesses should first pass the control industry. New industry cases should state the initial fixture, caller goal, required conduct, prohibited conduct, expected tools, expected final state, and the evidence used for scoring.

## Citation

If you use MIVAS Bench in published work, cite the repository and the benchmark release used for the evaluation.

```bibtex
@misc{siddiqi2026mivasbench,
      title={MIVAS Bench: Multi-Industry Voice Agent Simulation Bench},
      author={Faraz Siddiqi and Yash Savalia},
      year={2026},
      url={https://github.com/bluejay-ai-dev/mivas-bench},
}
```



## License

No license has been specified for this repository.