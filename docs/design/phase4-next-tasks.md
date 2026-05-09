# Phase 4 — Next Tasks

**Status**: ✅ **Phase 4 fully closed** · **Owner**: TBD · **Last revised**: 2026-05-09 (Wave 3+4 close-out)

> Operational task list following Phase 4 Wave 1's closure
> (PR [#1](https://github.com/ymylive/corlinman/pull/1), 16 commits).
> Companion to `phase4-roadmap.md` — the roadmap is the strategic
> picture, this doc is the tactical pick-up list.

## Progress snapshot (2026-05-09)

**Section A (Wave 1 cleanup)**: ✅ A1-A7 all complete (commits
`3a2ab56` / `95dafb6` / `2963aa8` / `26a721e`).

**Section B (Wave 2)**: ✅ **fully closed** — backend + UI all shipped.

**Section C (Wave 3)**: ✅ **fully closed** — all 4 streams (C1 MCP server, C2 plugin adapter, C3 Canvas Host, C4 Swift macOS client) shipped iter 1-10 and acceptance-passed. Branches `phase4-w3-{c1,c2,c3,c4}` on origin.

**Section D (Wave 4)**: ✅ **fully closed** — all 4 streams (D1 episodic memory, D2 goal hierarchies, D3 subagent runtime, D4 voice alpha) shipped iter 1-10 and acceptance-passed. Branches `phase4-w4-{d1,d2,d3,d4}` on origin.

**Phase 4 in numbers (Wave 3+4)**: 8 stream branches × 10 iters = **80 iters delivered**, **89 commits** across the implementation phase, **~12000+ LOC** of Rust + Python + Swift + TypeScript, **~1000+ new tests**, **8 design docs** (~3540 LOC). Roadmap §4 acceptance criteria all green: Claude Desktop replay (C1) · Canvas code-block round-trip (C3) · Swift demo contract (C4) · `{{episodes.last_week}}` recalls "operator approved skill_update for web_search that fixed timeout" (D1) · `{{goals.weekly}}` produces 4-item distilled list (D2) · subagent fan-out 0.330× serial vs <0.7 threshold (D3) · voice E2E happy-path with tool approval pause (D4).

| Task | Status | Tests | Commits |
|---|---|---|---|
| **B1** Meta proposal kinds (4-2A) | ✅ shipped | 5+13+5+3 | `07e8eee` `db5ff99` `a649aff` `674462e` `e9b0784` `81bbbb0` `193c764` |
| ↳ iter 1 4 new EvolutionKind variants | ✅ | 3 | `07e8eee` |
| ↳ iter 2 metadata JSON column | ✅ | 4 | `db5ff99` |
| ↳ iter 3 dual-clause recursion guard | ✅ | 7 | `a649aff` |
| ↳ iter 4 4 meta apply handlers | ✅ | 13 | `674462e` |
| ↳ iter 5 operator capability gate | ✅ | 5 | `e9b0784` `81bbbb0` (boot) |
| ↳ iter 6+7 UI (meta_pending tab + double-confirm) | ✅ | 3 | `193c764` |
| **B2** Cross-channel identity (4-2B) | ✅ full stack | 36+13+3 | `e05be35` → `e903fa2` + `399adb7` |
| ↳ iters 1-4 primitive crate | ✅ | 36 | `e05be35` `63756c5` `5d07c84` `8eb2fca` `8bed667` |
| ↳ iter 5 chat-path resolution | ✅ | 3 | `66d24b1` |
| ↳ iter 6 admin REST routes | ✅ | 13 | `5815263` |
| ↳ iter 7 admin UI page | ✅ | — | `e903fa2` |
| ↳ iter 8 HookEvent.user_id propagation | ✅ | — | `399adb7` |
| **B3** Per-tenant evolution federation (4-2C) | ✅ shipped | 5+9+17+5 | `f2dd619` `73efbb3` `0da2d76` `c4ec474` |
| ↳ iter 1 tenant_federation_peers table | ✅ | 5 | `f2dd619` |
| ↳ iter 3+4 share_with + rebroadcaster | ✅ | 9 | `73efbb3` |
| ↳ iter 5 admin REST | ✅ | 17 | `0da2d76` |
| ↳ iter 6+ admin UI (`/admin/federation`) | ✅ | 5 | `c4ec474` |
| **B4** Trajectory replay (4-2D) | ✅ shipped + rerun | — | `90411db` `bb28bb8` |
| ↳ rerun mode end-to-end | ✅ | — | `bb28bb8` |

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
| **B1** ✅ | **4-2A Meta proposal kinds** [done `193c764`] — new `EvolutionKind` variants `engine_config` / `engine_prompt` / `observer_filter` / `cluster_threshold`. Engine improves the engine that improves it. Strict one-level recursion guard (semantic via `trace_id` descent + per-`(tenant, kind)` cooldown), operator-only via `[admin] meta_approver_users` capability list, double-confirm UI on `engine_prompt`. `meta_grace_window_hours = 24` as a peer field on the rollback config. Routes to a separate `meta_pending` admin tab. Detailed plan: `docs/design/phase4-w2-b1-design.md` (10 iters). | 7-9d | Self-improvement core. Highest risk; requires `auto_rollback` window tightened (24h vs the default 72h). |
| **B2** ✅ | **4-2B Cross-channel `UserIdentityResolver`** [done `e05be35`→`e903fa2`+`399adb7`] — `user_identity.sqlite` schema; verification phrase exchange protocol (operator triggers from one channel, user pastes in the other); merged trait state via SQL union with confidence-weighted dedup. Primitive crate at 29 unit tests; iters 5-8 integration (gateway middleware + admin REST + admin UI + HookEvent attribution). Detailed plan: `docs/design/phase4-w2-b2-design.md`. | 6-8d | Same human across QQ + Telegram + native iOS = same `user_id`. |
| **B3** ✅ | **4-2C Per-tenant evolution federation (opt-in)** [done `f2dd619`→`c4ec474`] — operator-flagged `share-with-tenants` `skill_update` proposals get rebroadcast as proposals to opted-in tenants; receiving tenant must approve as if local. Detailed plan: `docs/design/phase4-w2-b3-design.md` (10 iters, two-clause loop prevention, asymmetric opt-in). | 5-7d | Tenant A's lessons benefit tenant B without auto-propagating. |
| **B4** ✅ | **4-2D Trajectory replay** [done `90411db` + rerun `bb28bb8`] — `corlinman replay <session_id>` reconstructs a past session deterministically; useful for debugging plus offline replay against new prompts before live deploy. Shipped: `corlinman-replay` crate, `corlinman replay` CLI, `/admin/sessions` + `/admin/sessions/:key/replay` gateway routes, operator UI page + dialog. **Rerun mode landed `bb28bb8`** (was deferred to W2.5; landed early). | 4-5d | Independent / no upstream deps; good warm-up task. |

**Suggested order**: B4 (independent, low risk) → B2 (high value,
moderate risk) → B1 (highest value, highest risk — needs B4's
replay tooling for offline meta-prompt evaluation) → B3 (federation,
requires B1 to be stable).

---

## C. Wave 3 — ✅ **fully closed** (MCP interop + native surfaces)

corlinman is reachable from Claude Desktop, native Swift macOS, and the
admin UI's Canvas Host. All 4 streams shipped iter 1-10. Design docs:
`phase4-w3-{c1,c2,c3,c4}-design.md`. Branches on origin: `phase4-w3-{c1,c2,c3,c4}`.

| Task | Status | Final HEAD | Branch |
|---|---|---|---|
| **C1** 4-3A MCP server | ✅ iter 1-10 | `6bdaa73` | `phase4-w3-c1` |
| **C2** 4-3B MCP plugin adapter | ✅ iter 1-10 | `89970e1` | `phase4-w3-c2` |
| **C3** 4-3C Canvas Host renderer | ✅ iter 1-10 | `e0b0118` | `phase4-w3-c3` |
| **C4** 4-3D Reference Swift macOS client | ✅ iter 1-10 | `6dd23f8` | `phase4-w3-c4` |

### C1 — MCP server (`corlinman-mcp` crate, `/mcp` WebSocket)

| Iter | Title | Commit |
|---|---|---|
| 1 | crate skeleton + JSON-RPC schema | `2b3e691` |
| 2 | `McpError` + JSON-RPC error mapping | `cf8fc12` |
| 3 | `SessionState` handshake state machine | `e556436` |
| 4 | WS transport + `/mcp` route + stdio client | `7d89510` |
| 5 | `CapabilityAdapter` trait + tools adapter | `cbf4ee6` |
| 6 | prompts adapter (wrapping SkillRegistry) | `dad069b` |
| 7 | `MemoryHost::get` extension + resources adapter | `92142e6` |
| 8 | auth ACL + tenant scoping | `28403f0` |
| 9 | gateway integration — `/mcp` mount + `McpConfig` | `f9e81b2` |
| 10 | Claude Desktop fixture replay + `mcp-cli-smoke` example | `6bdaa73` |

### C2 — MCP plugin adapter (`corlinman-plugins` `plugin_type = "mcp"` v3)

| Iter | Title | Commit |
|---|---|---|
| 1 | manifest v3 schema + `[mcp]` table | `a5268d5` |
| 2 | stdio spawn + reap primitive | `64f370f` |
| 3 | env passthrough + redaction | `5f24b69` |
| (merge) | merge `phase4-w3-c1` for stdio client | `0a24428` + `abda287` fixup |
| 4 | MCP adapter handshake | `367c6e6` |
| 5 | `tools/list` filter + multiplexed `tools/call` | `db982c2` |
| 6 | crash-restart supervisor | `dbeffd2` |
| 7 | `PluginRuntime` trait impl | `e76d743` |
| 8 | admin disable/enable/restart + sentinel | `3a28e59` |
| 9 | v2→v3 manifest migration polish | `2a3863b` |
| 10 | E2E vs Python echo MCP server fixture | `89970e1` |

### C3 — Canvas Host renderer (`corlinman-canvas` crate)

| Iter | Title | Commit |
|---|---|---|
| 1 | crate skeleton + protocol types | `5011139` |
| 2 | code adapter (syntect, class-based) | `342638c` |
| 3 | table adapter (markdown + csv) | `ac85f3a` |
| 4 | LaTeX adapter (`katex-rs` 0.2.x) | `e20b619` |
| 5 | sparkline adapter (hand-rolled SVG) | `0459824` |
| 6 | mermaid scaffold (deno_core, gated) | `5c01dd8` |
| 7 | blake3 LRU render cache | `592007e` |
| 8 | `POST /canvas/render` gateway route | `f07a9d7` |
| 9 | UI artifact rendering components | `f9fc94a` |
| 10 | E2E acceptance + close iter-9 gaps + `[canvas]` config | `e0b0118` |

### C4 — Reference Swift macOS client (`apps/swift-mac/`)

| Iter | Title | Commit |
|---|---|---|
| 1 | SwiftPM skeleton + 3-target split | `58cbe41` |
| 2 | `POST /admin/api_keys` mint endpoint | `b86cf18` |
| 3 | `POST /v1/chat/completions/:turn_id/approve` | `a3809a6` |
| 4 | SSE chat-stream parser | `9fe75fd` |
| 5 | local `SessionStore` over system SQLite3 | `807493e` |
| 6 | `ChatViewModel` + `ChatView`/`MessageList`/`Composer` | `110b223` |
| 7 | AuthStore (Keychain) + onboarding flow | `608f00d` |
| 8 | push receiver + APNs scaffolding | `2b8032b` |
| 9 | snapshot tests + macOS CI workflow | `b58c54e` |
| 10 | E2E acceptance + ApprovalSheet + demo contract docs | `6dd23f8` |

---

## D. Wave 4 — ✅ **fully closed** (long-horizon cognition)

Episodic memory, goal hierarchies, subagent delegation, and voice alpha. All
4 streams shipped iter 1-10 with Wave 4 acceptance criteria green. Design
docs: `phase4-w4-{d1,d2,d3,d4}-design.md`. Branches on origin: `phase4-w4-{d1,d2,d3,d4}`.

| Task | Status | Final HEAD | Branch |
|---|---|---|---|
| **D1** 4-4A Episodic memory | ✅ iter 1-10 | `7c7a611` | `phase4-w4-d1` |
| **D2** 4-4B Goal hierarchies | ✅ iter 1-10 | `70a0518` | `phase4-w4-d2` |
| **D3** 4-4C Subagent delegation runtime | ✅ iter 1-10 | `cabef63` | `phase4-w4-d3` |
| **D4** 4-4D Voice surface (alpha) | ✅ iter 1-10 | `a95bc33` | `phase4-w4-d4` |

### D1 — Episodic memory (`corlinman-episodes` Python + Rust resolver)

| Iter | Title | Commit |
|---|---|---|
| 1 | package skeleton + schema | `3d7ba1f` |
| 2 | distillation primitives | `e1f9d63` |
| 3 | importance + LLM distill | `3657f29` |
| 4 | distillation orchestrator/runner | `19e479f` |
| 5 | second-pass embedding writer | `200046b` |
| 6 | `distill-once` CLI + provider hooks | `a739635` |
| 7 | gateway `{{episodes.*}}` resolver | `3ca598b` |
| 8 | cold archival sweep | `7d3a55e` |
| 9 | rehydration + CLI | `17476ae` |
| 10 | Wave 4 acceptance E2E (operator-approved skill_update recall) | `7c7a611` |

### D2 — Goal hierarchies (`corlinman-goals` Python)

| Iter | Title | Commit |
|---|---|---|
| 1 | package skeleton + `agent_goals.sqlite` schema | `da8b7e3` |
| 2 | tier window math + `update`/`archive` CRUD | `0a5a3a0` |
| 3 | `GoalsResolver` + four `{{goals.*}}` keys | `9029d8e` |
| (merge) | merge `phase4-w4-d1` for episode runtime | `8fea342` |
| 4 | episode evidence module + D1 bridge | `6b932fe` |
| 5 | reflection job runner with mock LLM | `a5a07a6` |
| 6 | cascade aggregation evaluator | `3f32c35` |
| 7 | CLI surface + provider factory hooks | `0950c39` |
| 8 | `{{goals.weekly}}` cascade aggregation | `300d64d` |
| 9 | `goal.weekly_failed` evolution signal emission | `d012ad4` |
| 10 | Wave 4 acceptance E2E (4-item weekly distill) | `70a0518` |

### D3 — Subagent delegation runtime (`corlinman-subagent` Rust + Python via PyO3)

| Iter | Title | Commit |
|---|---|---|
| 1 | crate skeleton + types | `1f0af0e` |
| 2 | `ReadOnlyMemoryHost` adapter | `787dac6` |
| 3 | `SubagentSupervisor` cap accountant | `d2f95f9` |
| 4 (partial) | Python `run_child` happy path (code) | `88733e3` |
| 4 (tests backfill) | 5 design-mandated runner tests | `c3f3be3` |
| 5 | PyO3 bridge | `5c199ff` |
| 6 | tokio timeout enforcement | `e449f08` |
| 7 | tool-allowlist filter + escalation reject | `0ce38df` |
| 8 | tool-wrapper + parent-loop dispatch | `223aadc` |
| 9 | hook events + evolution-signal linking | `e2c1f27` |
| 10 | research-fan-out E2E benchmark (0.330× serial vs <0.7 threshold) | `cabef63` |

### D4 — Voice surface alpha (`corlinman-gateway/routes/voice/`)

| Iter | Title | Commit |
|---|---|---|
| 1 | `[voice]` config + 503 stub route | `2d1ea99` |
| 2 | WS framing primitives + subprotocol negotiation | `d835cd1` |
| 3 | cost-gating primitives | `81d3789` |
| 4 | provider trait + `MockEchoProvider` | `d1e6b7c` |
| 5 | OpenAI Realtime adapter (env-gated) | `2d76a15` |
| 6 | `voice_sessions` SQLite persistence + transcript sink | `7d87a9e` |
| 7 | tool-approval pause bridge | `b427afc` |
| 8 | budget enforcer + 1-Hz checkpoint ticker | `5d7b60e` |
| 9 | handler hot-path bridge | `169d0e4` |
| 10 (close iter-9 gaps) | session_key from `start` frame + audio_path retention | `40087778` |
| 10 (E2E) | E2E happy-path full bridge surface | `a95bc33` |

---

## F. Phase 5 deferrals (carried forward from Phase 4 streams)

Discovered while implementing Wave 3+4. Each stream's iter 10 close-out
flagged what intentionally got punted. Pick up these in Phase 5 planning.

### From Wave 3

- **C1** — real Claude Desktop session-capture replaces synthesised fixture; real Rust `MemoryHost` wiring at the gateway layer (today gateway hands resources adapter an empty `BTreeMap`); `resources/subscribe`; sampling capability (Wave 4-4C territory)
- **C2** — extract `corlinman-mcp-schema` leaf crate to break the `corlinman-plugins → corlinman-mcp` cycle that forced schema vendoring; re-export `corlinman_mcp::client` from lib.rs; `POST /admin/plugins` (add manifest at runtime) + `DELETE /admin/plugins/:name`; gateway `AppState.mcp_adapter` wiring + `chat.rs` `PluginType::Mcp` dispatcher branch
- **C3** — Mermaid feature-build E2E (V8 link cost too high for default CI); `/canvas/render` retirement (waits for Swift consumers); producer auto-detection
- **C4** — gateway `[channels.dev_push]` writer; `swift-snapshot-testing` committed to `Package.swift`; `POST /v1/devices` token-registration endpoint

### From Wave 4

- **D1** — Rust-side transparent rehydrate of cold episodes (today only Python `rehydrate-all` CLI escape hatch); `{{episodes.about(<tag>)}}` cosine rerank
- **D2** — real `corlinman-providers`-backed `Grader` factory wired at gateway boot; admin UI for goal-setting; auto-goal-setting from external systems; cross-agent goal-sharing; `goal_set` evolution kind for agent-authored goals
- **D3** — gateway dispatcher tool-call → `dispatch_subagent_spawn` route (PyO3) at agent-servicer boot; `Supervisor::with_hook_bus` install at gateway boot to actually emit iter-9 hook events in production; operator UI tree visualisation
- **D4** — actual PCM byte-stream writer for `retain_audio = true` (path is recorded; file writes parked behind `corlinman-voice` Python package per `phase4-roadmap.md:330`); retention sweeper job; Python `.wav` streaming harness; SQLite-backed `voice_spend` table (currently in-memory); Gemini Live as second provider

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

## Integration plan (Phase 4 → main)

All 8 stream branches live on origin parallel to `main`. Decide an
integration strategy before opening Phase 5:

1. **Open 8 draft PRs** (one per `phase4-w{3,4}-{c,d}{1-4}` branch)
   for code review, then merge in dependency order:
   - First: C1, D1 (no dependencies)
   - Second: C2 (depends on C1's `McpClient::connect_stdio`), D2 (depends on D1's episode runtime). Both already merged the dependency in their branch.
   - Third: C3, C4, D3, D4 (independent)
2. Each PR carries its own iter-by-iter commit history; squash-merge
   into main is OK if reviewers prefer flat history, otherwise
   merge-with-merge-commit preserves the iter trail.
3. After all 8 land on main, open a "Phase 4 close-out" PR that:
   - Updates `docs/design/phase4-roadmap.md` to mark §4 fully shipped
   - Cherry-picks the per-iter design-flaw notes from agent reports
     into respective design docs as "Implementation deltas"
   - Adds Phase 5 deferrals (above) to a new `phase5-next-tasks.md`

## Execution recommendations (Phase 5 ramp-up)

1. **Phase 5 design-doc round first** — same pattern that worked
   for Wave 3+4: dispatch parallel Software Architect agents to
   produce 10-iter design specs grounded in current code, then
   review before implementation dispatches.
2. **Parallel-dispatch cadence learned in Phase 4**: ≤3 streams
   per round, each in a pre-created `.claude/worktrees/<name>/`,
   with explicit "no `cd` out, no editing other worktrees" prompts.
   See `~/.claude/projects/-Users-cornna-project-corlinman/memory/agent_worktree_caveats.md`
   for the full lessons-learned pattern.
3. **Engineering debt (E1-E4 below)** is unblocked now that Phase 4
   is closed; pick up opportunistically as Phase 5 streams touch
   adjacent code.

---

## Tracking notes

- This doc is operational — keep entries current as tasks land.
  Mark items with `[done #PR]` when their PR merges.
- Phase 4 implementation history is preserved in the per-iter
  commit lists above; the strategic picture stays in
  `phase4-roadmap.md`.
- Phase 5 tactical work belongs in a new `phase5-next-tasks.md`
  once Phase 5 scoping starts.
