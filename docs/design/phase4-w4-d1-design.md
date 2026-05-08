# Phase 4 W4 D1 — Episodic memory

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-05-08 · **Estimate**: 6-8d

> Below this point, memory is *what was said* (chunks, tags,
> signals). Above this point, memory is *what happened* — operator
> approved a `skill_update` for `web_search` on 2026-04-22 that fixed
> a 30-second timeout; user verified their Telegram alias on the QQ
> channel; auto-rollback fired on `engine_prompt:clustering` 14h
> after apply. Episodes are the event-level layer.

D1 is the seed for iteration. Pins schema, distillation window,
`{{episodes.*}}` surface, embeddings integration, importance
rubric. **D2 (`{{goals.*}}`) blocks on D1** — goals consume episode
summaries as grounding. D1 itself blocks on no Phase-4 task
(`phase4-roadmap.md:293-294`).

## Why this exists

Four memory layers today, each scoped tighter than the next:

1. **Per-message** in `sessions.sqlite` (`session_sqlite.rs:44-56`).
   Cheap to write, expensive to query — "what happened this week"
   returns 4 000 lines of raw dialog.
2. **Per-chunk** in `kb.sqlite`; W3-A consolidation promotes
   survivors to `consolidated` (`consolidation.py:90-110,178-205`).
   Topic-level, not event-level.
3. **Per-tag-cluster** in `corlinman-tagmemo` (boost / pyramid). Tag
   affinity, not narrative.
4. **Per-evolution-signal** in `evolution_signals`
   (`schema.rs:12-22`); clustering filters most away
   (`clustering.py:56-92`).

The gap is above-message, below-history, **narrative-shaped**. "The
operator approved a skill_update for web_search that fixed timeout"
joins a `tool_invocation_failed` signal + `evolution_history` apply
row + the operator's approval turn. No layer carries that join. The
Wave 4 acceptance line (`phase4-roadmap.md:308-311`) asks the agent
to recall exactly that sentence.

An episode is a **frozen, summarised, embedded story** — durable,
queryable, importance-ranked, tenant-scoped.

## Episode shape — schema

`<data_dir>/tenants/<slug>/episodes.sqlite` (per-tenant; matches
the directory layout decision in `phase4-roadmap.md:341`):

```sql
CREATE TABLE IF NOT EXISTS episodes (
    id              TEXT PRIMARY KEY,           -- ULID-like; sortable
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    started_at      INTEGER NOT NULL,           -- unix-ms; earliest source row
    ended_at        INTEGER NOT NULL,           -- unix-ms; latest source row
    kind            TEXT NOT NULL,              -- see EpisodeKind below
    summary_text    TEXT NOT NULL,              -- LLM-distilled narrative (1-3 paragraphs)
    source_session_keys TEXT NOT NULL,          -- JSON array of session_keys touched
    source_signal_ids   TEXT NOT NULL,          -- JSON array of evolution_signals.id
    source_history_ids  TEXT NOT NULL,          -- JSON array of evolution_history.id
    embedding       BLOB,                       -- nullable; populated by post-distill embed pass
    embedding_dim   INTEGER,                    -- 384 (BGE-small) / 1024 / etc; matches embedding router
    importance_score REAL NOT NULL DEFAULT 0.5, -- 0..1; see §Importance scoring
    last_referenced_at INTEGER,                 -- unix-ms; updated each {{episodes.*}} hit
    distilled_by    TEXT NOT NULL,              -- provider alias used for the LLM call
    distilled_at    INTEGER NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_episodes_tenant_ended
    ON episodes(tenant_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_tenant_importance
    ON episodes(tenant_id, importance_score DESC, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_kind
    ON episodes(tenant_id, kind, ended_at DESC);

CREATE TABLE IF NOT EXISTS episode_distillation_runs (
    -- Idempotency log — every distillation pass writes one row,
    -- regardless of how many episodes it minted.
    run_id          TEXT PRIMARY KEY,           -- ULID
    tenant_id       TEXT NOT NULL,
    window_start    INTEGER NOT NULL,           -- unix-ms; right-inclusive
    window_end      INTEGER NOT NULL,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,                    -- nullable while running
    episodes_written INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,              -- 'running' | 'ok' | 'failed' | 'skipped_empty'
    error_message   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_distillation_window
    ON episode_distillation_runs(tenant_id, window_start, window_end);
```

`UNIQUE(tenant_id, window_start, window_end)` is the load-bearing
idempotency guard — a re-run on the same window short-circuits or
returns the prior run id. A crashed mid-distill row
(`status='running'`, no `finished_at`) is swept and retried, same
pattern as `apply_intent_log` (`phase4-roadmap.md:159-163`).

`EpisodeKind` (Python `StrEnum`, Rust `strum` mirror for read-side):

```python
class EpisodeKind(StrEnum):
    CONVERSATION = "conversation"   # session range with no special signals
    EVOLUTION    = "evolution"      # ≥1 evolution_history apply touched
    INCIDENT     = "incident"       # severity=error/critical signals dominate
    ONBOARDING   = "onboarding"     # first-N sessions of a new user_id
    OPERATOR     = "operator"       # admin acted (approve/deny/manual merge)
```

The kind biases the LLM prompt segment used for distillation
(§Distillation job). Adding a kind is purely additive — register a
prompt segment + classifier rule.

## What gets distilled

A pass over `[window_start, window_end)` joins five streams per
tenant:

1. **Session messages** in `sessions.sqlite` with `ts ∈ window`,
   grouped by `session_key`; floor `min_session_count_per_episode`
   (default 1).
2. **Evolution signals** (`schema.rs:12-22`) with `observed_at ∈
   window`; signal → session via `session_id` (`schema.rs:19`).
3. **Evolution history** apply/revert/auto-rollback rows; joins to
   signals via the proposal's `signal_ids` JSON.
4. **Hook events** with kind ∈ `{evolution_applied, tool_approved,
   tool_denied, error, auto_rollback_fired, identity_unified}`.
   Phase 4 W1.5 wired `HookEvent.tenant_id`
   (`phase4-roadmap.md:218-220`).
5. **Identity merges** — `verification_phrases` rows consumed
   in-window (`phase4-w2-b2-design.md:69-79`); narratively load-
   bearing ("user X now == user Y on Telegram").

Operator manual annotations are **out-of-scope for D1** — admin-
write surface needs UI + audit; defer to D1.5. Source rows are
**never deleted**; episodes are an additive read-model.

## Distillation job

Lives in `corlinman-episodes` (Python). Pattern shamelessly
copied from `consolidation.py:90-167` since clustering +
batch-summary jobs already share that runner shape.

```
corlinman-episodes/
├── pyproject.toml
├── src/corlinman_episodes/
│   ├── __init__.py
│   ├── config.py            # EpisodesConfig (dataclass, mirrors [episodes] TOML)
│   ├── store.py             # SQLite open + schema bootstrap; idempotency CRUD
│   ├── sources.py           # join sessions/signals/history/hooks/identity
│   ├── distiller.py         # LLM call → summary_text; PII redactor pass
│   ├── classifier.py        # heuristic EpisodeKind picker (signals dominate, etc.)
│   ├── importance.py        # see §Importance scoring
│   ├── embed.py             # corlinman-embedding router call
│   ├── runner.py            # episodes_run_once(*, config, …) → RunSummary
│   └── cli.py               # argparse — `corlinman-episodes distill-once`
└── tests/
    └── test_runner_idempotency.py
```

**Trigger**: cron `[episodes] schedule = "0 6 * * * *"` (daily
06:00 UTC, `phase4-roadmap.md:380-383`) wired through the prod
`corlinman-scheduler` (`scheduler/src/lib.rs:1-23`; in prod since
Phase 3.1 / `phase4-roadmap.md:164-169`). On-demand: `POST
/admin/tenants/:tenant/episodes/distill` spawns a one-shot CLI
invocation; operator UI button (D1 iter 9).

**Window**: `window_end = now`; `window_start = max(now -
distillation_window_hours, last_ok_run.window_end)`. If the
result is smaller than `min_window_secs` (default 1h) → skip
(`status='skipped_empty'`).

**Method**:
1. `sources.collect(window)` → per-`session_key` bundles carrying
   messages + linked signals + history + hooks. Empty bundles
   dropped.
2. `classifier.classify(bundle)` picks an `EpisodeKind` (rule
   precedence: `auto_rollback_fired` → `INCIDENT`; any apply →
   `EVOLUTION`; operator approve → `OPERATOR`; first-N for user →
   `ONBOARDING`; else `CONVERSATION`).
3. `distiller.distill(bundle, kind)` calls the LLM via
   `corlinman-providers::registry::resolve` (`registry.py:97,191`).
   System prompt is `episodes/prompts/<kind>.md` (one segment per
   kind, persona-crate style). PII redactor (Phase 3.1 Tier 3 /
   S-1, `phase4-roadmap.md:148-152`) runs over the bundle **and**
   the LLM output.
4. `importance.score(bundle, kind)` — see §Importance scoring.
5. `store.insert_episode(...)` writes with `embedding=NULL`. A
   second pass (`embed.populate_pending`) backfills via the
   router (`router.py:44`). Splitting summary-write from
   embed-write keeps a remote-embedding outage non-blocking.

**Idempotency**: re-run on the same `(tenant, window_*)` hits the
unique index → returns prior `run_id`. The per-bundle insert is
additionally guarded by a `(tenant_id, started_at, ended_at,
kind)` natural-key probe so a half-flushed crashed run never
double-mints.

## Query surface — `{{episodes.*}}`

The placeholder cascade lives in the Rust gateway
(`PlaceholderEngine`, `placeholder.rs:48-50,126-198`). Reserved
namespaces today: `var`, `sar`, `tar`, `agent`, `session`, `tool`,
`vector`, `skill`. **D1 adds `episodes`** to `RESERVED_NAMESPACES`.

The resolver lives in
`corlinman-gateway/src/placeholder/episodes.rs` (new), implements
`DynamicResolver` (`placeholder.rs:120-122`), and reads
`episodes.sqlite` via the admin-routes pool.

Supported keys (everything after `episodes.`):

| Token | Behaviour |
|---|---|
| `{{episodes.last_week}}` | Top 5 by `importance_score DESC` over `ended_at >= now - 7d`; rendered as a markdown bullet list of `summary_text` truncated to 240 chars each. |
| `{{episodes.last_24h}}`, `{{episodes.last_month}}` | Same shape, different window. |
| `{{episodes.recent}}` | Last `max_episodes_per_query` (default 5) by `ended_at DESC` regardless of importance. |
| `{{episodes.about(<tag>)}}` | Filter by tag — joined via `corlinman-tagmemo` boost path; episodes whose source signals carry the tag rank first. Argument syntax matches the existing `{{toolbox.NAME}}` shape (`context_assembler.py:77`). |
| `{{episodes.kind(<kind>)}}` | Filter by `EpisodeKind`; e.g. `{{episodes.kind(incident)}}`. |
| `{{episodes.about_id(<id>)}}` | Single episode by id; for citation in agent answers. |

Each render stamps `last_referenced_at = now` on hit rows (one
batched UPDATE per render). This drives §Decay pruning.

`PlaceholderCtx.session_key` (`placeholder.rs:80-99`) carries the
session; the resolver reads `tenant_id` from
`ctx.metadata["tenant_id"]` (the gateway middleware already
stamps it for all reserved-namespace renders — same path as
`{{vector.*}}`).

## Importance scoring

Computed once at distillation time, never updated — a 3-month-old
episode shouldn't shift rank because a signal kind got re-weighted.
Score is `clip(sum(weights), 0, 1)`:

| Component | Weight | Source |
|---|---|---|
| Per-source-signal density | `+0.05` per signal up to 10 (cap 0.5) | `len(source_signal_ids)` |
| Evolution apply outcome | `+0.2` per applied + `+0.1` per auto-rollback | `evolution_history` join |
| Severity = critical | `+0.3` (single hit) | signal max severity |
| Severity = error | `+0.15` (single hit) | signal max severity |
| Operator action present | `+0.1` | hook event `tool_approved` / `evolution_applied` |
| Identity unified | `+0.15` | identity merge in window |
| Onboarding kind | `+0.1` baseline | first-N for user_id |

Defaults under-weight high-volume / low-novelty conversation; the
0.0 floor is fine — episodes still get written, they just sort
last. Operator override (single-episode bump) is D1.5 admin work.

## Embeddings + retrieval

The `embedding` BLOB lives on the row, not in a sidecar vector
table — projected rowcount is low (3-tenant × daily for 12 months
≈ 3 600 rows). Linear scan after tag filtering is fine for v1; a
sidecar index becomes v2 if it bites.

The embedding router (`router.py:44-100`) is config-driven;
`[episodes]` carries its own alias so a small model can serve
episodes while chunk retrieval keeps a larger one. Dimension is
asserted on write (`router.py:92-99`); a mismatch hard-errors
rather than silently truncating.

`{{episodes.about(<tag>)}}` is two-stage: tag-filtered candidates
first; if the tag carries a query phrase, cosine rerank against a
once-per-render embed of the phrase. Both stages cap at
`max_episodes_per_query`.

## Decay / pruning

**Never delete** — episodes are the audit trail; deleting breaks
downstream provenance. Cold storage is fair game.

- `last_referenced_at` updates on every `{{episodes.*}}` hit.
- After **180 days unreferenced**, `summary_text` + `embedding`
  move to `<data_dir>/tenants/<slug>/episodes_cold/<id>.md`; the
  row remains with NULL hot columns + sentinel
  `summary_text='<archived:see cold>'`. Reads transparently
  re-hydrate (one-render latency penalty); writes never touch
  cold.
- `corlinman-episodes rehydrate-all` CLI flag forces hot
  promotion (pre-migration use).
- Auto-rollback episodes + operator-flagged `important=true`
  (D1.5) are exempted from cold archival.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `distillation_idempotent_on_same_window` | runner | Two `episodes_run_once` on identical window → one episode minted, second returns existing run id |
| `distillation_resumes_after_crash` | runner | Inject `status='running'` + null `finished_at` row → runner sweeps + retries window |
| `importance_score_pure_function_of_inputs` | importance | Same bundle → same score across runs |
| `importance_ranks_incident_over_chitchat` | importance | One `auto_rollback_fired` window vs one chat-only window → first ranks higher |
| `placeholder_episodes_last_week_top5_by_importance` | resolver | Inserted 7 episodes (varying importance) → render returns top 5 in score-desc order |
| `placeholder_episodes_about_filters_by_tag` | resolver | Tag-tagged episode + untagged → only first surfaces |
| `placeholder_episodes_kind_filter` | resolver | Mixed kinds → only requested kind returned |
| `placeholder_unknown_episode_token_round_trips` | resolver | `{{episodes.gibberish}}` returns literal in `unresolved_keys` |
| `tenant_isolation` | resolver | Tenant A renders episodes; tenant B's rows never surface |
| `last_referenced_updated_on_hit` | resolver | Pre-render `last_referenced_at` < post-render |
| `large_window_batches_in_chunks` | runner | 30-day window with 200 sessions → memory bounded; multiple LLM calls sharded by `max_messages_per_call` |
| `embedding_failure_persists_episode_with_null_vector` | embed | Remote embedding 503 → episode row written, embedding NULL, retried on next sweep |
| `pii_redactor_runs_pre_and_post_llm` | distiller | Phone/email in source bundle never appears in `summary_text` |
| `cold_archive_after_180d_unreferenced` | store | Time-warped row → hot columns null, cold file present, reads still work |
| `cold_rehydrate_on_reference` | store | Cold row hit by resolver → row promoted back to hot |
| `e2e_acceptance_recall_skill_update_episode` | integration | Synthetic apply trail: `tool_invocation_failed:web_search` signal → operator approves `skill_update` → applier writes; daily distill; `{{episodes.last_week}}` includes "operator approved … web_search … fixed timeout" |

The last test is the Wave 4 acceptance line lifted verbatim from
`phase4-roadmap.md:308-311`.

## Configuration

```toml
[episodes]
enabled = true
schedule = "0 6 * * * *"
distillation_window_hours = 24
min_session_count_per_episode = 1
min_window_secs = 3600                 # short-circuit cron-collision empty windows
max_messages_per_call = 60             # LLM context budget per distill call
llm_provider_alias = "default-summary" # corlinman-providers registry key
embedding_provider_alias = "small"     # corlinman-embedding router alias
max_episodes_per_query = 5
last_week_top_n = 5
cold_archive_days = 180
run_stale_after_secs = 1800            # crash sweeper threshold
```

Defaults match Wave 4's "ship a working surface; tune later" stance.

## Open questions

1. **Provider for distillation** — operator-set or auto-pick
   cheapest? Lean **operator-set alias `default-summary`**,
   default-shipped pointing at the same provider as the prompt-
   template handler so a fresh install works without
   `episodes.toml`. `registry.py:97,191` already supports alias
   lookup.
2. **Sessions crossing an episode boundary** — a 3-5am session
   straddles a 06:00 UTC distillation. Lean: **half-in / half-out
   is fine** — window is `(start, end]`; messages past `end` join
   the next window for that session and produce a part-2 episode
   referencing the same `session_key`. Prompt segment includes a
   "this is part 2 of session X" hint.
3. **Double-counting evolution signals** — the same signal feeds
   `EvolutionSignalCluster` (`engine.py`) and an episode's
   `source_signal_ids`. Lean **no double-spend**: episodes are a
   read-model, signals stay primary; importance weights *outcome*
   (apply / rollback), so a noisy un-clustered signal doesn't
   inflate episode rank.
4. **Embedding model migration** — operator changes
   `embedding_provider_alias`; old rows have wrong dim. Lean: a
   D1.5 `reembed --since=<ts>` CLI subcommand; D1 hard-errors at
   query time on dim mismatch.
5. **Cold archive encryption-at-rest** — out of D1; matches the
   current treatment of `kb.sqlite`.

## Implementation order (suggested for autonomous iterations)

Each numbered item is a single bounded iteration (~30 min – 2 hours):

1. **`corlinman-episodes` package skeleton + schema** —
   `pyproject.toml`, `__init__.py`, `config.py` with
   `EpisodesConfig` dataclass, `store.py` with `SCHEMA_SQL` +
   `open_episodes_db()` (idempotent `CREATE … IF NOT EXISTS`),
   `EpisodeKind` enum. Tests: schema round-trip, idempotent re-open.
2. **`store.insert_episode` + idempotency log** —
   `episode_distillation_runs` CRUD, unique-index race test, sweep
   stale-`running` rows. 5 tests including a 16-way concurrent
   first-call.
3. **`sources.collect`** — multi-stream join (sessions, signals,
   history, hooks, identity) over a window; per-`session_key`
   bundle dataclass; tenant-scoped. 6 tests including
   tenant-isolation + empty-window + N-stream join.
4. **`classifier.classify` + `importance.score`** — pure functions
   over a bundle; default rules per §Importance scoring. 8 tests
   covering each kind precedence + each weight.
5. **`distiller.distill` w/ stub provider** — wire
   `corlinman-providers::resolve(alias)`; per-kind prompt segment
   load from `episodes/prompts/<kind>.md`; PII redactor pre+post.
   3 tests with a deterministic stub provider.
6. **`embed.populate_pending`** — second-pass embedding writer via
   `corlinman-embedding::EmbeddingRouter`; null-vector fallback on
   remote failure. 4 tests including dim-mismatch + 503 retry.
7. **`runner.episodes_run_once` end-to-end** — wire all pieces +
   crash-resume sweep. CLI subcommand `distill-once`. 5 tests
   including the full happy path on a synthetic fixture DB.
8. **Rust gateway resolver** — `corlinman-gateway/src/placeholder/episodes.rs`
   implementing `DynamicResolver`; register on
   `RESERVED_NAMESPACES` (`placeholder.rs:48-50`); wire in gateway
   `main.rs` boot path next to existing namespace registrations. 8
   tests covering each query token + tenant isolation +
   unknown-token literal.
9. **Operator on-demand route + UI button** — `POST
   /admin/tenants/:tenant/episodes/distill` spawning the CLI;
   `/admin/tenants/:tenant/episodes` list page with importance +
   kind filter; manual distill button. 6 backend + 4 UI tests.
10. **E2E acceptance test** — synthetic
    `tool_invocation_failed:web_search` signal → approve →
    applier writes → daily distill → `{{episodes.last_week}}`
    contains the expected sentence. The Wave 4 acceptance line in
    one integration test (`phase4-roadmap.md:308-311`).

## Out of scope (D1)

- **D2 — goal hierarchies** (`{{goals.*}}`, weekly self-grading).
  Goals consume episodes; designed under D2. **D2 blocks on D1.**
- **D3 — subagent delegation runtime.** Children may write to
  parent episodes, but spawn/lifetime contract is D3.
- **D4 — voice surface.** Voice produces sessions like any other
  channel; episodes pick them up via `sessions.sqlite`.
- **Operator manual annotations.** Admin-write surface + audit;
  defer to D1.5.
- **Auto-replay against episodes.** Trajectory replay is W2-D
  (`phase4-roadmap.md:266`); episodes don't ship a replay verb.
- **Topic-shift episode boundaries** (mid-session splitting). Per
  Phase 4 OQ 6 (`phase4-roadmap.md:413`), per-session granularity
  is v1; topic-shift is Phase 5.
- **Cross-tenant episode federation.** Tenant boundary is the
  isolation boundary; not requested by Wave 4 acceptance.
