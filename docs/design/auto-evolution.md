# Auto-Evolution Subsystem — Design

**Status**: Draft · **Target phase**: 2 (skeleton + low-risk kinds) → 3 (medium/high-risk + auto-rollback)
**Owner**: TBD · **Last revised**: 2026-04-24

---

## 1. TL;DR

The agent proposes changes to its own skills / prompts / agent-cards / memory
organization based on observed signals (failures, rejections, successful
patterns). Every proposal goes through an approval queue; approved proposals
are applied via config hot-reload; metrics-degraded changes auto-rollback
within a grace window.

This is the mechanism layer behind the project's "类人 → 超越人类" framing: a
human takes years to acquire a new skill and can't rollback a bad habit
deterministically; a corlinman instance accumulates proposals weekly, tests
them in shadow, and reverts anything that regresses.

**Non-goals**:
- Not "AI proposes code changes to its own Rust/Python source." Proposals
  only mutate declarative config assets (skills, prompts, tags, thresholds).
- Not replacing operator judgment. Medium+ risk proposals always require a
  human approval.
- Not an autoML system for fine-tuning model weights. Model choice is a
  `tool_policy` kind but we don't retrain.

## 2. Design Principles (safety first)

1. **Approval-gated by default.** Every mutation to a persisted asset
   (skill, prompt, agent card, tag tree, tool policy) passes through the
   existing `ApprovalGate`. Low-risk kinds can be flagged `auto` with an
   explicit allow-list; everything else stays human-reviewed.
2. **Fully versioned + reversible.** Every applied proposal creates a
   git-style revision row in `evolution_history`. Rollback is a single
   command that re-applies the inverse diff.
3. **Budget-bounded.** Per-week and per-kind caps (e.g. max 3 skill updates
   / week, max 1 prompt template rewrite / week). A runaway `EvolutionEngine`
   hits the cap and quietly stops proposing; the operator is alerted.
4. **Shadow-tested for medium+ risk.** Prompt and tool-policy changes must
   run against a frozen evaluation set before the queue surfaces them to
   the operator. Pass/fail metrics attach to the proposal.
5. **Metrics-degradation triggers auto-rollback.** For every applied change,
   capture a baseline on the preceding N days of the tracked metrics. If
   those metrics degrade past a threshold within the grace window (default
   72 hours), auto-revert and log a `rollback.auto` event.
6. **Every step is observable.** Hook events, tracing spans, Prometheus
   counters at each stage (observation / proposal / approval / application /
   rollback). No silent mutations.

## 3. Architecture

```
                      Hook bus (corlinman-hooks)
                              │
                              ▼
                      EvolutionObserver  ──► evolution_signals (SQLite)
                                                       │
         scheduler tick (corlinman-scheduler, daily)   │
                              │                        ▼
                              └──► EvolutionEngine (Python agent loop)
                                              │
                                              ├─► read signals
                                              ├─► run self-improvement prompt
                                              └─► emit EvolutionProposal[]
                                                       │
                                                       ▼
                                         ShadowTester (optional, risk≥medium)
                                                       │
                                                       ▼
                                         evolution_proposals (SQLite)
                                                       │
                                                       ▼
                                     /evolution UI · operator review
                                                       │
                                                       ▼
                                             ApprovalGate::resolve
                                                       │
                                                       ▼
                                         EvolutionApplier (writes assets)
                                                       │
                                                       ▼
                                      config hot-reload · hooks broadcast
                                                       │
                                                       ▼
                                     evolution_history + metrics baseline
                                                       │
                                           (N days metrics watch)
                                                       │
                                                       ▼
                                        AutoRollback? ─► revert + log
```

## 4. Components

### 4.1 EvolutionObserver (Rust, new module in corlinman-gateway)

A hook-bus subscriber that filters for a curated event set and writes one
row per observation to `evolution_signals`. Never blocks; on write failure
logs WARN and drops. Signals:

- `tool.call.failed` — tool name, error kind, latency, retry count
- `tool.call.timeout` — tool name, configured timeout, session id
- `approval.rejected` — proposal kind (if nested), rejection reason, actor
- `session.ended` — session id, turn count, tool calls, final state
- `user.correction` — operator-flagged "this was wrong" events (new hook
  event, UX spec pending)

Rate-limit: write queue of 10K; older rows auto-pruned at 90 days.

### 4.2 EvolutionEngine (Python, new package `corlinman-evolution`)

A scheduled agent loop (daily by default, cron-configurable via
`[scheduler.jobs]`). Each run:

1. Load signals from last `lookback_days` (default 7).
2. Cluster signals by kind/target (e.g. "all timeouts on `web_search`").
3. For each cluster above `min_cluster_size` (default 3), invoke a
   dedicated self-improvement prompt (model configurable; cheaper model
   by default) that emits a structured `EvolutionProposal`.
4. Dedup proposals against recent `evolution_history` — don't re-propose
   something that got denied twice in last 30 days.
5. Write proposals to `evolution_proposals` with status `pending` (or
   `shadow_running` if risk ≥ medium).

Runs under a per-run time budget (default 60s). If it over-budgets, logs
WARN and defers remaining clusters to next tick.

### 4.3 ShadowTester (Rust, new module in corlinman-evaluator or gateway)

For `risk ∈ {medium, high}`, the tester:

1. Snapshot the current asset (e.g. current version of `web_search.md`).
2. Apply the proposed diff to a sandbox copy.
3. Replay a frozen evaluation set against both versions.
4. Record metrics: accuracy, latency p50/p95, cost, tool call count.
5. Attach `shadow_metrics` JSON to the proposal row.

Frozen eval set lives at `eval/evolution/<kind>.jsonl` — maintained by
operators; 20-50 cases is enough for most kinds. Missing eval set → the
proposal is labeled `shadow_skipped` (operator decides whether to block
on shadow or allow).

### 4.4 EvolutionApplier (Rust, part of gateway)

Given an approved proposal:

1. Lock the target asset (flock or in-memory mutex per asset path).
2. Apply the diff. For file-backed assets, write to a tmp file then
   atomic-rename. For DB-backed (tag tree, memory ops), run the SQL in a
   transaction.
3. Record `evolution_history` row: proposal id, diff hash, before-hash,
   after-hash, `applied_at`, operator, rollback link.
4. Emit `evolution.applied` hook event (lets skills / UI react).
5. Trigger config hot-reload on relevant channels/skills/agents.

### 4.5 AutoRollback (Rust, gateway background task)

Runs every 30 min. For each applied change within its grace window:

1. Compute metrics delta vs baseline (captured at apply time).
2. If metric crosses degradation threshold (per-kind configurable; e.g.
   "error rate up > 2 percentage points AND p95 latency up > 25%"):
   - Emit a `rollback` proposal auto-approved with reason
     `auto_rollback:<metric>`.
   - EvolutionApplier applies the inverse diff.
   - Writes a new row to `evolution_history` linking the two.
3. Alert operator via log + UI banner.

### 4.6 ProposalQueue UI (Next.js, new route `/evolution`)

Three-tab layout: `Pending` (actionable) · `Approved` (in grace window) ·
`History` (applied / rolled back). Each proposal card shows:

- Kind badge + risk badge + budget cost
- Target asset path
- Unified diff view (syntax-highlighted)
- Reasoning (from the agent)
- Trace ids (link to /logs filtered by these traces)
- Shadow metrics delta table (if run)
- Approve / Deny / Edit-then-approve buttons

Bulk actions for low-risk clusters (approve all memory ops from last 24h).

## 5. Data Model

### 5.1 `evolution_signals`
```sql
CREATE TABLE evolution_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind   TEXT NOT NULL,             -- 'tool.call.failed' etc
    target       TEXT,                       -- tool name / skill id / etc
    severity     TEXT NOT NULL,              -- 'warn' | 'error' | 'info'
    payload_json TEXT NOT NULL,              -- structured event detail
    trace_id     TEXT,
    session_id   TEXT,
    observed_at  INTEGER NOT NULL            -- unix ms
);
CREATE INDEX idx_evol_signals_kind_target ON evolution_signals(event_kind, target);
CREATE INDEX idx_evol_signals_observed ON evolution_signals(observed_at);
```

### 5.2 `evolution_proposals`
```sql
CREATE TABLE evolution_proposals (
    id              TEXT PRIMARY KEY,        -- 'evol-2026-04-24-001'
    kind            TEXT NOT NULL,
    target          TEXT NOT NULL,
    diff            TEXT NOT NULL,            -- unified diff
    reasoning       TEXT NOT NULL,
    risk            TEXT NOT NULL,            -- 'low' | 'medium' | 'high'
    budget_cost     INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL,            -- 'pending' | 'shadow_running'
                                              -- | 'shadow_done' | 'approved'
                                              -- | 'denied' | 'applied' | 'rolled_back'
    shadow_metrics  TEXT,                     -- JSON, nullable
    signal_ids      TEXT NOT NULL,            -- JSON array of evolution_signals.id
    trace_ids       TEXT NOT NULL,            -- JSON array
    created_at      INTEGER NOT NULL,
    decided_at      INTEGER,
    decided_by      TEXT,
    applied_at      INTEGER,
    rollback_of     TEXT REFERENCES evolution_proposals(id)
);
```

### 5.3 `evolution_history`
```sql
CREATE TABLE evolution_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id    TEXT NOT NULL REFERENCES evolution_proposals(id),
    kind           TEXT NOT NULL,
    target         TEXT NOT NULL,
    before_sha    TEXT NOT NULL,
    after_sha     TEXT NOT NULL,
    inverse_diff   TEXT NOT NULL,             -- the rollback payload
    metrics_baseline TEXT NOT NULL,           -- JSON, captured at apply
    applied_at     INTEGER NOT NULL,
    rolled_back_at INTEGER,
    rollback_reason TEXT
);
```

## 6. Proposal Schema (JSON)

```json
{
  "id": "evol-2026-04-24-001",
  "kind": "skill_update",
  "target": "skills/web_search.md",
  "diff": "--- a/skills/web_search.md\n+++ b/skills/web_search.md\n@@ -12,3 +12,3 @@\n-timeout: 8s\n+timeout: 12s\n",
  "reasoning": "5 web_search failures in past 24h (trace ids attached) — all hit the 8s ceiling on mainland network paths. Raising default timeout to 12s covers the p95.",
  "risk": "medium",
  "budget_cost": 1,
  "shadow_test_plan": {
    "fixture": "eval/evolution/web_search.jsonl",
    "metrics": ["success_rate", "p95_latency_ms", "avg_cost_usd"]
  },
  "trace_ids": ["a12...", "b34..."],
  "signal_ids": [4821, 4833, 4901, 4908, 4919]
}
```

## 7. Evolution Kinds & Risk Matrix

| Kind                | Risk        | Default approval | Shadow | Budget cost |
|---------------------|-------------|------------------|--------|-------------|
| `memory_op`         | low         | **auto**         | no     | 0           |
| `tag_rebalance`     | low         | **auto**         | no     | 0           |
| `retry_tuning`      | low–medium  | auto (opt-in)    | no     | 1           |
| `agent_card`        | medium      | human            | no     | 2           |
| `skill_update`      | medium      | human            | yes    | 3           |
| `prompt_template`   | high        | human            | required | 5         |
| `tool_policy`       | high        | human            | required | 5         |
| `new_skill`         | high        | human + gray-out | required | 8         |

`budget_cost` rolls into a per-week cap (default 15/week). Agent can't
exceed cap; oldest rejected proposals drop off first.

## 8. Integration Points

| Subsystem               | Integration                                                      |
|-------------------------|------------------------------------------------------------------|
| `corlinman-hooks`       | Observer subscribes; Applier emits `evolution.applied` / `rollback` events |
| `corlinman-scheduler`   | Engine runs on a cron schedule; added as a built-in job          |
| `middleware/approval.rs`| Proposal approval is an ApprovalGate call with its own `kind=evolution` namespace |
| `corlinman-skills`      | `skill_update` / `new_skill` proposals write to `skills/*.md`   |
| `corlinman-vector + tagmemo` | `tag_rebalance` / `memory_op` operate on vector store + tag tree |
| `corlinman-core/config` | `prompt_template` / `tool_policy` / `retry_tuning` may mutate TOML; triggers config hot-reload |
| Admin UI                | New route `/evolution`; detail drawer mirrors `/logs` pattern    |

## 9. Budget & Rate Limiting

- Per-week `budget_total` (default 15). Persisted in `config.toml` under
  `[evolution.budget]`.
- Per-kind caps: e.g. `skill_update ≤ 3/week`, `prompt_template ≤ 1/week`.
- When cap hit: Engine stops proposing, logs WARN, surfaces a banner on
  `/evolution` ("budget reached — raise cap or wait").
- Operator-triggered override: a `force_propose` flag on Engine run (from
  CLI) bypasses the cap once.

## 10. Rollout

### Phase 2 (skeleton, 2-3 weeks)

- [ ] Data model migration (evolution_signals / proposals / history tables)
- [ ] EvolutionObserver — subscribe + write, no clustering yet
- [ ] EvolutionEngine — one kind only (`memory_op`), cron-scheduled
- [ ] EvolutionApplier — memory op application
- [ ] `/evolution` UI — pending tab only; approve/deny
- [ ] Metrics: counters at every stage
- [ ] No ShadowTester, no AutoRollback yet (memory ops are low-risk)

Success criterion: a week of real signal collection on a dev instance,
≥ 10 memory_op proposals, ≥ 5 approved and applied cleanly, zero silent
mutations.

### Phase 3 (medium/high kinds, 3-4 weeks)

- [ ] Extend Engine to `tag_rebalance` + `skill_update` + `agent_card`
- [ ] ShadowTester MVP — evals for skill_update + agent_card
- [ ] `/evolution` — approved + history tabs; metrics delta viz
- [ ] AutoRollback — 72h window, default thresholds per kind
- [ ] Budget enforcement + caps UI
- [ ] Prompt template kind behind a config flag `[evolution.enable_prompt_kind]`

### Phase 4 (optional)

- [ ] `tool_policy` + `new_skill` kinds with canary/gray rollout
- [ ] Cross-agent proposal sharing (agent A's skill update benefits B)
- [ ] Federated evolution across multiple corlinman instances

## 11. Open Questions

1. **Evaluation set authorship.** Who writes the frozen `eval/evolution/*.jsonl`?
   Operator? Or do we bootstrap from production traces (with redaction)?
2. **Signal quality.** Tool timeouts are noisy (network blips vs real
   misconfiguration). Do we need a `severity` smoothing step before clustering?
3. **Rollback conflicts.** If a rollback tries to apply while a new proposal
   is mid-flight on the same target, who wins? (Lean: rollback always wins
   because it's a correction; log both.)
4. **Prompt template safety.** A bad prompt can silently drop the agent's
   output quality across all users. Should `prompt_template` always require
   canary (5% traffic) before full rollout? Lean: yes, after Phase 3.
5. **Multi-tenant.** When corlinman is deployed multi-tenant, are proposals
   per-tenant or global? Lean: per-tenant with an opt-in "share learnings"
   feature. Revisit in Phase 4.
6. **Privacy.** Signals include tool call args — may contain PII. Apply the
   same redaction pipeline used for logs. Not in Phase 2 MVP but must land
   before prod.

## 12. Anti-Goals

Explicit list of things this subsystem will **not** do, lest scope creep:

- No autonomous code generation that lands on main branch.
- No model weight fine-tuning. Model choice may be a `tool_policy`
  proposal; training is out of scope.
- No personality/emotion-state mutation. That's a separate subsystem (see
  类人 Phase 3 roadmap).
- No cross-tenant learning without explicit opt-in.
- No "proposal that proposes meta-proposals." One level of recursion. If
  the operator wants to change how evolution itself works, they edit config
  manually.
