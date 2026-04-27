# Migrating from v0.2.x to v0.3.x

For engineers who deployed **v0.2.x** and want to upgrade to **v0.3.x**.

## 1. Summary

v0.3 ships **Phase 3 Wave 1-A — ShadowTester**: medium/high-risk proposals
are now run through an in-process eval set before they reach the operator
queue, with the measured deltas attached to each proposal row.

The visible deltas:

- Two new columns on `evolution_proposals` (`eval_run_id`,
  `baseline_metrics_json`).
- One new config section (`[evolution.shadow]`) — disabled by default, so
  rollout is a no-op until the operator opts in.
- A new in-process scheduler job (`shadow_tester`) — only spawned when the
  config section is `enabled = true`.

Backwards-compatible on every contract: existing `evolution.sqlite` files
gain the two columns automatically on first open (idempotent ALTER inside
`EvolutionStore::open`); existing `config.toml` files load unchanged.

## 2. Backend changes

### 2.1 Evolution schema v0.2 → v0.3

Four new nullable columns on `evolution_proposals` (W1-A adds the first
two; W1-B adds the second two — both ship together as v0.3):

| Column                  | Type    | Wave | Purpose |
|-------------------------|---------|------|---------|
| `eval_run_id`           | TEXT    | W1-A | Pointer to the shadow run that populated `shadow_metrics`. Lets the operator (and AutoRollback) trace metrics back to the eval set version they were measured against. |
| `baseline_metrics_json` | TEXT    | W1-A | Pre-change baseline captured at shadow time, so the operator review surface can render a delta (`shadow_metrics − baseline_metrics_json`) instead of just the post-change snapshot. |
| `auto_rollback_at`      | INTEGER | W1-B | Unix-ms timestamp set when the AutoRollback monitor decides to revert this proposal. `NULL` for proposals that were never auto-reverted (the common case). |
| `auto_rollback_reason`  | TEXT    | W1-B | Human-readable string the monitor wrote describing which threshold breached (e.g. `"err_signal_count: 4 → 23 (+475%); threshold +50%"`). Read by the operator when triaging a rollback. |

All four are nullable: `pending` proposals (low-risk, or filed before
ShadowTester ran) leave the W1-A columns `NULL`; `applied` proposals
that never tripped the monitor leave the W1-B columns `NULL`. Existing
decoders treat `NULL` as "no shadow / no rollback" and keep working.

**Migration is automatic.** The `evolution-evolution` crate's
`EvolutionStore::open` walks `schema::MIGRATIONS` after applying
`SCHEMA_SQL` and runs each ALTER only when the target column is missing
(pragma-checked). Fresh DBs get the columns from `CREATE TABLE` and skip
the ALTER; v0.2 DBs get the ALTER and converge.

If you want to apply the migration by hand (e.g. for an offline copy):

```sql
ALTER TABLE evolution_proposals ADD COLUMN eval_run_id           TEXT;
ALTER TABLE evolution_proposals ADD COLUMN baseline_metrics_json TEXT;
ALTER TABLE evolution_proposals ADD COLUMN auto_rollback_at      INTEGER;
ALTER TABLE evolution_proposals ADD COLUMN auto_rollback_reason  TEXT;
```

SQLite ALTER TABLE ADD COLUMN is online and lock-free for nullable text
columns, so this is safe to run on a live DB if the gateway is paused.

### 2.2 Config additions

Two new sections. Existing `config.toml` loads unchanged because both
are `#[serde(default)]`.

```toml
[evolution.shadow]
enabled        = false                  # opt-in master switch
eval_set_dir   = "/data/eval/evolution" # root with per-kind subdirs
sandbox_kind   = "in_process"           # only valid value in v0.3

[evolution.auto_rollback]
enabled            = false              # opt-in master switch
grace_window_hours = 72                 # how long after apply a row stays eligible

[evolution.auto_rollback.thresholds]
default_err_rate_delta_pct    = 50.0    # +50% over baseline error count → revert
default_p95_latency_delta_pct = 25.0    # +25% over baseline p95 latency → revert
signal_window_secs            = 1800    # symmetric pre/post-apply sliding window
min_baseline_signals          = 5       # quiet targets need ≥ 5 baseline signals
```

`sandbox_kind = "docker"` is reserved for Phase 4 and rejected on load.
`[evolution.auto_rollback].enabled = false` ships off so applies don't
surprise-revert before the monitor is fully wired; `metrics_baseline`
is still captured at apply time so flipping the switch on later
doesn't lose the audit data.

## 3. Filesystem additions

```
/data/eval/evolution/
└── memory_op/
    ├── case-001-near-duplicate-merge.yaml
    ├── case-002-content-hash-mismatch.yaml
    └── case-003-no-op-on-distinct.yaml
```

Each YAML case carries (a) a fixture seeding script for `kb.sqlite`, (b)
the proposal under test, (c) the expected metrics shape. The
`corlinman-shadow-tester` crate ships starter cases under
`tests/fixtures/eval/`; copy them to `eval_set_dir` before flipping
`enabled = true`. Authoring rules live in
[`docs/guides/eval-sets.md`](../guides/eval-sets.md) (W1-A pending).

## 4. Behavioral changes when `[evolution.shadow].enabled = true`

The proposal lifecycle gains two intermediate states for medium/high-risk
kinds:

```
pending  →  shadow_running  →  shadow_done  →  approved  →  applied
   ▲                                  ▲
   │                                  └── operator sees metrics delta;
   │                                      decides on the measured outcome
   │
   └── low-risk kinds (memory_op today) stay on the original
       pending → approved path; ShadowTester does not touch them
```

State transitions for medium/high-risk:

1. Engine writes `pending` (unchanged from v0.2).
2. Scheduler fires the `shadow_tester` job (default 30 min after
   `evolution_engine`). It claims `pending` rows with
   `risk in ('medium','high')`, transitions them to `shadow_running`.
3. Runner loads matching eval cases from `eval_set_dir`, executes against
   a tempdir copy of `kb.sqlite` (production DB is never written), and
   collects per-kind metrics.
4. Runner writes `shadow_metrics`, `baseline_metrics_json`, `eval_run_id`,
   transitions to `shadow_done`. Admin API surfaces the row with a delta
   visualization.

Low-risk proposals (Phase 2's `memory_op` is the only kind in v0.3) skip
ShadowTester entirely and remain immediately approvable from `pending`.

## 5. Step-by-step upgrade

```bash
# 1. Back up the evolution + kb DBs (the rest of /data is unaffected).
cp /data/evolution.sqlite /data/evolution.sqlite.backup-v2
cp /data/kb.sqlite        /data/kb.sqlite.backup-v2

# 2. Update binaries.
git pull
cargo build --release -p corlinman-gateway -p corlinman-cli -p corlinman-shadow-tester

# 3. First run — the schema migration applies automatically on
#    EvolutionStore::open. Existing v0.1 config.toml loads unchanged.
./target/release/corlinman dev
```

Smoke checks after the first run:

- `sqlite3 /data/evolution.sqlite ".schema evolution_proposals"` shows the
  two new columns.
- `corlinman config validate` is green.
- The admin UI's `/evolution` page renders unchanged (ShadowTester is
  disabled by default; no behavior shift yet).

To opt in:

```toml
[evolution.shadow]
enabled = true
```

…then restart the gateway and verify the scheduler logs the
`shadow_tester` job registration on boot.

## 6. Rollback

Schema columns are nullable and the migration is additive — there is no
breaking down-path. To revert to v0.2 behavior without touching the DB:

```toml
[evolution.shadow]
enabled = false
```

If the binaries themselves need to be rolled back, swap them and the
existing v0.2 readers will simply ignore the two new columns (they're
not in the v0.2 INSERT/SELECT lists).

A full restore from backup is also valid:

```bash
systemctl stop corlinman              # or: docker compose down
mv /data/evolution.sqlite             /data/evolution.sqlite.failed-v3
mv /data/evolution.sqlite.backup-v2   /data/evolution.sqlite
./target/release-v0.2.x/corlinman dev
```

## 7. Troubleshooting

1. **`apply migration evolution_proposals.eval_run_id: …`** — the
   pragma-check or ALTER failed (typically because the file is on a
   read-only mount or another writer holds an exclusive lock). The error
   surfaces from `EvolutionStore::open`; restart with the gateway
   stopped.
2. **`unknown variant 'docker' for ShadowSandboxKind`** — config sets
   `sandbox_kind = "docker"`, which is a Phase 4 reservation. Use
   `"in_process"` until the docker sandbox lands.
3. **`shadow_tester` job not in scheduler logs.** Either
   `[evolution.shadow].enabled = false` (default) or the scheduler
   itself is disabled. Confirm via `corlinman config show
   evolution.shadow` and `corlinman config show scheduler`.
