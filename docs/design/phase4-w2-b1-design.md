# Phase 4 W2 B1 — Meta proposal kinds

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-04-30 · **Estimate**: 7-9d

> The Engine learns to improve the Engine. Four new `EvolutionKind`s
> mutate the proposer itself: clustering prompts, engine config,
> observer signal filters, cluster thresholds. Highest blast radius in
> the codebase — every guard rail (recursion, approval, rollback
> window) is tightened proportionally.

Design seed for the iterations that follow. Pins schema, applier
dispatch, recursion guard, operator-only auth, the double-confirm
token round-trip, the tightened auto-rollback window, and the
`meta_pending` admin tab. Mirrors `phase4-w2-b2-design.md` in shape.

## Why this exists

Today the Engine produces eight `EvolutionKind` variants
(`rust/crates/corlinman-evolution/src/types.rs:45-54`), all mutating
**agent assets** (memory, tags, skills, prompts, tools, agent-cards).
None mutate the **proposer itself**. The roadmap (`phase4-roadmap.md`
§4 Wave 2 row 4-2A) calls for the recursive leap: Engine becomes a
target of EvolutionLoop, behind a one-level recursion guard with
operator-only approval. B1 unlocks the loop: a clustering-prompt
regression spotted by the operator becomes an `engine_prompt`
proposal that rewrites the prompt the next Engine pass uses. With
guards, bounded self-improvement; without them, divergence amplifies.

## The four kinds — what each modifies

All four new variants live in `EvolutionKind` alongside the existing
eight. Per-tenant on disk, mirroring the `prompt_template` /
`agent_card` layout from
`evolution_applier.rs:911-1101`:

| Kind | On-disk target | Diff shape | Inverse_diff |
|---|---|---|---|
| `engine_config` | `<data_dir>/tenants/<t>/engine/config.toml`, key path in `target` (e.g. `clustering.similarity_threshold`) | `{ before: <toml-value>, after: <toml-value>, rationale }` (typed scalar / list) | `{ op: "engine_config", tenant, key, before_value, before_present }` |
| `engine_prompt` | `<data_dir>/tenants/<t>/engine/prompts/<segment>.md` (e.g. `clustering`, `summarize_signals`) | `{ before, after, rationale }` (full text, like `prompt_template`) | `{ op: "engine_prompt", tenant, segment, before, before_present }` |
| `observer_filter` | `<data_dir>/tenants/<t>/engine/observer_filter.toml` (allow/deny list of `event_kind` patterns) | `{ before: [<rules>], after: [<rules>], rationale }` | `{ op: "observer_filter", tenant, before_rules, before_present }` |
| `cluster_threshold` | `<data_dir>/tenants/<t>/engine/cluster_thresholds.toml`, single key (per-`event_kind` float) | `{ before: <float>, after: <float>, key, rationale }` | `{ op: "cluster_threshold", tenant, key, before_value, before_present }` |

**Separate `engine/` subtree** vs reusing `prompt_segments/`: the
persona crate consumes `prompt_segments/` as a chat-time override
layer. Engine-internal prompts are read by the Python
`corlinman-evolution-engine` package on its scheduled run
(`engine.py:120,241`). Sharing the subtree conflates "chat-time"
with "engine introspection" and muddles the recursion guard.

**Format**: TOML for config / threshold / filter (typed, diff-friendly,
matches `tool_policy.toml`); Markdown for `engine_prompt` (matches
`prompt_segments/` so the atomic tmp+rename + canonicalise pattern at
`evolution_applier.rs:967-986` carries over).

## Schema additions

One column on `evolution_proposals` and one boolean predicate. Migration
appended to `MIGRATIONS` in
`rust/crates/corlinman-evolution/src/schema.rs:149`:

```sql
-- v0.4 → v0.5: meta proposal flag.
ALTER TABLE evolution_proposals
  ADD COLUMN is_meta INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_evol_proposals_meta_status
  ON evolution_proposals(is_meta, status, created_at);
```

`is_meta = 1` iff `kind ∈ {engine_config, engine_prompt,
observer_filter, cluster_threshold}`. The Rust helper
`EvolutionKind::is_meta()` is the source of truth; the column is a
denormalised cache so the meta-tab query hits an index instead of
scan-and-reparse on `kind`. `ProposalsRepo::insert` populates it from
`kind.is_meta()`; no caller changes.

## Public Rust types

```rust
// rust/crates/corlinman-evolution/src/types.rs (additions to EvolutionKind)
pub enum EvolutionKind {
    // ... existing eight ...
    EngineConfig,
    EnginePrompt,
    ObserverFilter,
    ClusterThreshold,
}

impl EvolutionKind {
    /// True iff this kind mutates the Engine itself (vs an agent
    /// asset). Used by the recursion guard, the operator-only
    /// auth check, and the `is_meta` column.
    pub fn is_meta(&self) -> bool {
        matches!(
            self,
            Self::EngineConfig
                | Self::EnginePrompt
                | Self::ObserverFilter
                | Self::ClusterThreshold,
        )
    }
}
```

## Recursion guard — what "one level" means

Two complementary checks:

1. **No meta-from-meta** (semantic). At propose time (Python engine):
   refuse to emit a meta-kind proposal when any source signal's
   `trace_id` matches a previously-applied meta proposal's audit
   chain. SQL: `WHERE is_meta = 0` is added to the
   signals→cluster→propose path inside the meta handler.

2. **Quiet period** (temporal). At apply time: a meta proposal cannot
   be applied within `meta.cooldown_hours` (default **1h**) of the
   most recent meta apply on the same `(tenant_id, kind)`. Applier
   reads `MAX(applied_at) FROM evolution_proposals WHERE is_meta = 1
   AND tenant_id = ? AND kind = ? AND status = 'applied'`; if `now -
   max < cooldown`, returns `ApplyError::MetaCooldown { until_ms }`.
   Catches "human approves two `engine_prompt` proposals back-to-back"
   even though they aren't engine-generated-from-engine-output.

The guard is **human-immutable** (`phase4-roadmap.md:328-330`): no
proposal kind mutates the guard. Config lives in `corlinman.toml`
only — no override under `tenants/<t>/engine/`.

## Operator-only approval — the auth gate

Today the admin surface has a single role
(`middleware/admin_auth.rs:48`, one user/password pair). B1 introduces
a **capability flag**, not a new user table:

- `corlinman.toml` adds `[admin] meta_approver_users = ["alice"]` —
  admin usernames permitted to approve/deny meta proposals.
- `require_admin` middleware stamps the resolved username on request
  extensions (`AdminPrincipal { username }`).
- `approve_proposal` / `deny_proposal` load the proposal first; if
  `proposal.kind.is_meta()` and `principal.username ∉
  meta_approver_users`, **403** `{ "error":
  "meta_requires_operator" }`.

Default `meta_approver_users = []` means a fresh deployment has *no
one* who can approve meta — operators must opt in. Safe failure mode:
queues forever rather than auto-approving.

Enforcement lives at the **route layer** + **defence-in-depth check
inside `EvolutionApplier::apply`** (a future CLI bypassing routes
still fails). Applier returns `ApplyError::OperatorRequired`.

## Double-confirm UX for `engine_prompt`

The highest-blast-radius variant: a single approve flips the prompt
the Engine itself uses next pass. The UX adds a server-issued
**confirmation token** (mirrors CSRF tokens but scoped to one
proposal):

```text
POST /admin/evolution/:id/approve  (engine_prompt only)
  → 200 { "stage": "confirm_required",
          "confirmation_token": "<opaque-32-byte-base64>",
          "expires_at": <unix-ms-+5min>,
          "diff_preview": <first 400 chars of after> }

POST /admin/evolution/:id/approve  (with token in body)
  body: { "decided_by": "alice",
          "confirmation_token": "<same opaque>",
          "second_confirm": "I understand this rewrites Engine prompts" }
  → 200 { "id": "...", "status": "approved" }
```

Tokens stored in-memory keyed by `(proposal_id, principal)`, 5-min
TTL, single-use, scoped to the issuing admin. UI: first POST shows a
modal with diff + the literal `second_confirm` text the operator
types verbatim; second POST sends both. Reused / expired / wrong-user
→ **409** `{ "error": "confirmation_token_invalid" }`.

Double-confirm wired **only** for `engine_prompt`. Configurable via
`[evolution.meta] require_double_confirm = ["engine_prompt"]` so an
operator can extend.

## Tightened `auto_rollback` window

The roadmap (`phase4-roadmap.md:283`) calls for a **24h** window on
meta proposals (vs the default 72h on non-meta —
`config.rs:1374`). Two implementation paths considered:

| Option | Pros | Cons |
|---|---|---|
| Per-kind override in `EvolutionAutoRollbackConfig` | Operators can tune any kind | Bigger config surface; extra validation |
| Hardcoded "if `is_meta`, divide by 3" | Trivial to ship | Inflexible; magic constant |

**Decision**: per-kind override via a single `meta_grace_window_hours`
field defaulting to 24. `AutoRollbackMonitor::run_once`
(`corlinman-auto-rollback/src/monitor.rs:96`) calls
`list_applied_in_grace_window` once for non-meta (72h) and once for
meta (24h), unioning candidates. `watched_event_kinds`
(`metrics.rs:62`) extends to the four meta kinds — each watches
`evolution.proposal.shadow_failed` and a new
`evolution.engine.cluster_yield_dropped` (engine emits when a
post-meta-apply pass yields fewer clusters than its 3-pass moving
average).

Config:

```toml
[evolution.auto_rollback]
grace_window_hours = 72            # existing
meta_grace_window_hours = 24       # new
```

## `meta_pending` admin tab

Filter parameter on the existing list endpoint, not a new route:

```text
GET /admin/evolution?status=pending&filter=meta
GET /admin/evolution?status=pending&filter=non_meta   # default for back-compat
GET /admin/evolution?status=pending&filter=all
```

Implementation: `ListQuery` (`routes/admin/evolution.rs:99`) gains a
`filter: Option<String>` field. `list_proposals` adds
`AND is_meta = ?` to the SQL when filter is `meta` / `non_meta`. The
new `idx_evol_proposals_meta_status` index covers the query.

UI: `/admin/evolution` gains a tab strip — `Pending (N)` | `Meta
Pending (M)` | `Applied` | `History`. Counts via one `count` call
per tab. Meta Pending shows a banner: "These proposals modify the
Engine itself. Approval requires operator role; engine_prompt
requires double-confirm."

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `kind_is_meta_correct_for_all_variants` | types | All four new variants report `is_meta() == true`; existing eight report `false` |
| `apply_engine_config_round_trip` | applier | TOML key write + read-back; `inverse_diff` carries prior value |
| `apply_engine_prompt_round_trip` | applier | atomic tmp+rename; before/after sha differ; inverse_diff round-trips |
| `apply_observer_filter_round_trip` | applier | rule list write + parse + revert |
| `apply_cluster_threshold_round_trip` | applier | float key write + revert restores prior |
| `revert_engine_prompt_restores_byte_for_byte` | applier | re-apply inverse_diff matches `before_sha` |
| `recursion_guard_rejects_meta_from_meta_signals` | engine (Python) | propose call refuses to emit a meta proposal when source signals descend from a previously-applied meta trace |
| `meta_cooldown_blocks_second_apply_within_window` | applier | second meta apply within 1h returns `MetaCooldown` |
| `approve_meta_without_operator_role_403s` | route | non-operator admin gets 403 with `meta_requires_operator` |
| `apply_meta_without_operator_role_403s_at_applier` | applier | direct `EvolutionApplier::apply` call still rejects |
| `engine_prompt_approve_first_call_returns_token` | route | first POST returns 200 with `stage: confirm_required` + token |
| `engine_prompt_approve_second_call_consumes_token` | route | second POST with token transitions to `approved` |
| `engine_prompt_approve_token_reuse_rejected` | route | reusing a consumed token returns 409 |
| `engine_prompt_approve_token_expired_rejected` | route | TTL+1m elapsed → 409 |
| `engine_prompt_approve_token_wrong_user_rejected` | route | different admin → 409 |
| `auto_rollback_uses_24h_window_for_meta` | monitor | meta proposal applied 30h ago is **not** in candidate list (would have been at 72h) |
| `auto_rollback_uses_72h_window_for_non_meta` | monitor | non-meta proposal at 50h still picked up |
| `list_proposals_filter_meta_returns_only_meta` | route | filter=meta excludes non-meta rows |
| `list_proposals_filter_non_meta_default` | route | filter omitted = old behaviour preserved |
| `is_meta_column_backfills_via_migration` | repo | legacy DB w/o column gets ADDed; existing rows backfill 0; new meta inserts write 1 |

## Open questions for the implementation iteration

1. **Recursion guard scope**: full transitive descent or direct parent
   only? Recommendation: **full transitive via `trace_id`
   propagation** — any signal whose `trace_id` matches an applied
   meta's `trace_ids` is a descendant. Cheaper than ancestry walking;
   correct for one-level.
2. **Operator-only enforcement site**: route or applier? Both —
   applier authoritative, route friendlier 403. Same pattern as
   `tool_policy` drift check.
3. **Meta cooldown granularity**: per-tenant `(tenant_id, kind)` —
   a bug in one tenant's clustering prompt doesn't block another's.
4. **`observer_filter` semantics**: deny-list (default allow). The
   observer ingests everything today; filter narrows.

## Implementation order (suggested for autonomous iterations)

Each numbered item is a single bounded iteration (~30 min - 2 hours):

1. **Schema migration + `EvolutionKind` extension** — append the four
   variants to the enum (`types.rs`), add `is_meta()`, append the
   `is_meta` column migration to `MIGRATIONS`, add the index. Tests:
   `kind_is_meta_correct_for_all_variants` + migration backfill test.
2. **`ProposalsRepo` writes `is_meta`** — `insert` and any test
   fixtures populate the column from `kind.is_meta()`. Add a
   `list_pending_meta` helper + `list_pending_non_meta`. Tests:
   `is_meta_column_set_on_insert` + filter helpers round-trip.
3. **Applier handlers — `engine_config` + `cluster_threshold`** — both
   TOML files; share a `apply_engine_toml_key` helper. Forward +
   revert handlers, `inverse_diff` shapes, TOCTOU re-check. Tests:
   round-trip + revert per kind.
4. **Applier handlers — `engine_prompt` + `observer_filter`** — markdown
   file (mirrors `apply_prompt_template`) + TOML rule list. Forward +
   revert. Tests: round-trip + revert per kind, byte-for-byte
   restoration.
5. **Recursion guard — Python proposer side** — meta handler reads
   source signals, rejects when any descend from an applied meta
   trace. Tests: positive (clean signals → propose ok) + negative
   (meta-tainted → refuse).
6. **Recursion guard — applier cooldown** — `MetaCooldown` error +
   per-kind/tenant max(applied_at) lookup. Config field
   `[evolution.meta] cooldown_hours = 1`. Tests:
   `meta_cooldown_blocks_second_apply_within_window` +
   `meta_cooldown_does_not_block_after_expiry`.
7. **Operator-only auth gate** — `[admin] meta_approver_users` config
   field; `AdminPrincipal` extension; route + applier checks. Tests:
   approve, deny, and apply paths each reject non-operator with
   correct error.
8. **Double-confirm token** — in-memory token store (`DashMap` keyed
   by `(proposal_id, principal)`); 5-min TTL; first/second-call route
   logic for `engine_prompt`. Tests: token issue, consume, reuse,
   expiry, wrong-user.
9. **Tightened auto-rollback window** — `meta_grace_window_hours`
   config; `AutoRollbackMonitor` runs two passes (meta + non-meta).
   Extend `watched_event_kinds` for the four meta kinds. Tests:
   24h-window-for-meta + 72h-still-applies-non-meta.
10. **Admin list filter + UI tab** — `?filter=meta` query param;
    UI tab strip with counts. Tests: route filter + UI render
    (snapshot test against the existing admin React harness).

## Out of scope (B1)

- **Cross-tenant meta sharing** — federation belongs in B3
  (`phase4-next-tasks.md`). Meta proposals stay inside the tenant.
- **Engine self-modifies recursion guard** — forbidden
  (`phase4-roadmap.md:328-330`). Guard config is not a target.
- **Meta-meta proposals** — no `engine_recursion_guard_config`
  kind; defeats human-immutability.
- **Per-meta-kind shadow eval set** — meta kinds inherit
  `in_process`; `docker` not required (no agent code runs). Future
  iteration can diff cluster yield before/after.
