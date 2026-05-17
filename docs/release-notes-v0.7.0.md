# corlinman v0.7.0 — Multi-Agent

## Headline

corlinman gains **parallel sibling agents** and a **self-evolving
prompt-variant scorer**. One user request can now decompose into
multiple specialist agents that run concurrently, coordinate through
a shared blackboard, and improve themselves over time based on what
historically worked.

The release is inspired by two open projects:

- [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
  — the "agent that grows with you" model: skills auto-distilled from
  past tasks, multi-agent orchestration. We adopt the philosophy and
  ship our own GEPA-lite scorer (deterministic, no LLM-judge).
- [openclaw/openclaw](https://github.com/openclaw/openclaw) — the
  pre-warmed container pool pattern for cutting cold-start latency.
  We adopt the warm-pool shape at the Python agent-runner layer
  (Phase C, in progress).

## What's new in this release

### Parallel sibling agents

- **`subagent.spawn_many` tool.** Orchestrator agents can fan out up to
  3 sibling subagents concurrently. Wall-clock time becomes
  `max(child_time)` instead of `sum(child_time)`. Existing per-parent
  concurrency cap (default 3) still gates live siblings; fan-outs that
  exceed the cap are rejected up-front with a clean args-invalid
  envelope.
- **Shared blackboard.** New `blackboard.read` / `blackboard.write`
  tools give sibling agents a trace-scoped, append-only sqlite
  scratchpad for coordination. Writes never overwrite; reads return
  the latest value at call time; trace isolation is the security
  boundary.
- **`agents/orchestrator.yaml`.** A new planner persona that
  decomposes → dispatches → reduces. Tool allowlist:
  `subagent.spawn`, `subagent.spawn_many`, `blackboard.read`,
  `blackboard.write`.

### Self-evolving prompt variants

- **GEPA-lite Pareto scorer** (`corlinman_evolution_engine.score_variants`).
  Given a list of candidate prompt-template variants and a sample of
  historical episodes, scores each variant on `(success_overlap,
  token_cost)` and returns the Pareto frontier. No LLM-judge, no DSPy
  dependency — deterministic token Jaccard against the episodes that
  already succeeded.
- All six evolution kinds remain enabled by default: `memory_op`,
  `tag_rebalance`, `skill_update`, `prompt_template`, `tool_policy`,
  `agent_card`. High-risk kinds continue to require operator approval
  (and, for `agent_card` / `tool_policy`, an additional meta-approver
  per `[admin].meta_approver_users`).

### Cold-start latency

- **BuildKit cache mounts** on cargo registry / git / `target/` and
  on uv's wheel cache. Cold first build is unchanged (~12 min);
  subsequent rebuilds on the same host with the same `Cargo.lock` drop
  to ~90 s. CI runners with persistent cache get the same win.
- **Pre-warmed Python agent runner pool** (OpenClaw-style)
  designed but **deferred to v0.7.1** — needs a new `corlinman-runner-pool`
  Rust crate and gateway lifecycle wiring that didn't fit in this
  release's window. The design is in `docs/multi-agent-release-plan.md` §2.3.

## Compatibility

- **Wire-stable tool names.** `subagent.spawn` remains; the new
  `subagent.spawn_many`, `blackboard.read`, `blackboard.write` are
  additive. Agents whose `tools_allowed` list omits the new names
  cannot call them; existing agents are unaffected.
- **Config.** No new required keys in `config.toml`. The orchestrator
  agent is opt-in via `agents/orchestrator.yaml` (already shipped in
  the release).
- **Database.** The blackboard table is `CREATE IF NOT EXISTS`'d on
  first use; no migration needed.
- **Python API.** `score_variants`, `EpisodeSample`, `VariantScore`,
  `dispatch_subagent_spawn_many`, `BlackboardStore` are new exports.
  Existing API surface unchanged.

## Migration

No action required for existing deployments. To start using the new
multi-agent capability:

1. Update to v0.7.0 binary / image.
2. Optionally register the orchestrator agent by ensuring
   `agents/orchestrator.yaml` is in the configured `agents/` dir.
3. Point clients at `agent=orchestrator` for any request that decomposes
   into multiple subtasks.

## Acceptance gates

- [x] `pytest python/packages/corlinman-evolution-engine` — 103 tests pass.
- [x] `pytest python/packages/corlinman-agent` — 55 subagent tests pass
  (28 existing + 27 new for spawn_many + blackboard).
- [x] `pytest python/packages/corlinman-server` — 23 tests pass
  (21 existing + 2 new for builtin-tool dispatch).
- [x] Rust `cargo test -p corlinman-core --test config_samples` —
  `orchestrator.yaml` validates.
- [x] Phase A.1 — Python foundation for `subagent.spawn_many` + blackboard.
- [x] Phase A.2 — agent servicer intercepts builtin tools in-process.
- [x] Phase B — GEPA-lite Pareto scorer wired into the package API.
- [x] Phase D — BuildKit cache mounts on cargo + uv stages.
- [ ] Phase C — pre-warmed agent runner pool (**deferred to v0.7.1**).
- [ ] Clean-VM smoke test on `deploy/install.sh --mode docker`
  (manual operator step after `git tag v0.7.0` triggers CI).

## Credits

Inspired by:

- Nous Research's [hermes-agent](https://github.com/NousResearch/hermes-agent)
  and [hermes-agent-self-evolution](https://github.com/NousResearch/hermes-agent-self-evolution).
- The [openclaw](https://github.com/openclaw/openclaw) project's
  pre-warmed container pool pattern.

See `docs/multi-agent-release-plan.md` for the architecture details
and `CHANGELOG.md` for the per-commit diff.
