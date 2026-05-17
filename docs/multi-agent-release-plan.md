# Multi-Agent Release Plan (v0.7.0)

> **Status**: draft — pending operator sign-off on §0.3.
> **Author**: Claude Code (planning agent), 2026-05-17.
> **Inspired by**: [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent),
> [`NousResearch/hermes-agent-self-evolution`](https://github.com/NousResearch/hermes-agent-self-evolution),
> [`openclaw/openclaw`](https://github.com/openclaw/openclaw).

## 0. Goal & framing

### 0.1 Goal

Ship **v0.7.0 — multi-agent**: corlinman becomes a system where multiple
specialised agents collaborate on one user turn, refines itself nightly,
and boots in seconds on a clean VM. Three concrete deltas vs. v0.6.8:

| Surface | v0.6.8 | v0.7.0 target |
| --- | --- | --- |
| Multi-agent | nested only, sequential (depth ≤ 2) | parallel siblings + shared blackboard + orchestrator role |
| Self-evolution | `KIND_MEMORY_OP` only; agent card / prompt / skill handlers wired but gated off | all 6 kinds gated on behind shadow-test + auto-rollback; GEPA-lite prompt scoring |
| Fresh-VM cold start | `docker pull` (~30 s) + first chat completion (~1.5 s python import + provider SDK init) | `docker pull` (~10 s split image) + first chat completion (~50 ms warm pool hit) |

### 0.2 What we are absorbing from the reference projects

**From [hermes-agent](https://github.com/NousResearch/hermes-agent) + [hermes-agent-self-evolution](https://github.com/NousResearch/hermes-agent-self-evolution):**

1. **Skill auto-distillation.** After a task completes successfully, the
   solution path is summarised into a reusable skill with name, when-to-use,
   tool sequence, and exemplar. Future similar tasks load the skill into
   the system prompt. We map this to our existing
   `corlinman-skills` package + `KIND_SKILL_UPDATE` handler, which is
   already coded but gated off (`engine.py:82`).
2. **GEPA-style prompt evolution.** Genetic-Pareto: keep a small pool of
   prompt variants per agent card; score each against stored episodes; the
   Pareto front of (success rate, token cost) survives to the next round.
   Hermes uses DSPy + LLM-judge; we ship a tractable v1 — deterministic
   scoring on `episodes` rows we already write — and leave the LLM-judge
   slot pluggable.
3. **True multi-agent.** Hermes issue #344 captures the move from
   "single agent + throwaway children" to siblings that can talk and share
   state. Our subagent runtime already supports siblings architecturally
   (`api.py:200-222` derives a `ParentContext` per `child_seq`); what's
   missing is the concurrent spawn path and the shared blackboard.

**From [openclaw](https://github.com/openclaw/openclaw):**

1. **Pre-warmed pool.** `OPENCLAW_MAX_WARM_CONTAINERS` boots a pool of
   ready-to-go runtimes in the background; the first matching session
   claims a warm slot instead of cold-spawning. We map to a **Python
   agent-runner pool** keyed by `(agent_card_id, provider_alias)`. Cold
   spawn only when the session has bespoke boot inputs.
2. **Session-resident workers.** A claimed worker stays resident across
   the conversation's turns rather than being recreated; eviction is
   `OPENCLAW_MAX_ACTIVE_CONTAINERS` with oldest-idle. We will adopt the
   same bounded-pool / oldest-idle eviction shape.
3. **Per-session sandbox option.** Docker-per-session is correct for
   *untrusted* code; corlinman already uses bollard-based sandboxing for
   tools. We are **not** going Docker-per-session at the agent layer —
   that's an extreme our threat model doesn't need yet.

### 0.3 Scope decisions to confirm with the operator

Before we go heads-down, the operator should confirm:

- [ ] **Parallel siblings: required for v1.** Not deferred to v0.8.
- [ ] **Evolution kinds to enable in v0.7.0:** all 6 (memory_op,
      tag_rebalance, skill_update, prompt_template, tool_policy,
      agent_card) — with `agent_card` and `tool_policy` defaulting to
      **shadow-only** for one release cycle.
- [ ] **Deploy speed lever:** prewarmed agent pool (Phase C) is the
      primary cold-start win; Dockerfile rework (Phase D) is secondary.
- [ ] **Release vehicle:** v0.7.0 tag on `main` after Phase E, no LTS
      branch.

---

## 1. What's already built (do not rebuild)

Findings from a code-grounded walk of the repo. Cite these to keep
follow-up tasks scoped tightly.

- `agents/{editor,mentor,researcher}.yaml` — 3 agent cards already exist,
  parsed by Rust (`rust/crates/corlinman-core/tests/config_samples.rs:29`)
  and Python (`corlinman_agent/agents/card.py:1-60`).
- `python/packages/corlinman-agent/src/corlinman_agent/subagent/` —
  full subagent runtime with PyO3 supervisor. `api.py:200-222` already
  derives unique `ParentContext` per `child_seq`. `__init__.py:1-36`
  documents max depth = 2, max tool calls = 12, max wall = 60s.
- `python/packages/corlinman-evolution-engine/` — `engine.py:79-89`
  registers all 6 handler types in `DEFAULT_HANDLERS`. Gating happens
  via `EngineConfig.enabled_kinds` — flipping the gates is a 1-line
  config change. `KIND_PROMPT_TEMPLATE`, `KIND_TOOL_POLICY`,
  `KIND_AGENT_CARD` already have ShadowTester hooks documented at
  `engine.py:83-89`.
- `dist/` — prebuilt tarballs for `aarch64-apple-darwin`,
  `aarch64-linux-gnu`, `x86_64-linux-gnu` exist through v0.6.4+.
  `deploy/install.sh` supports `--mode docker` and `--mode native`.
- `docker/Dockerfile:55-69` — cargo-chef recipe caching is in place.
  ghcr.io/ymylive/corlinman is published per tag + `:latest` + `:dev`.

The implication: a lot of v0.7.0 is **flipping gates and adding the
concurrency + pool plumbing**, not green-fielding.

---

## 2. Architecture changes

### 2.1 Parallel sibling agents

```
       ┌────────────────────────┐
user → │  Orchestrator agent    │
       │  (new card: planner)   │
       └─┬──────┬──────┬────────┘
         │      │      │       spawn_many({editor, researcher, mentor},
         ▼      ▼      ▼       blackboard=<shared key>)
       ┌──┐   ┌──┐   ┌──┐
       │e │   │r │   │m │      ← all 3 run concurrently
       └─┬┘   └─┬┘   └─┬┘
         │      │      │
         └──────┴──────┘
                │           reduce: orchestrator reads blackboard,
                ▼           writes final response.
       ┌────────────────┐
       │ blackboard     │  ← SQLite-backed, per-trace, append-only.
       │ (kb.sqlite/    │     Sibling writes keyed by (trace_id, key),
       │  blackboard)   │     reads are snapshot-at-call.
       └────────────────┘
```

New surfaces:

- `subagent.spawn_many(tasks: list[TaskSpec], blackboard_key: str)` —
  Rust supervisor fan-outs N siblings, awaits all (or until a budget
  cap), returns `list[TaskResult]`. The supervisor already isolates
  siblings via `child_seq`; we add a `Vec<JoinHandle>` and a per-fanout
  `tokio::sync::Semaphore` for concurrency cap.
- `blackboard.read(key)` / `blackboard.write(key, value)` tools, gated
  via the existing tool allowlist mechanism. Storage: a new table in
  `kb.sqlite` (`blackboard(trace_id, key, value, written_at, written_by)`)
  with primary key `(trace_id, key, written_at)`.
- New agent card `agents/orchestrator.yaml` — a planner persona whose
  allowed tools are `subagent.spawn`, `subagent.spawn_many`,
  `blackboard.read`, `blackboard.write`.

Hard caps in v0.7.0 to keep blast radius small:
- max **3** concurrent siblings per fan-out
- max **2** fan-out levels per request (so a sibling can fan out once,
  not infinitely)
- shared wall-clock budget = `min(parent_remaining, 90s)`
- per-tenant fan-out quota lives in the same `corlinman-subagent`
  quota store

### 2.2 Self-evolution mutations

Enable all 6 kinds but gate the destructive ones:

| Kind | Default in v0.7.0 | Notes |
| --- | --- | --- |
| `memory_op` | **auto-apply** | already shipped in v0.6.x |
| `tag_rebalance` | **auto-apply** | already in `DEFAULT_HANDLERS` |
| `skill_update` | **operator queue** | proposals filed; operator approves |
| `prompt_template` | **shadow-only → operator queue** | runs in shadow against last 50 episodes; if win-rate ≥ +5% and token cost ≤ +10%, queued |
| `tool_policy` | **operator queue** | low frequency, security-sensitive |
| `agent_card` | **shadow-only → operator queue** | mutates persona; never silent |

New plumbing:

- `corlinman_evolution_engine.gepa` — a 200-line module. Given a
  prompt-template proposal and a sample of recent `episodes` rows for
  that agent card, runs the agent with each variant against the same
  inputs and scores: (a) tool-call sequence match, (b) final-response
  semantic similarity to known-good, (c) token cost. Pareto front of
  (success, cost) survives. No LLM training, no DSPy dependency — that
  stays a v0.8 conversation.
- ShadowTester integration: `corlinman-shadow-tester` binary is already
  built (`Dockerfile:140`). We wire the engine to invoke it for
  `prompt_template` and `agent_card` proposals before queueing.
- AutoRollback: `corlinman-auto-rollback` is also built; we wire it as
  a cron `0 */6 * * *` that checks SLOs and reverts the last applied
  mutation if any tracked SLO regresses by > 10%.

### 2.3 Pre-warmed agent runner pool

New Rust component `corlinman-runner-pool` (a sibling of
`corlinman-subagent`). Owned by the gateway; lifecycle = gateway
lifecycle.

```
pool entry := (agent_card_id, provider_alias, runtime_handle)
                                                     │
                                                     ▼
                            python subprocess speaking gRPC over UDS,
                            already-imported provider SDK, ready for
                            the first ChatRequest with ≈50 ms TTFB.

pool size = CORLINMAN_RUNNER_POOL_WARM (default: 2 per agent card)
active cap = CORLINMAN_RUNNER_POOL_MAX (default: 8 across all keys)
eviction = oldest-idle when active_cap pressed
```

Cold-spawn path (unchanged) kicks in for sessions whose ChatRequest
carries `runtime_overrides` (custom system prompt patches, non-default
plugin set). The warm pool is for the median case — a user opens a
chat with `agent=editor` and the existing default config.

Metrics: emit `runner_pool_hit_total`, `runner_pool_miss_total`,
`runner_pool_evict_total`, `runner_pool_warm_age_seconds` to Prometheus.

### 2.4 Deploy speed

Three changes, smallest blast radius first:

1. **Split the runtime image.** Today `runtime` carries `python:3.12-slim`
   + nodejs + venv + rust binaries (~ 350 MB). Split into:
   - `ghcr.io/ymylive/corlinman-base:<py-version>` — python + node + uv,
     bumped only when language toolchains move. Built and pushed once
     per quarter.
   - `ghcr.io/ymylive/corlinman:<tag>` — `FROM corlinman-base`, just the
     application bits. Per-tag pull drops to ~80 MB.
2. **Pre-bake the venv as a named layer.** Currently the venv is
   `COPY`-ed wholesale (Dockerfile:147). Move it to its own `FROM scratch
   AS venv` and `COPY --from=venv` so it cache-hits across Rust-only
   changes. Bonus: lets us publish `ghcr.io/ymylive/corlinman-venv:<lock-hash>`
   that local dev can `docker pull` instead of running `uv sync` (~3 min
   on a clean clone).
3. **Cache hint for cargo-chef.** Add `--mount=type=cache,target=/usr/local/cargo/registry`
   on the `cargo chef cook` line (Dockerfile:57). Local rebuilds drop from
   ~12 min to ~90 s on a warm registry cache. Buildx CI already supports
   this.

Out of scope for v0.7.0: zig cross / sccache distributed. Those are
v0.8 conversations.

---

## 3. Phased delivery

Each phase is one to two days of work. Each ends with `cargo test &&
pytest && make doctor` green and a smoke test on a clean VM.

### Phase A — parallel siblings (3 days)

1. Rust `corlinman-subagent`: add `spawn_many` method that constructs
   N child supervisors, runs them concurrently under a semaphore,
   awaits all, returns `Vec<TaskResult>` in input order. Reuses the
   single-spawn quota path per child.
2. Python `corlinman_agent.subagent`: add `spawn_many(tasks)` tool;
   register in `tool_wrapper.py` next to `spawn`. Result envelope is
   `{"tasks": [TaskResult, ...]}`.
3. Blackboard storage: new migration in `corlinman-vector` (kb.sqlite
   carries it; namespace = `blackboard:<trace_id>`). Read tool returns
   `{key: value}`; write tool returns `{written_at}`. Concurrent writes
   to the same key serialize via SQLite's `IMMEDIATE` transaction.
4. `agents/orchestrator.yaml` — new card. System prompt teaches the
   pattern: "decompose → spawn_many → reduce". Tool allowlist:
   `subagent.spawn`, `subagent.spawn_many`, `blackboard.read`,
   `blackboard.write`.
5. Tests:
   - unit (rust): `spawn_many` of 3 tasks finishes in `max(child_time)`,
     not `sum(child_time)`.
   - unit (py): blackboard concurrent writes don't lose data.
   - integration: `agent=orchestrator` answering a question that
     requires research + editing produces a coherent final.

**Acceptance:** orchestrator card answers "Summarise the 3 latest
release notes and propose what v0.7 should focus on" by dispatching to
researcher (read docs/), then editor (synth), and returns under 30s
wall-clock for a no-network-latency provider mock.

### Phase B — evolution enablement (2 days)

1. Flip `EngineConfig.enabled_kinds` default in `cli.py` /
   `config.example.toml` to include all 6 kinds, with policy bits
   (`apply_mode = "auto" | "queue" | "shadow"`) per kind.
2. Wire ShadowTester into the engine for `prompt_template` and
   `agent_card` proposals. `engine.py:83-89` already names the hook;
   build the actual call site.
3. New module `corlinman_evolution_engine.gepa` — described in §2.2.
   Pareto frontier code: lifted from cookbook patterns, fits in ~150
   lines.
4. AutoRollback cron: register `0 */6 * * *` in the gateway scheduler;
   reuse the existing `corlinman-auto-rollback` binary path.
5. Docs: replace placeholder sections in `docs/evolution-loop.md`.

**Acceptance:**
- a synthetic `prompt_template` cluster (≥ 3 signals) on the `mentor`
  card produces a proposal; shadow-test runs against 50 cached
  episodes; proposal is queued only if Pareto-dominates.
- an `agent_card` proposal never auto-applies, always queues.
- killing one provider for 10 min then restoring it causes
  AutoRollback to revert the most recent prompt mutation iff
  success-rate dropped > 10%.

### Phase C — runner pool (2 days)

1. New crate `rust/crates/corlinman-runner-pool`. API:
   `acquire(key) -> RunnerHandle`, `release(handle)`,
   `prewarm(key, n)`. Internally: tokio mpsc + bounded
   `tokio::sync::Mutex<HashMap<Key, VecDeque<Handle>>>`.
2. Gateway integrates: when a ChatRequest lands, look up
   `(card_id, provider_alias)`, call `pool.acquire`. On 200/500,
   `release` returns it to the warm pool if `runtime_overrides` is
   empty, otherwise drops.
3. Spawn path: each warm runner is the same Python subprocess we
   already spawn (`corlinman-agent-server`); the new thing is we
   *keep it alive between turns* with a `keepalive` ping every 30 s.
4. Eviction: oldest-idle when `active_count > CORLINMAN_RUNNER_POOL_MAX`.
   Idle = "no in-flight request for ≥ 60 s".
5. Metrics: 4 new Prometheus counters/gauges (§2.3).

**Acceptance:** `make bench-cold-start` on a quiet host shows the
median ChatRequest with a warm pool hit at ≤ 80 ms TTFB vs. ≥ 800 ms
on cold (already true today, just regressed-test it).

### Phase D — Docker split + cache (1 day)

1. Carve out `docker/Dockerfile.base` (python + node + uv) and push to
   ghcr as `corlinman-base:py3.12-node20`.
2. Rewrite `docker/Dockerfile` to `FROM corlinman-base`. Move the venv
   into its own multi-stage so `--cache-from
   ghcr.io/ymylive/corlinman-venv:<uv.lock-hash>` works.
3. Add `--mount=type=cache` to cargo-chef step.
4. CI matrix: build base image only when `pyproject.toml` or `uv.lock`
   changes; otherwise reuse the published one.
5. Smoke: `time docker pull ghcr.io/ymylive/corlinman:0.7.0-rc1`
   on a clean Docker host should be ≤ 15 s.

**Acceptance:** clean-clone `docker compose up` finishes in ≤ 90 s
(was ~15 min) when running off the published image.

### Phase E — release v0.7.0 (1 day)

1. CHANGELOG entry. Version bump: `Cargo.toml`, `pyproject.toml`,
   `python/packages/*/pyproject.toml`, `ui/package.json`.
2. `docs/release-notes-v0.7.0.md` — covers all four pillars with a
   migration checklist.
3. Update `docs/architecture.md` to mention the orchestrator role and
   pool.
4. Tag → CI builds tarballs + pushes images.
5. Final smoke: spin a clean VM, run `deploy/install.sh --mode docker`,
   confirm `/admin/agents` lists `orchestrator`, ask it a fan-out
   question, verify metrics show pool hits.

**Acceptance:** Release tagged on `main`. CI green. Tarballs SHA-256
published. Smoke VM works.

---

## 4. Risks & rollback

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Concurrent siblings deadlock on blackboard | low | `IMMEDIATE` transaction + 5s timeout; tests cover contention |
| Auto-applied `prompt_template` proposal degrades a tenant | medium | shadow-test gates; AutoRollback cron; ops dashboard alarm on success-rate drop |
| Warm pool leaks Python subprocesses | medium | hard cap `CORLINMAN_RUNNER_POOL_MAX`; idle eviction; `make doctor` checks process count |
| Split base image diverges from runtime | medium | base image rebuild pinned to `pyproject.toml` / `uv.lock` hash; CI gate |
| Orchestrator card costs too many tokens | low | hard cap of 3 siblings × 12 tool calls × 60 s; orchestrator system prompt prefers serial when one tool suffices |

**Rollback per phase:** every phase is shippable independently. If
Phase B regresses, set `enabled_kinds = ["memory_op", "tag_rebalance"]`
in `config.toml` — engine reverts to v0.6.x behavior with no code
change. If Phase C regresses, set `CORLINMAN_RUNNER_POOL_WARM=0` —
pool degenerates to cold-spawn.

---

## 5. Acceptance gates (overall)

The release ships only when **all** are green:

1. `cargo test --workspace --release` passes.
2. `pytest python/packages -q` passes.
3. `make doctor` returns 20+ green checks on a clean VM.
4. Orchestrator smoke (Phase A acceptance) passes against the prebuilt
   image.
5. Evolution synthetic clusters (Phase B acceptance) all behave per
   policy.
6. Cold-start microbenchmark (Phase C acceptance) under 80 ms median.
7. Clean-VM `docker compose up` under 90 s (Phase D acceptance).
8. `docs/release-notes-v0.7.0.md` reviewed by the operator.

---

## 6. Out of scope (v0.8 candidates)

- DSPy / LLM-judge for GEPA scoring (we ship deterministic v1).
- Agent-to-agent direct messaging (we ship blackboard, not channels).
- Docker-per-session sandboxing at the agent layer.
- Multi-host / multi-tenant federation of warm pools.
- WebSocket control plane for live agent steering.

---

*End of plan. Operator sign-off requested on §0.3 before Phase A
starts.*
