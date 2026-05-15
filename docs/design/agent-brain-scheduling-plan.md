# Agent Brain — Scheduling-Integration Plan

Status: draft
Branch: `feat/agent-brain-scheduling`
Follow-up to: PR #4 (`feat(agent-brain): add memory curator package`)
Author note (PR #4 description): *"Full CLI/gateway scheduling integration can follow separately."*

## 1. Why this is a separate piece of work

PR #4 shipped the curator **primitives** — `session_reader`, `extractor`,
`risk_classifier`, `link_planner`, `vault_writer`, `index_sync`,
`serialization`, `models`, `config` — plus unit tests. What it did **not**
ship:

1. A `corlinman_agent_brain.cli` module — yet `pyproject.toml` already
   declares `corlinman-agent-brain = "corlinman_agent_brain.cli:main"`.
   The wheel installs a console script that today imports a missing
   module. **This is a latent install-time bug.**
2. A `runner.py` that composes the primitives end-to-end
   (`bundle → candidates → risk → links → nodes → vault → index`).
3. Run-state tracking (the design's "stage 10/11" — idempotency, retry,
   per-session "already curated?" check).
4. A scheduler entry — there is no `[[scheduler.jobs]]` block for the
   curator in `docs/config.example.toml`.
5. Provider-factory glue — the extractor wants an `ExtractionProvider`
   (LLM); `index_sync` wants an HTTP client; nothing wires those to
   `corlinman-providers` / the gateway's MemoryHost.

Each of these has at least one open design choice (see §6), which is
why we are publishing this plan instead of jumping into code.

## 2. How "scheduling" works in this repo (context)

The scheduler is **rust-side**. `rust/crates/corlinman-scheduler` parses
`[[scheduler.jobs]]` from `corlinman-core::config`, spawns one tokio task
per job, sleeps until the next cron tick (7-field `cron` crate grammar),
and dispatches by `JobAction`. Today only `Subprocess` is end-to-end.
`RunAgent` / `RunTool` warn-and-emit `EngineRunFailed` so the failure is
observable.

Every existing background python job follows the **same shape**:

| Job | Cron | Subprocess command (from `docs/config.example.toml`) |
|-----|------|------------------------------------------------------|
| `evolution_engine`   | `0 0 3 * * * *`  | `corlinman-evolution-engine run-once` |
| `shadow_tester`      | `0 30 3 * * * *` | `corlinman-shadow-tester run-once --config …` |
| `auto_rollback`      | `0 0 4 * * * *`  | `corlinman-auto-rollback run-once …` |
| `persona_decay`      | `0 0 * * * * *`  | `corlinman-persona decay-once --db …` |
| `memory_consolidation` | `0 0 5 * * * *` | `corlinman-evolution-engine consolidate-once …` |
| `user_model_distill` | `0 30 5 * * * *` | `corlinman-user-model distill-recent --since-hours 24` |

The reference implementation to copy is `corlinman-episodes`:
`runner.py` (orchestration) + `cli.py` (subcommands `distill-once`,
`embed-pending`, `archive-sweep`, `rehydrate-all`) + a registered
**provider factory** (`register_summary_provider_factory`) so the
gateway can plug in the real LLM at boot without adding an import cycle.
The gateway never imports the curator directly — it only spawns a
subprocess via `JobAction::Subprocess`.

There is **no in-process "session-end hook"** today. Episodes/persona/
user-model all run on cron sweeping windows of recently-completed
sessions. We will adopt the same model — the design plan §10
explicitly calls this out ("不在聊天实时链路中阻塞").

## 3. Scope of this follow-up

**In scope** (one branch, one PR):

1. `python/packages/corlinman-agent-brain/src/corlinman_agent_brain/runner.py`
   — `curator_run_once(...)` orchestrating the whole pipeline.
2. `…/cli.py` — argparse with `curate-once`, `curate-session`,
   `rebuild-index` subcommands; `--stub-llm`, `--stub-retrieval`,
   `--dry-run`, `--json` flags so the scheduler smoke run works
   without LLM/HTTP.
3. `…/runs_store.py` — tiny SQLite store (`curator_runs` table:
   `run_id, tenant_id, session_id, status, started_at_ms, finished_at_ms`).
   Used for idempotency (skip already-curated session_ids in the last N
   days) and for the future `rebuild-index` / `rollback` operations.
4. `…/config_loader.py` — `[agent_brain]` section in workspace TOML,
   mirrors `corlinman_episodes._load_episodes_config`.
5. Provider-factory registry in `cli.py` (mirrors
   `register_summary_provider_factory` / `register_embedding_provider_factory`
   in `corlinman-episodes/cli.py`).
6. New `[[scheduler.jobs]]` block in `docs/config.example.toml`
   (cadence: hourly, 15 min after `user_model_distill` so episodes are
   fresh — `0 45 * * * * *`).
7. Tests:
   - `test_runner.py` — happy path, skip-discarded, idempotent re-run,
     dry-run, semi-auto vs draft-first policy.
   - `test_cli.py` — argparse + factory registration + exit codes
     (mirrors `corlinman-episodes/tests/test_cli.py`).
   - `test_runs_store.py` — schema + dedup query.

**Out of scope** (deliberately, justify-later):

1. Real LLM provider wiring — gateway boot will register the factory
   in a follow-up; we ship `--stub-llm` so the scheduler subprocess
   smoke-runs green from day one.
2. Real `IndexSyncClient` HTTP transport — already complete in
   `index_sync.py`; runner just constructs it from `[memory_host]` URL.
3. Admin HTTP route on the gateway (the design plan's §11 audit/review
   surface). Phase 2.
4. The `approve` / `reject` / `review` CLI subcommands. They need the
   draft-review queue persistence, which the design plan §11 splits
   out as its own milestone (M5).
5. Any rust-side change. The `Subprocess` action is already enough.

## 4. File-level work breakdown

| File | LOC est. | Notes |
|------|----------|-------|
| `runner.py` | ~250 | `curator_run_once`, `_candidate_to_node`, `_apply_link_plan`, dedup against `runs_store`. |
| `cli.py` | ~200 | Mirrors `corlinman-episodes/cli.py` shape. Subcommands + factory registry + summary printers. |
| `runs_store.py` | ~120 | aiosqlite (already a dep), one table, three queries. |
| `config_loader.py` | ~60 | tomllib → `CuratorConfig` overrides. |
| `__init__.py` | +5 | Re-export `curator_run_once`, `extract_candidates`, `read_session_by_id`, `IndexSyncClient` (currently missing from public surface). |
| `tests/test_runner.py` | ~250 | |
| `tests/test_cli.py` | ~150 | |
| `tests/test_runs_store.py` | ~80 | |
| `docs/config.example.toml` | +12 | One `[[scheduler.jobs]]` block + comment. |
| `docs/design/agent-brain-scheduling-plan.md` | (this file) | |

Total: ~**1100 LOC** including tests. ~600 LOC if you exclude tests.
This sits above the "small enough to just do it" threshold, which is
why this is a plan and not a commit of the implementation.

## 5. The core unanswered question that drives 80% of the design — `MemoryCandidate → KnowledgeNode`

`MemoryCandidate` carries: `topic, kind, summary, evidence, confidence,
risk, tags`. `KnowledgeNode` requires additionally: `node_id, title,
path, key_facts, decisions, evidence_sources, related_nodes,
frontmatter.scope, frontmatter.status, frontmatter.created_at,
frontmatter.updated_at, frontmatter.links`.

The naive mapping (`title = topic`, `key_facts = evidence`,
`decisions = []`, `scope = AGENT if agent_id else GLOBAL`) loses
fidelity. The design plan §7 shows a richer body shape (摘要 / 关键事实 /
决策 / 偏好 / 状态 / 证据来源 / 相关节点) that the extractor does **not**
currently fill. Two options:

- **A. Naive mapping now** — runner does a one-line conversion,
  `key_facts == evidence` (likely just quotes). Ship it; iterate later.
  Trade-off: vault Markdown will read like quote dumps until the
  extractor learns to emit structured key_facts/decisions per kind.
- **B. Extend the extractor** — change `SYSTEM_PROMPT` to ask the LLM
  for `key_facts: [...]` and `decisions: [...]` per candidate kind,
  add fields to `MemoryCandidate`, plumb through. Cleaner output,
  larger blast radius (touches PR #4's already-tested extractor +
  models). Will also bump test fixtures.

**Recommendation**: ship **A** in this branch (note in
`runner.py`'s docstring), file a follow-up issue for **B** so the
prompt change ships with its own LLM-output-quality eval.

## 6. Open design questions (need user/maintainer answer before code)

1. **Cadence**. Hourly seems right (matches `persona_decay`) but
   episodes runs daily. The curator depends on episodes being
   distilled, so its cron should be downstream of
   `user_model_distill`. Proposal: `0 45 * * * * *` (every hour at
   :45). **Need confirmation.**
2. **Multi-tenant routing**. The existing scheduler jobs run with a
   single tenant via env var (`CORLINMAN_DATA_DIR=/data`) and the CLI
   reads `--tenant default`. Curator inherits the same single-tenant
   assumption. Multi-tenant fan-out is a separate problem the whole
   scheduler subsystem hasn't solved yet. **Defer to whoever does
   multi-tenant scheduler routing.**
3. **Failure semantics**. Today subprocess exit ≠ 0 → `EngineRunFailed`
   on the hook bus. Curator should follow the same convention. The
   `runs_store` row should be marked `failed` so the next sweep can
   retry. **Confirm: retry forever, or N attempts then give up?**
   Episodes uses `run_stale_after_secs` to reclaim hung runs — same
   knob recommended.
4. **Where does `vault_root` live?** Design plan §6 suggests
   `knowledge/agent-brain/`. Not in `[storage]` today. Proposal: put
   it under the tenant data dir as `<data_dir>/agent-brain/`, override
   via `[agent_brain] vault_root = "..."` in TOML. **Need
   confirmation it should not live in `corlinman-server`'s sessions
   tree.**
5. **MemoryHost namespace**. `index_sync.py` defaults to namespace
   `"agent-brain"`. The wider memory-host's RAG layer needs to learn
   to query that namespace at retrieval time. **Confirm whether
   `corlinman-memory-host` already routes by namespace, or if a
   companion change is needed.** (Quick grep suggests it does — but
   verify before relying on it.)
6. **Where does the gateway register `register_extraction_provider_factory`?**
   `corlinman-episodes` is registered from the gateway boot via
   `corlinman_server.main`. We should add a parallel registration in
   the same boot — but only after the runner/CLI surface lands. **One-
   PR-at-a-time:** ship runner+CLI+stub here, file a follow-up to
   wire the real factory.

## 7. Acceptance criteria for the implementation PR

1. `uv run corlinman-agent-brain --help` succeeds (no missing module).
2. `uv run corlinman-agent-brain curate-once --sessions-db <path> --episodes-db <path> --vault-root <tmp> --runs-db <tmp>/runs.sqlite --tenant default --stub-llm '[]' --json` exits 0 on an empty DB, exits 0 on a populated DB, and the second invocation prints `status: skipped_already_curated` for the same `session_id` (idempotency).
3. `uv run pytest python/packages/corlinman-agent-brain/` is green
   (PR #4 tests stay green; new tests cover runner + CLI + runs_store).
4. The new `[[scheduler.jobs]]` block in `docs/config.example.toml`
   parses successfully with `corlinman doctor` (rust-side validator).
5. No rust changes. No `corlinman-vector` changes. No `Cargo.lock` /
   `deny.toml` changes.

## 8. Pointer cheat-sheet for whoever picks this up

| Concept | File |
|--------|------|
| Reference runner shape | `python/packages/corlinman-episodes/src/corlinman_episodes/runner.py` |
| Reference CLI shape | `python/packages/corlinman-episodes/src/corlinman_episodes/cli.py` |
| Provider factory pattern | `corlinman_episodes.cli.register_summary_provider_factory` |
| Scheduler job dispatch | `rust/crates/corlinman-scheduler/src/{runtime,subprocess,jobs}.rs` |
| Job config schema | `rust/crates/corlinman-core/src/config.rs:942-993` |
| Existing job declarations | `docs/config.example.toml:289-361` |
| Curator primitives surface | `python/packages/corlinman-agent-brain/src/corlinman_agent_brain/__init__.py` (note: missing `extract_candidates`, `read_session_by_id`, `IndexSyncClient` — add when wiring runner) |
| Design — stage 9 (CLI) | `docs/design/agent-brain-memory-curator-task-plan.md:525-552` |
| Design — stage 10 (scheduling) | `docs/design/agent-brain-memory-curator-task-plan.md:554-572` |
| Design — stage 11 (audit/rollback) | `docs/design/agent-brain-memory-curator-task-plan.md:574-594` |
