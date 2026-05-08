# Phase 4 W4 D2 — Goal hierarchies

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-05-08 · **Estimate**: 5-7d

> Agent grows a spine. Three tiers — today (24h), this week (Mon-Sun),
> this quarter (90d) — stored in `agent_goals.sqlite`. A nightly
> reflection job grades each 0-10 against D1 episodes, citing ids that
> justify the score. Prompts pull `{{goals.today/weekly/quarterly/
> failing}}` next to `{{persona.*}}` and `{{user.*}}`. A weekly score
> < 5 is the kindling the evolution loop turns into a `skill_update`
> proposal.

Code lands in a new Python package, **`corlinman-goals`**, mirroring
`python/packages/corlinman-persona/src/corlinman_persona/`:
`store.py` (schema + CRUD), `placeholders.py` (resolver, parallels
`corlinman_persona/placeholders.py:77-121`), `reflection.py` (LLM
grader), `cli.py` (`set`/`list`/`edit`/`archive`), `evaluator.py`
(cascade aggregation).

## Why this exists

An operator who wants to point the agent at "become competent at
infrastructure topics" has one lever today: the system prompt
(`corlinman-persona/seeder.py:1-149`; `prompt_segments/` for the
tenant override). System prompts are good at posture, bad at
progress — they tell the agent *what to be*, not *how it's doing*.
The reflection loop has no stable target; last week's numerator
drifts as prompt edits compound.

Goal storage breaks that. A goal is an immutable noun the grader can
re-evaluate over time; the same goal-row scored on day 7 vs day 28 is
the only honest signal of "did the agent improve." Two consequences:

- **Self-grading needs a stable target.** D1 episodes (symbolic)
  are the evidence pool; goals are the rubric. Without explicit
  rubric rows the grader is grading against a moving prompt.
- **Evolution loop needs failure to learn.** Roadmap row 4-4B
  (`phase4-roadmap.md:301`) makes the goal table the trigger surface
  for `skill_update` proposals. A weekly score < 5 emits an
  `evolution_signals` row with `event_kind = "goal.weekly_failed"`;
  the engine clusters those into `skill_update` candidates exactly
  the way it clusters `tool_failure` today
  (`corlinman-evolution-engine/engine.py:79-89`).

## Goal hierarchy — three tiers

Tiers are **wall-clock windows**, not free-form labels. Window math
is deterministic — two runs on the same evidence produce the same
score.

| Tier | Window | `target_date` | Reflection cron |
|---|---|---|---|
| `short` ("today") | rolling 24h ending at run start | run-day midnight UTC | `5 0 * * *` UTC |
| `mid` ("this week") | Mon 00:00 → Sun 23:59 UTC (ISO week) | following Monday midnight | `10 0 * * 1` UTC |
| `long` ("this quarter") | 90 calendar days from `created_at` | `created_at + 90d` | `15 0 1 1,4,7,10 *` UTC |

Mid clamps to ISO week, not "rolling 7d", so two operators looking
at "this week's score" mean the same thing. Long uses 90d-from-
creation, not calendar quarters — operators set quarterlies on
arbitrary days, so "Q2 progress" on a goal created May 15 would
mislead.

## Schema — `agent_goals.sqlite` per-tenant

Lives at `<data_dir>/tenants/<t>/agent_goals.sqlite`, alongside
`episodes.sqlite` from D1, `agent_state.sqlite`,
`evolution.sqlite`. Following the convention in
`phase4-roadmap.md:341` (per-tenant DB list).

```sql
CREATE TABLE IF NOT EXISTS goals (
    id              TEXT PRIMARY KEY,            -- 'goal-<yyyymmdd>-<slug>'
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    agent_id        TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('short','mid','long')),
    body            TEXT NOT NULL,                -- one-sentence goal statement
    created_at      INTEGER NOT NULL,             -- unix ms
    target_date     INTEGER NOT NULL,             -- unix ms; tier-derived
    parent_goal_id  TEXT REFERENCES goals(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','completed','expired','archived')),
    source          TEXT NOT NULL                 -- see "Goal sources"
                    CHECK (source IN ('operator_cli','operator_ui','agent_self','seed'))
);
CREATE INDEX IF NOT EXISTS idx_goals_tenant_agent_tier_status
    ON goals(tenant_id, agent_id, tier, status);
CREATE INDEX IF NOT EXISTS idx_goals_parent
    ON goals(parent_goal_id) WHERE parent_goal_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS goal_evaluations (
    goal_id              TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    evaluated_at         INTEGER NOT NULL,
    score_0_to_10        INTEGER NOT NULL CHECK (score_0_to_10 BETWEEN 0 AND 10),
    narrative            TEXT NOT NULL,           -- LLM-authored, redacted
    evidence_episode_ids TEXT NOT NULL,           -- JSON array of episode ids
    reflection_run_id    TEXT NOT NULL,           -- idempotency key
    PRIMARY KEY (goal_id, evaluated_at)
);
CREATE INDEX IF NOT EXISTS idx_goal_eval_recent
    ON goal_evaluations(goal_id, evaluated_at DESC);
```

The `tenant_id` default + composite-key shape mirrors the persona
migration (`corlinman-persona/store.py:24-40`).
`evidence_episode_ids` is JSON, not a join table — one evaluation
cites ≤ 8 ids and we want the row self-contained for replay (sibling
to W2-D trajectory replay).

## Goal sources

`goals.source` tracks provenance for two reasons: audit (who
authored a goal) and authority (who's allowed to edit it).

| Source | Authored by | Edit/archive permission |
|---|---|---|
| `operator_cli` | `corlinman goals set` | operator only |
| `operator_ui` | future admin UI (out of scope D2) | operator only |
| `agent_self` | `skill_update`-shaped evolution proposal whose target is `goal:<id>` | operator-approved through evolution queue |
| `seed` | `corlinman-goals seed` from a YAML default (parallels `corlinman-persona/seeder.py:1-149`) | operator only |

**Agent-self-set goals are rare and gated**: an agent never writes a
goal directly. It files an evolution proposal whose target is
`goal:<id>` (a dedicated `goal_set` kind is D3+ scope; D2 ships only
the read/write primitive). The `source = agent_self` value exists in
the schema today so D3 doesn't need a migration.

## `{{goals.*}}` placeholder semantics

Substitution happens in the Rust gateway's `PlaceholderEngine`
(`rust/crates/corlinman-core/src/placeholder.rs:126-155`), the same
engine `{{persona.*}}` and `{{user.*}}` register against. Python
exposes `GoalsResolver` (parallels
`corlinman_persona/placeholders.py:77`); the gateway binds it via
the gRPC bridge at
`corlinman-agent/src/corlinman_agent/placeholder_client.py:32-99`
(error contract: `cycle:` / `depth_exceeded` / `resolver:`).

Four canonical keys; anything else under `goals.<custom>` resolves
to `""` (typo-tolerant, same posture as
`corlinman_persona/placeholders.py:117-121`).

| Key | Resolves to |
|---|---|
| `{{goals.today}}` | bullet list of `tier='short' AND status='active' AND target_date >= now()`, ordered by `created_at`. Bare bullets, no scores (the day isn't graded yet). Empty string if none. |
| `{{goals.weekly}}` | mid-tier active goals **plus** the previous week's `goal_evaluations` summary lines for that tier (`- <body>: score 7 — <one-line narrative>`). Bounded to 8 lines total. |
| `{{goals.quarterly}}` | long-tier active goals + the last 12 weekly mid-tier scores rolled up (`avg`, `min`, `count_failing`). |
| `{{goals.failing}}` | every active goal whose **most recent** `goal_evaluations.score_0_to_10 < 5`, regardless of tier. Drives self-correction prompts; bounded to 5. |

Resolver returns plain strings, never structured payloads — the
prompt is the contract. Empty string on missing `agent_id` (same
rationale as `corlinman_persona/placeholders.py:22-24`).

## Reflection job

Lives in `corlinman-goals/reflection.py`. Wires through the existing
gateway scheduler (`corlinman-scheduler`,
`rust/crates/corlinman-scheduler/src/jobs.rs:14-62`,
`config.rs:929-970` `JobAction::Subprocess`) the same way the W3-A
consolidation job does
(`corlinman-evolution-engine/consolidation.py:1-80`). No new
runtime; the scheduler knows how to run a Python subprocess on cron.

```toml
[[scheduler.jobs]]
name = "goals-reflect-short"
cron = "5 0 * * *"
action = { type = "subprocess",
           command = "corlinman-goals",
           args = ["reflect-once", "--tier", "short"] }
```

### Reflection contract

Inputs (per (tenant, agent) pair):

1. Active goals at the requested tier.
2. Episodes from D1 in the tier's window (`{{episodes.last_24h}}` /
   `last_week` / `last_quarter` semantics; D1 design owns the query).
3. Provider alias from `[goals.reflection_llm_alias]`
   (default: same alias the persona seeder uses; one cheap call per
   goal, not per agent).

Per goal, one structured-output LLM call:

```
Evaluate progress on goal: <goal.body>
Window: <window_start>..<window_end>.
Episodes: <numbered list of bodies + ids>
Return: { score_0_to_10: int, narrative: str <= 280 chars,
          cited_episode_ids: [str] subset of provided ids }
```

Narrative passes through the existing PII redactor (Phase 3.1 S-1,
`phase4-roadmap.md:148-152`) before write. `cited_episode_ids` is
intersected with the input id set on write — model hallucinations
get dropped, not stored as dangling references.

### Idempotency, retries, partial-window

- **Idempotency**: `reflection_run_id = "<tier>-<window_start_ms>"`;
  `INSERT OR IGNORE` against `(goal_id, evaluated_at)` — rerun after
  a crash never double-counts.
- **Retry**: per-goal failure (timeout, parse) logged + counted in
  `goals_reflection_total{outcome="error"}`; run continues, goal
  un-scored for that window, next window picks up.
- **Partial windows**: goal created Wednesday evaluated Sunday gets
  window `(created_at, Sunday-end)`, not `(Mon, Sun)` — fair sample
  of available evidence.
- **No episodes fallback**: zero episodes in window → skip LLM,
  write `{ score: 0, narrative: "no_evidence", cited_episode_ids:
  [] }`. Sentinel so `{{goals.failing}}` excludes it ("no activity"
  ≠ "actively failing").

## Goal-setting CLI surface

One binary, six subcommands; mirrors `corlinman-persona`
(`corlinman-persona/cli.py:32-80`). All emit JSON on `--json`.

```text
corlinman goals set    --agent-id A --tier short --body "..."
                       [--parent-goal-id G] [--target-date 2026-05-09]
corlinman goals list   --agent-id A [--tier ...] [--status ...]
                       [--include-evaluations]
corlinman goals edit   --goal-id G [--body|--target-date|--parent-goal-id|--status NEW]
corlinman goals archive --goal-id G [--cascade]
corlinman goals reflect-once --tier short|mid|long [--dry-run]
corlinman goals seed   --yaml path.yaml [--agent-id A]
```

`set` rejects non-existent `parent-goal-id`, cross-tenant parent, or
parent of equal/lower tier (a short cannot parent a mid).
`target-date` defaults from `tier` so the operator never calculates
ISO weeks. `archive --cascade` walks one level of children.

## Cascading

`parent_goal_id` makes the table a forest: long ← mid ← short.
Single-level descent per row, transitive across the tree.

`evaluator.py` aggregates upward at read time, not write time:

- A `mid` goal's display score is `max(direct_score, avg(child
  short scores in window))`. Max not weighted-avg — operators want
  optimistic surfacing; a strong week is a strong week even if
  Tuesday flopped.
- A `long` goal surfaces *two* numbers: most recent direct score
  AND trailing-4-week average of `mid` children. A single number
  hides the trend.
- `{{goals.failing}}` queries the **stored** `score_0_to_10` only,
  never the aggregate — the audit row is the source of truth.

Computed in Python at placeholder-resolve time (one query per tier,
≤ 50 rows). No materialised view.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `goals_schema_round_trips` | store | insert/select preserves all columns; CHECK rejects bad tier/status/source |
| `placeholder_today_renders_active_short` | resolver | only short/active/in-window rows; bullet format; empty → "" |
| `placeholder_weekly_includes_last_week_scores` | resolver | mid bodies + previous-week `goal_evaluations` lines, bounded to 8 |
| `placeholder_quarterly_aggregates_weekly_scores` | resolver | trailing-12-weeks roll-up shape (avg/min/count_failing) |
| `placeholder_failing_filters_by_recent_score_under_5` | resolver | only goals whose latest evaluation is < 5; sentinel "no_evidence" excluded |
| `placeholder_unknown_subkey_returns_empty` | resolver | `{{goals.bogus}}` → "" not error |
| `reflection_idempotent_within_window` | reflection | rerun with same `reflection_run_id` is a no-op (`INSERT OR IGNORE`) |
| `reflection_drops_hallucinated_episode_ids` | reflection | model returns id not in input → filtered before write |
| `reflection_no_evidence_writes_sentinel` | reflection | empty episode set → score 0 + narrative "no_evidence", no LLM call |
| `reflection_partial_window_for_new_goal` | reflection | goal created mid-window scored against (created_at, window_end) |
| `cascade_short_to_mid_aggregates_max` | evaluator | mid goal's display score = max(direct, avg(child shorts)) |
| `cascade_archive_walks_one_level` | cli | `archive --cascade` archives direct children, not grandchildren (single-level explicit) |
| `multi_tenant_isolation_no_cross_read` | store | tenant A list never returns tenant B rows; resolver same |
| `cli_set_rejects_cross_tier_parent` | cli | `set --tier mid --parent-goal-id <a-short-id>` → exit 2, error `cross_tier_parent` |
| `goal_failure_emits_evolution_signal` | reflection | weekly score < 5 → row written to `evolution_signals` with `event_kind = "goal.weekly_failed"` |

## Config knobs

```toml
[goals]
enabled = true
reflection_llm_alias = "default-cheap"   # provider alias from [providers.*]
short_window_hours = 24
mid_window_days = 7
long_window_days = 90

[goals.reflection]
narrative_max_chars = 280
evidence_max_episodes = 8
no_evidence_sentinel = "no_evidence"

[[scheduler.jobs]]
name = "goals-reflect-short"
cron = "5 0 * * *"
action = { type = "subprocess", command = "corlinman-goals",
           args = ["reflect-once", "--tier", "short"], timeout_secs = 600 }

[[scheduler.jobs]]
name = "goals-reflect-mid"
cron = "10 0 * * 1"
action = { type = "subprocess", command = "corlinman-goals",
           args = ["reflect-once", "--tier", "mid"], timeout_secs = 1200 }

[[scheduler.jobs]]
name = "goals-reflect-long"
cron = "15 0 1 1,4,7,10 *"
action = { type = "subprocess", command = "corlinman-goals",
           args = ["reflect-once", "--tier", "long"], timeout_secs = 1800 }
```

Window knobs are present so a small-deployment operator can shrink
to "today/yesterday/this month" without code changes; defaults match
the roadmap.

## Open questions

1. **Who edits an `agent_self` goal?** Today the table allows
   operator edits unconditionally. Lean: **lock `body` on agent-self
   goals** when D3 lands the `goal_set` evolution kind; D2 ships
   permissive editing because all D2 goals are operator-authored.
2. **Cascading constraint enforcement.** Forbid orphaning a child
   whose parent is archived? `parent_goal_id` is `ON DELETE SET
   NULL`. Lean: **set null + log** — hard FK forces chasing children
   before archive; bad cleanup friction.
3. **Completion vs expiration.** Short goal at day end with last
   score ≥ 8: auto-transition `active → completed`, or require
   explicit close? Lean: **auto `expired` on `target_date < now`;
   `completed` is operator-only**. Auto-completion rewards score
   gaming; explicit expiry keeps the audit honest.
4. **`{{goals.weekly}}` line cap.** 8 is a guess; 12 mid-tier goals
   would silently truncate. Lean: **emit `… (+N more)`** trailing
   line so the prompt knows truncation happened.

## Implementation order

Each numbered item is one bounded iteration (~30 min - 2 hours):

1. **`corlinman-goals` skeleton + schema** — `pyproject.toml`
   mirroring `corlinman-persona/pyproject.toml`; `store.py` with
   `SCHEMA_SQL` + `_table_exists` / `_column_exists` helpers copied
   from `corlinman-persona/store.py:55-77`; `Goal` /
   `GoalEvaluation` dataclasses. Tests:
   `goals_schema_round_trips`, `multi_tenant_isolation_no_cross_read`.
2. **CRUD + tier-derived `target_date`** — `add_goal` /
   `list_goals` / `update_goal` / `archive_goal` (with `cascade`).
   Tier→default-date math (ISO week for `mid`, +90d for `long`).
   Tests: round-trip per tier, default-date calculation.
3. **`GoalsResolver` + four placeholder keys** — bullet formatting,
   line caps, empty-string fallback. Mirrors
   `corlinman_persona/placeholders.py:77-121`. Tests: all five
   `placeholder_*` cases.
4. **CLI** — `set` / `list` / `edit` / `archive` / `seed` (YAML)
   subcommands; argparse mirrors `corlinman-persona/cli.py:32-80`;
   `--json` output. Tests: `cli_set_rejects_cross_tier_parent`,
   archive cascade, seed idempotency.
5. **Reflection job — D1-less stub** — `reflect-once` subcommand
   that fetches goals, **stubs** episode lookup behind a protocol
   (`EpisodeQuery`), calls a stub `LLMGrader`, writes
   `goal_evaluations`. Tests: `reflection_idempotent_within_window`,
   `reflection_partial_window_for_new_goal`,
   `reflection_no_evidence_writes_sentinel`.
6. **Reflection job — wire D1 episode query** — replace the stub
   `EpisodeQuery` with the D1 store import (`corlinman_episodes.store`).
   Tests: end-to-end with a fake episodes DB seeded by the test.
7. **Reflection job — wire LLM provider** — `reflection_llm_alias`
   config; one cheap call per goal; structured-output schema; PII
   redactor pass; hallucinated-id filter. Tests:
   `reflection_drops_hallucinated_episode_ids`, redaction smoke.
8. **Cascade evaluator + `{{goals.weekly}}` aggregation** — `evaluator.py`;
   `max(direct, avg(children))` for mid; long's two-number split.
   Tests: `cascade_short_to_mid_aggregates_max`, quarterly roll-up.
9. **Evolution signal emission on failure** — when reflection writes
   a mid-tier score < 5, also `INSERT INTO evolution_signals` with
   `event_kind = "goal.weekly_failed"`,
   `target = "goal:<id>"`. Tests:
   `goal_failure_emits_evolution_signal`.
10. **E2E acceptance from `phase4-roadmap.md:305-310`** — fresh
    tenant, seed 4 mid-tier goals, simulate 7 days of episodes (D1
    fixture), run `corlinman-goals reflect-once --tier mid`, assert
    `{{goals.weekly}}` rendered through the placeholder bridge
    returns a 4-item list with scores. Single integration test;
    `TempDir` + scheduler-less direct invocation.

## Out of scope (D2)

- **Admin UI** — goal-setting page, evaluation history, failing-
  dashboard. CLI is the only operator surface in D2; admin REST +
  UI follow in a Wave-4 follow-up shaped like
  `phase4-w2-b3-design.md`.
- **Auto-goal-setting from external systems** — Notion/Jira/time-
  tracker sync. The `source` enum has room (`operator_ui` / future
  `operator_sync`); D2 ships only CLI + seed.
- **Cross-agent goal-sharing** — federation across tenants.
  Parallels `phase4-w2-b3-design.md` skill federation; Phase 5.
- **`goal_set` evolution kind** — agent-authored goals through the
  evolution queue; D3 / Phase 5. D2 ships only the storage shape
  (`source = 'agent_self'`) so D3 doesn't migrate.
- **Goal-driven curriculum** — agent picks which skill to deepen
  from `{{goals.failing}}`. Listed under "Bonus / Stretch"
  (`phase4-roadmap.md:317`).
