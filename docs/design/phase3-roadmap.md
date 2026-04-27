# Phase 3 Roadmap — Brave Kinds + 类人 Cognition

**Status**: Draft · **Target window**: 3-5 weeks · **Owner**: TBD · **Last revised**: 2026-04-24

> Phase 2 closed the EvolutionLoop on `memory_op` (low-risk, deterministic).
> Phase 3 unlocks the **interesting kinds** (skill / prompt / tool policy)
> behind the safety infra they require, and lays the first stone of the
> **类人 cognition** layer (memory decay, user model, persona persistence)
> per the project's "类人 → 超越人类" framing.

---

## 1. Goals & Non-Goals

### Goals
1. **Bravery with safety**: enable medium/high-risk kinds (`skill_update`,
   `agent_card`, `prompt_template`, `tool_policy`) with shadow-test gating
   and metrics-degradation auto-rollback so we can be aggressive.
2. **Closed learning loop** (hermes-agent inspired): agent observes
   successful task patterns and proposes **new skills** (not just
   modifications). The "task → skill extraction → refinement → reuse"
   cycle.
3. **类人 baseline**: short-term/long-term memory split with decay,
   reflection-driven consolidation, and a deepening user model that
   survives sessions.
4. **Operator visibility**: `/evolution` Approved + History tabs with
   per-kind metric deltas, AutoRollback alerts surfaced.

### Non-Goals
- No multi-tenant federation. Single-instance only.
- No autonomous code generation against `rust/` or `python/` source.
- No model fine-tuning / weight updates.
- No "agent dreams" subsystem as a single bundled feature — instead its
  parts (reflection, consolidation, scheduler) are absorbed as primitives.

---

## 2. Architecture Overview (vs Phase 2)

```
                     [ Phase 2 baseline ]
   hooks ──► Observer ──► signals ──► Engine ──► proposals ──► Applier
                                                                  │
                                                                  ▼
                                                            history (audit)

                     [ + Phase 3 deltas ]

   Engine adds:
     - skill_extraction handler (closed loop)
     - tag_rebalance handler
     - skill_update / agent_card / prompt_template / tool_policy handlers
   ↓
   ShadowTester runs proposals through eval set before queue
   ↓
   /evolution UI: Approved + History tabs · metrics delta viz
   ↓
   AutoRollback monitors applied changes for N days; auto-revert on degrade

   Plus a parallel cognition stream (independent of EvolutionLoop):
     - MemoryDecay scheduled job (kb.sqlite vectors decay; promotion
       to "consolidated" namespace via reflection)
     - UserModel per-channel-session distilled traits + drift detection
     - Persona state (agent-card mutable fields) persisted across sessions
```

---

## 3. Wave Structure

Three waves, each 1-1.5 weeks, mostly parallel.

### Wave 1 — Safety Infrastructure (3 agents, ~1 week)
Goal: build the gates so Wave 2 kinds can land safely.

| ID | Title | Stack | Wkload | Status |
|---|---|---|---|---|
| **3-1A** | **ShadowTester** — eval-set runner + metrics collector + proposal annotation | Rust + Python | 4-5d | ✅ Done 2026-04-27 |
| **3-1B** | **AutoRollback** — 72h grace window, per-kind threshold config, automatic revert + history link | Rust | 3-4d | ✅ Done 2026-04-27 |
| **3-1C** | **Budget enforcement** — per-week / per-kind caps in config; engine respects; UI surfaces remaining quota | Rust + UI | 2-3d | TBD |

**3-1A breakdown** (see `docs/design/shadow-tester.md`):
- Step 1 ✅ — schema migration (`eval_run_id`, `baseline_metrics_json` columns); `[evolution.shadow]` config block; `corlinman-shadow-tester` crate scaffold.
- Step 2 ✅ — `EvalCase` / `EvalSet` types + YAML loader + 4 hand-crafted `memory_op` fixtures.
- Step 3 ✅ — `KindSimulator` trait + `MemoryOpSimulator` impl + `ShadowRunner` orchestrator.
- Step 4 ✅ — `corlinman-shadow-tester` CLI binary + `[scheduler.jobs.shadow_tester]` subprocess job (03:30 daily, 30 min after engine) + cross-crate e2e suite in `corlinman-integration-tests/tests/shadow_loop.rs`.

**Wave 1 acceptance**: a fake high-risk proposal goes through shadow → ops sees metrics delta → approves → applies → AutoRollback monitor active. End-to-end harness in `tests/`.

### Wave 2 — Brave Kinds (4 agents, ~1.5 weeks; depends on Wave 1)
Goal: turn on the kinds that actually move the needle on agent quality.

| ID | Title | Stack | Wkload |
|---|---|---|---|
| **3-2A** | **`skill_extraction` kind** — agent infers from successful task clusters which procedures should become reusable skills (closed learning loop, hermes-agent inspired) | Python | 5-7d |
| **3-2B** | **`tag_rebalance` + `skill_update` handlers** — Engine generates these proposals; Applier executes (skill = file diff in `skills/*.md`; tag = SQL on `tag_nodes`) | Python + Rust | 4-5d |
| **3-2C** | **`agent_card` + `prompt_template` handlers** — high-risk kinds; require shadow_test; guarded behind `[evolution.enable_prompt_kind]` flag | Python + Rust | 3-4d |
| **3-2D** | **`/evolution` Approved + History tabs + metrics delta viz** — operator surface for Wave 1 + 2 work | UI | 3-4d |

**Wave 2 acceptance**: agent runs for 7 days, produces ≥ 5 skill_extraction proposals, ≥ 2 are approved + applied, none rolled back. Metrics show measurable improvement on the affected tools' success rate.

### Wave 3 — 类人 Cognition (3 agents, ~1.5 weeks; partially independent of W1/W2)
Goal: lay the first stone of the human-like cognitive layer.

| ID | Title | Stack | Wkload |
|---|---|---|---|
| **3-3A** | **Memory decay + consolidation** — `kb.sqlite` namespace `recent` decays exponentially; `consolidated` namespace persists; reflection job promotes selected chunks | Rust + Python | 5-7d |
| **3-3B** | **User model** — per-session distillation of who-the-user-is (interests, tone preference, recurring topics); deepens across sessions; available as `{{user.*}}` placeholders | Python | 5-7d |
| **3-3C** | **Persona persistence** — agent-card mutable state (mood / fatigue / recent topics) survives across sessions in `agent_state.sqlite`; queryable from prompts | Python | 3-4d |

**Wave 3 acceptance**: cross-session test — agent A interacts with user U over 3 sessions, on session 3 the user model has > 5 distilled traits, the persona's "recent topics" reflects sessions 1-2, and a memory chunk from session 1 has decayed to < 50% relevance score.

### Wave 4 — Bonus / Stretch (1-2 agents, optional)

| ID | Title | Stack |
|---|---|---|
| **3-4A** | **Subagent delegation primitive** — agent can spawn child agent at runtime for parallel sub-tasks | Rust |
| **3-4B** | **Cron NL parser** — `[scheduler.jobs.foo.schedule] = "every weekday at 3am UTC"` → cron expr | Rust |

These are quality-of-life. Not required for "类人 baseline" but they sharpen the experience.

---

## 4. Deliverables — What Lands Where

### New crates
- `corlinman-shadow-tester` (Rust): eval set loader, sandbox runner, metrics collector
- `corlinman-rollback` (Rust): metrics monitor + auto-revert task; could also live inside `corlinman-gateway` if minimal
- `corlinman-user-model` (Python): user trait distillation
- `corlinman-persona` (Python): persona state DB + queries

### Extended packages
- `corlinman-evolution-engine` (Python): + 5 new KindHandlers
- `corlinman-evolution` (Rust): + 1 column `eval_run_id` on proposals, + auto-rollback status field
- `corlinman-vector` (Rust): + `decay_score` column on chunks, + namespace promotion API
- `corlinman-gateway` (Rust): EvolutionApplier extended for skill/prompt/tool diffs

### UI
- `/evolution` Approved tab + History tab (with metrics delta sparklines)
- `/memory` new page: shows recent vs consolidated, decay heatmap
- `/user-model` new page: distilled traits with confidence per session
- `/agent-state` new page: per-agent persona snapshot

---

## 5. Data Model Additions

### evolution_proposals (extend)
```sql
ALTER TABLE evolution_proposals ADD COLUMN eval_run_id TEXT;
ALTER TABLE evolution_proposals ADD COLUMN baseline_metrics_json TEXT;
ALTER TABLE evolution_proposals ADD COLUMN auto_rollback_at INTEGER;
ALTER TABLE evolution_proposals ADD COLUMN auto_rollback_reason TEXT;
```

### chunks (vector store, extend)
```sql
ALTER TABLE chunks ADD COLUMN decay_score REAL NOT NULL DEFAULT 1.0;
ALTER TABLE chunks ADD COLUMN consolidated_at INTEGER;
ALTER TABLE chunks ADD COLUMN last_recalled_at INTEGER;
```

`namespace='consolidated'` chunks are immune to decay; everything else
decays per `[memory.decay]` config.

### NEW: `user_model.sqlite`
```sql
CREATE TABLE user_traits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    trait_kind   TEXT NOT NULL,    -- 'interest' | 'tone' | 'topic' | 'preference'
    trait_value  TEXT NOT NULL,
    confidence   REAL NOT NULL,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    session_ids  TEXT NOT NULL    -- JSON array
);
CREATE INDEX idx_user_traits_user ON user_traits(user_id);
```

### NEW: `agent_state.sqlite`
```sql
CREATE TABLE agent_persona_state (
    agent_id      TEXT PRIMARY KEY,
    mood          TEXT NOT NULL DEFAULT 'neutral',
    fatigue       REAL NOT NULL DEFAULT 0.0,
    recent_topics TEXT NOT NULL DEFAULT '[]',  -- JSON, last N
    updated_at    INTEGER NOT NULL,
    state_json    TEXT NOT NULL DEFAULT '{}'   -- extension point
);
```

---

## 6. Configuration Additions

```toml
[evolution.budget]
weekly_total = 15
[evolution.budget.per_kind]
skill_update = 3
prompt_template = 1
tool_policy = 1
agent_card = 5
new_skill = 2

[evolution.shadow]
enabled = true
eval_set_dir = "/data/eval/evolution"
sandbox_kind = "in_process"   # 'in_process' | 'docker' (Phase 4)

[evolution.auto_rollback]
enabled = true
grace_window_hours = 72
[evolution.auto_rollback.thresholds]
default_err_rate_delta_pct = 2.0
default_p95_latency_delta_pct = 25.0

[evolution.enable_prompt_kind]
enabled = false   # opt-in even after Phase 3 — extra-careful kind

[memory.decay]
enabled = true
half_life_hours = 168   # 1 week
floor_score = 0.05      # below this is GC eligible
recall_boost = 0.3      # recalled chunk gets +0.3 score (capped 1.0)

[memory.consolidation]
enabled = true
schedule = "0 4 * * *"  # 04:00 daily
promotion_threshold = 0.65   # consolidate chunks above this score
max_promotions_per_run = 50

[user_model]
enabled = true
distill_after_session_turns = 5
trait_confidence_floor = 0.4
[user_model.distillation]
schedule = "0 5 * * *"   # right after consolidation

[persona]
enabled = true
mood_decay_per_hour = 0.05
fatigue_recovery_per_hour = 0.1
```

---

## 7. Dependencies & Sequencing

```
Phase 2 (done) ──► Wave 1 (safety) ──► Wave 2 (kinds)
                                            │
                            Wave 3 (cognition) ─┘
                            (can start in parallel with W1)

  Wave 4 (bonus) — anywhere after W1
```

Wave 1 must land before Wave 2's medium/high-risk handlers can be enabled (they need shadow_test + auto_rollback). Wave 3 is **independent of W1/W2** — different subsystems, can run in parallel from week 1.

If staffing allows: launch W1 + W3 together (week 1-2), then W2 (week 2.5-4), W4 floats.

---

## 8. Risk Matrix

| Risk | Likelihood | Mitigation |
|---|---|---|
| Skill extraction generates noise / low-quality proposals | High | Strict confidence threshold; per-week cap; eval-set must include "rejected" cases agent shouldn't propose |
| AutoRollback overreacts (false positive on metrics noise) | Medium | Require `n=2` consecutive degradation samples; metric-specific dead bands |
| User model leaks PII into prompts | High | Redaction pipeline before distillation; trait kinds whitelist (no raw quotes) |
| Persona state diverges from agent card → confusion | Medium | Persona is read-only at prompt-render time; mutations only via Engine proposals |
| Memory decay deletes chunks operator wanted | High | "consolidated" namespace + manual pin via UI before any GC; floor_score is removal-eligible not actual delete (Phase 3 just marks; actual GC behind a Phase 4 flag) |
| Eval set authoring is bottleneck | Medium | Bootstrap from production traces with redaction; encourage operator to grow set over time |

---

## 9. Open Questions (decision before W1)

1. ~~**Eval set authorship**: operator-written, prod-trace-distilled, or hybrid?~~ — **Decided 2026-04-27 (W1-A)**: hybrid — hand-crafted seed cases ship in-repo at `rust/crates/corlinman-shadow-tester/tests/fixtures/eval/<kind>/`; operators grow the set via approved flagged sessions. The bundled 4 `memory_op` cases are the seed.
2. ~~**Shadow sandbox isolation**: in-process eval vs docker sandbox?~~ — **Decided 2026-04-27 (W1-A)**: in-process for Phase 3, Docker reserved for Phase 4. Encoded in `EvolutionShadowConfig::sandbox_kind` (`ShadowSandboxKind::InProcess` | `Docker`); the `Docker` variant parses but is rejected on load until the runner supports it.
3. **AutoRollback metrics scope**: which metrics count? — **Decided 2026-04-27**: per-kind whitelist lives in code at `corlinman-auto-rollback::metrics::watched_event_kinds` (not config — config churn risks operators silently disabling the safety gate). Initial coverage ships only `memory_op → ['tool.call.failed', 'search.recall.dropped']`; new kinds extend the match arm as their handlers land. The `min_baseline_signals` config knob (default 5) is the quiet-target guard against `0 → 1 = +∞%` false positives; absolute thresholds (per-kind p95 latency, etc) wait for the kinds that emit those signals.
4. **User model granularity**: per-user, per-channel-session, per-conversation? — Lean: per-user keyed by `(channel, sender_id)` tuple; cross-channel join is Phase 4
5. **Persona mutability source-of-truth**: agent-card YAML vs runtime DB? — Lean: YAML is the seed/template; DB is the runtime state; on agent-card update via Evolution proposal, DB resets to YAML defaults
6. **Closed learning loop privacy**: skill_extraction sees full conversation text; how is sensitive content excluded? — Lean: same redaction pipeline as user_model; opt-in setting `[skills.allow_extraction_from_session]` defaulting false

---

## 10. Success Criteria for Phase 3 Exit

After 3-5 weeks the deployment should show:

- ✅ All 8 EvolutionKind values implementable (and at least 5 actively producing approved proposals weekly)
- ✅ ≥ 1 AutoRollback fires with valid reasoning during the 4-week window (validates the safety infra by exercising it)
- ✅ User model has ≥ 10 distilled traits per recurring user; demonstrably influences a `{{user.*}}` placeholder in a real prompt
- ✅ Memory decay shows measurable kb.sqlite size stability (chunks pruned ≈ chunks added)
- ✅ Operator can answer "what changed about the agent in the last 7 days?" via `/evolution` History tab in < 30s

If any of these miss, we don't bump to Phase 4 — we stabilize.

---

## 11. Anti-Goals (Phase 3 will NOT)

- Touch model weights / fine-tuning
- Implement multi-tenant evolution
- Auto-generate code in `rust/` or `python/` source
- Have agent self-modify its scheduler config (that's a Phase 4+ debate)
- Replace operator approval with full automation for any kind ≥ medium risk
- Ship a "DreamSystem" feature — its parts (reflection job, pending review, NL prose) are absorbed as primitives

---

## 12. Phase 4 Preview (out of scope, for context only)

- Multi-tenant federated evolution (cross-tenant skill sharing with explicit opt-in)
- Docker-isolated shadow sandbox for prompt/tool kinds
- Agent self-improving its own EvolutionEngine prompts (recursion guard required)
- Cross-channel user model unification
- Real Canvas Host + native client integration (per original plan doc)
- MCP (Model Context Protocol) compatibility layer
