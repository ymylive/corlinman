# Phase 4 — Next Tasks

**Status**: Active · **Owner**: TBD · **Last revised**: 2026-05-08

> Operational task list following Phase 4 Wave 1's closure
> (PR [#1](https://github.com/ymylive/corlinman/pull/1), 16 commits).
> Companion to `phase4-roadmap.md` — the roadmap is the strategic
> picture, this doc is the tactical pick-up list.

## Progress snapshot (2026-05-08)

**Section A (Wave 1 cleanup)**: ✅ A1-A7 all complete (commits
`3a2ab56` / `95dafb6` / `2963aa8` / `26a721e`).

**Section B (Wave 2)**: backend functionally complete; UI in flight.

| Task | Status | Tests | Commits |
|---|---|---|---|
| **B1** Meta proposal kinds (4-2A) | ✅ backend | 5+13+5 | `07e8eee` `db5ff99` `a649aff` `674462e` `e9b0784` `81bbbb0` |
| ↳ iter 1 4 new EvolutionKind variants | ✅ | 3 | `07e8eee` |
| ↳ iter 2 metadata JSON column | ✅ | 4 | `db5ff99` |
| ↳ iter 3 dual-clause recursion guard | ✅ | 7 | `a649aff` |
| ↳ iter 4 4 meta apply handlers | ✅ | 13 | `674462e` |
| ↳ iter 5 operator capability gate | ✅ | 5 | `e9b0784` `81bbbb0` (boot) |
| ↳ iter 6+ UI (meta_pending tab + double-confirm) | 🟡 in flight | — | — |
| **B2** Cross-channel identity (4-2B) | ✅ full stack | 36+13+3 | `e05be35` → `e903fa2` + `399adb7` |
| ↳ iters 1-4 primitive crate | ✅ | 36 | `e05be35` `63756c5` `5d07c84` `8eb2fca` `8bed667` |
| ↳ iter 5 chat-path resolution | ✅ | 3 | `66d24b1` |
| ↳ iter 6 admin REST routes | ✅ | 13 | `5815263` |
| ↳ iter 7 admin UI page | ✅ | — | `e903fa2` |
| ↳ iter 8 HookEvent.user_id propagation | ✅ | — | `399adb7` |
| **B3** Per-tenant evolution federation (4-2C) | ✅ backend | 5+9+17 | `f2dd619` `73efbb3` `0da2d76` |
| ↳ iter 1 tenant_federation_peers table | ✅ | 5 | `f2dd619` |
| ↳ iter 3+4 share_with + rebroadcaster | ✅ | 9 | `73efbb3` |
| ↳ iter 5 admin REST | ✅ | 17 | `0da2d76` |
| ↳ iter 6+ admin UI | 🟡 in flight | — | — |
| **B4** Trajectory replay (4-2D) | ✅ shipped | — | `90411db` |

## Wave 2 in numbers

- **17 successful agent dispatches** (1 retry after worktree-baseline mishap)
- **56 commits** since Wave 1 closed
- **84 tests added** across the wave (B1: 28 + B2: 52 + B3: 31 — primitive + integration)
- Workspace test totals: corlinman-core 102 / corlinman-evolution 42 / corlinman-gateway 350 / corlinman-identity 36 / corlinman-tenant 30 / corlinman-hooks 8

**B1 + B3 designs**: `docs/design/phase4-w2-b1-design.md` (10-iter plan,
capability-list operator gate + dual-clause recursion guard) and
`docs/design/phase4-w2-b3-design.md` (10-iter plan, asymmetric opt-in +
two-clause loop prevention). Both produced by parallel
Software-Architect background agents. **Implementation through iter 5
on both tracks; UI iters in flight.**

---

## A. Wave 1 cleanup (≈ 1 week, 7 items)

Small finish-the-job items the Wave 1 PR explicitly deferred or that
turned up during execution. Land these as a follow-up batch (or
separate PRs if reviewers prefer) before starting Wave 2.

| # | Task | Estimate | Why it matters |
|---|---|---|---|
| **A1** ✅ | **`HookEvent.tenant_id` propagation** [done `3a2ab56`] through the chat request lifecycle. Adds `tenant_id: Option<String>` to message-scoped variants, plumbs tenant context from the gateway request middleware down to every emit site, has `EvolutionObserver` honor the field when persisting signals. | 2-3d | Multi-tenant signal correctness — currently every signal attributes to the reserved `default` tenant, so per-tenant proposers see merged data. Read-path 403 isolation still holds; this fixes the write-path attribution. |
| **A2** ✅ | **Operator-initiated rollback** [done `95dafb6`] — `POST /admin/evolution/:id/rollback` route in `routes/admin/evolution.rs` + UI button in `/admin/evolution`. AutoRollback monitors already drive `EvolutionApplier::revert` programmatically; this surface is for manual operator action. | 0.5d | UI gap — operators can apply but can't manually undo without DB surgery. |
| **A3** ✅ | **DriftMismatch UX plumbing** [done `95dafb6`] — `apply_tool_policy` returns `ApplyError::DriftMismatch{expected, actual}` but the route layer flattens to a generic 4xx. Plumb `actual` through the JSON envelope so the operator sees the on-disk mode that diverged. | 0.5d | Operator can't re-evaluate without re-querying. Pure surface change. |
| **A4** ✅ | **`GET /admin/tenants/:tenant/{prompt_segments,agent_cards}/:name`** [done `95dafb6`] returning `{exists, content}`. The operator UI's diff view needs this to render the live `before` since `diff.before` is empty by design (Python proposer stays decoupled from the persona crate). | 1d | UI diff view currently shows only `after`. |
| **A5** ✅ | **Shared slug-validation spec** [done `2963aa8`] — `corlinman-tenant::TenantId::new` (Rust) and `ui/lib/api/tenants.ts` (TS) duplicate the regex `^[a-z][a-z0-9-]{0,62}$`. Add a shared YAML spec under `docs/contracts/` + a codegen step OR extract a single source-of-truth string and import it cross-language. | 0.5d | Drift defeats the mock's purpose — UI accepts a slug Rust will then reject. |
| **A6** ✅ | **Worktree cleanup** [done — A7 era, on disk] — `.claude/worktrees/` accumulated 30+ locked agent worktrees over Phase 3.1 + Phase 4. Force-remove the stale ones. | 0.2d | Disk hygiene only; harness still works fine with them present. |
| **A7** ✅ | **Flaky test fix in `corlinman-evolution::repo::tests`** [done `26a721e`] — `history_latest_for_proposal_round_trip` and `intent_log_record_then_commit_clears_uncommitted` fail intermittently under `cargo test --workspace`'s parallel scheduling. Each passes in isolation and with `--test-threads=1`. Fixed by `EvolutionStore::open_with_pool_size(1)` test fixture + serialising `connect_with` inside the `TenantPool` cache lock. | 1-2d | CI red on rerun; triage hasn't pinned the shared resource (likely sqlx static state or filesystem disk-IO contention). |

**Suggested grouping**: A1 → its own PR (cross-cutting). A2+A3+A4 →
one UI/Rust pair PR. A5+A6+A7 → individual chore PRs.

---

## B. Wave 2 (≈ 4-5 weeks, 4 items)

Recursive self-improvement + cross-channel identity. The big leap
Phase 4 promises. Per `phase4-roadmap.md` §4 Wave 2.

| # | Task | Estimate | Concept |
|---|---|---|---|
| **B1** | **4-2A Meta proposal kinds** [design pending integration] — new `EvolutionKind` variants `engine_config` / `engine_prompt` / `observer_filter` / `cluster_threshold`. Engine improves the engine that improves it. Strict one-level recursion guard (semantic via `trace_id` descent + per-`(tenant, kind)` cooldown), operator-only via `[admin] meta_approver_users` capability list, double-confirm UI on `engine_prompt`. `meta_grace_window_hours = 24` as a peer field on the rollback config. Routes to a separate `meta_pending` admin tab. Detailed plan: `docs/design/phase4-w2-b1-design.md` (10 iters). | 7-9d | Self-improvement core. Highest risk; requires `auto_rollback` window tightened (24h vs the default 72h). |
| **B2** 🟡 | **4-2B Cross-channel `UserIdentityResolver`** [iters 1-4 done in `e05be35`/`63756c5`/`5d07c84`/`8bed667`] — `user_identity.sqlite` schema; verification phrase exchange protocol (operator triggers from one channel, user pastes in the other); merged trait state via SQL union with confidence-weighted dedup. Primitive crate at 29 unit tests; iters 5-8 are integration (gateway middleware + admin REST + admin UI + HookEvent attribution). Detailed plan: `docs/design/phase4-w2-b2-design.md`. | 6-8d | Same human across QQ + Telegram + native iOS = same `user_id`. |
| **B3** | **4-2C Per-tenant evolution federation (opt-in)** [design `299ea32`] — operator-flagged `share-with-tenants` `skill_update` proposals get rebroadcast as proposals to opted-in tenants; receiving tenant must approve as if local. Detailed plan: `docs/design/phase4-w2-b3-design.md` (10 iters, two-clause loop prevention, asymmetric opt-in). | 5-7d | Tenant A's lessons benefit tenant B without auto-propagating. |
| **B4** ✅ | **4-2D Trajectory replay CLI** [done `90411db`] — `corlinman replay <session_id>` reconstructs a past session deterministically; useful for debugging plus offline replay against new prompts before live deploy. Shipped: `corlinman-replay` crate (`replay()` + `list_sessions()` primitives, 6 tests), `corlinman replay` subcommand (3 tests), `/admin/sessions` + `/admin/sessions/:key/replay` gateway routes (9 tests), and the operator UI page + dialog with mock-server contract (Agent B). Rerun mode ships with a `not_implemented_yet` marker for Wave 2.5 to fill in. | 4-5d | Independent / no upstream deps; good warm-up task. |

**Suggested order**: B4 (independent, low risk) → B2 (high value,
moderate risk) → B1 (highest value, highest risk — needs B4's
replay tooling for offline meta-prompt evaluation) → B3 (federation,
requires B1 to be stable).

---

## C. Wave 3 (≈ 3-4 weeks, 4 items)

MCP interop + native client surfaces. corlinman becomes reachable
from Claude Desktop and a reference Swift app. Per
`phase4-roadmap.md` §4 Wave 3.

| # | Task | Estimate |
|---|---|---|
| **C1** | **4-3A MCP server** — `/mcp` WebSocket; expose tools/skills/memory as MCP capabilities; tested against Claude Desktop's MCP client. | 5-7d |
| **C2** | **4-3B MCP plugin adapter** — `corlinman-plugins` accepts `kind = "mcp"` plugins (any MCP-stdio server becomes a corlinman tool); registry, sandbox, manifest v3. | 5-7d |
| **C3** | **4-3C Canvas Host renderer** — Phase 1 stubbed the protocol; this implements the actual code-block / diagram / table renderer service; Tidepool aesthetic. | 5-7d |
| **C4** | **4-3D Reference Swift macOS client** — minimal SwiftUI app under `apps/swift-mac/`; gRPC bindings to gateway; receives push notifications via APNs (or stubbed local socket for dev); demonstrates the contract for iOS/Android teams. | 7-10d |

C1+C2 share the MCP layer and benefit from being landed together.
C3 and C4 are independent of the MCP work.

---

## D. Wave 4 (≈ 3-4 weeks, partially parallel, 4 items)

Long-horizon cognition. Per `phase4-roadmap.md` §4 Wave 4.

| # | Task | Estimate |
|---|---|---|
| **D1** | **4-4A Episodic memory** — new `episodes` table (event-level summaries, distilled from session ranges); episode = "what happened" not "what was said"; queryable as `{{episodes.last_week}}`. | 6-8d |
| **D2** | **4-4B Goal hierarchies** — agent has short-term (today), mid-term (this week), long-term (this quarter) goals stored in `agent_goals.sqlite`; reflection job grades self-progress; goals influence prompt construction via `{{goals.*}}` placeholders. | 5-7d |
| **D3** | **4-4C Subagent delegation runtime** — agent loop gains `spawn_child(agent_card, task) → Future<TaskResult>`; children inherit memory_host federation, fresh persona, time-bounded; results merge back into parent's context. | 7-10d |
| **D4** | **4-4D Voice surface (alpha)** — gateway `/voice` endpoint accepts realtime audio (whisper-compatible); replies via TTS; one provider (OpenAI realtime / Gemini live) wired; gated under `[voice.enabled]` flag because cost. | 5-7d |

D1 unlocks D2 (goals reference episodes). D3 + D4 are independent
of D1/D2.

---

## E. Engineering debt (cross-cutting, no single deadline)

Quality-of-life infrastructure that benefits every wave going
forward. Pick up opportunistically when adjacent code is being
touched anyway.

| # | Task | Why |
|---|---|---|
| **E1** | **Cross-language schema codegen** — single source of truth for shared types (tenant slug being the first candidate; future: `EvolutionProposal` wire shape, `HookEvent` JSON, agent-card metadata). | Each new shared type currently risks drift between Rust / Python / TS. |
| **E2** | **CI gate** — workspace `cargo clippy -D warnings` + `cargo fmt --check` + `uv run ruff` + `pnpm typecheck` on every PR. The gate exists in spots; not workspace-wide. | Several pre-existing fmt drifts surfaced during Phase 4 because workspace fmt wasn't a gate. |
| **E3** | **Docker shadow sandbox in CI** — current integration test skips when daemon unreachable. Add a CI job that builds `corlinman-sandbox:dev` and runs `sandbox-self-test` end-to-end. | The sandbox has 0 CI signal today; a regression that breaks the image build wouldn't be caught. |
| **E4** | **Phase 4 W2 readiness — config audit** — every `[xxx].enabled` flag the roadmap reserves (`[evolution.meta]`, `[user_identity]`, `[mcp.server]`, `[canvas]`, `[voice]`) needs a coherent default + load test before the wave that uses it lands. | Avoid a Wave 2 W2-C-style "shelved kind" situation. |

---

## Execution recommendations

1. **Wait for PR #1 to merge** before continuing — local main is 16
   commits ahead of `origin/main`; future work should branch off
   the merged main, not the unmerged feat branch.
2. **A1 first** — multi-tenant signal correctness should land in
   the same release window as Wave 1, not after. It's the only
   Wave 1 deferred item that matters for production correctness.
3. **A2 + A3 + A4** can be one PR — small UI / Rust pair, all
   under `routes/admin/evolution.rs` + UI's existing `/admin`
   pages. Reviewable as a single unit.
4. **B4 (trajectory replay)** is the recommended Wave 2 starter —
   independent, useful immediately, and unblocks B1's offline
   meta-prompt evaluation later.
5. **C and D can interleave with B** if multiple developers pick
   up tasks in parallel. The roadmap deliberately split waves to
   keep them mostly independent.

---

## Tracking notes

- This doc is operational — keep entries current as tasks land.
  Mark items with `[done #PR]` when their PR merges.
- New items discovered during execution: append to the end of the
  relevant section with a brief "discovered while X" note so the
  context isn't lost.
- The strategic picture stays in `phase4-roadmap.md`; only
  per-task tactical work goes here.
