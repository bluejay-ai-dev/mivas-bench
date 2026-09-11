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

# mivas-run (Claude Code shim)

The skill is agent-agnostic and lives in `.agents/skills/mivas-run/`. Read
`.agents/skills/mivas-run/SKILL.md` now and follow it exactly; every path in it is
relative to the repository root. Do not duplicate content here.
