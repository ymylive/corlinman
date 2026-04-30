# Phase 4 Roadmap — Multi-Tenant · Recursive Self-Improvement · Embodied Reach

**Status**: Draft · **Target window**: 12-16 weeks · **Owner**: TBD · **Last revised**: 2026-04-29 (drafted 2026-04-24; synced post-Phase 3.1)

> Phase 3 closed the cognitive primitives loop: agent observes itself,
> proposes changes through EvolutionLoop, and now has memory decay /
> consolidation, a user model, and persona persistence — the "类人
> baseline". Phase 4 takes the next leap toward "超越人类":
>
> 1. **Out of single-instance** — corlinman runs as a platform, not a
>    pet. Multi-tenancy + federation + cross-channel identity.
> 2. **Recursive** — agent improves the system that improves it.
>    EvolutionEngine becomes itself a target of EvolutionLoop, behind a
>    one-level recursion guard.
> 3. **Embodied** — backend exposes contracts that real client surfaces
>    (iOS / macOS / Android / browser-via-MCP) plug into. Voice. Canvas
>    rendering. Subagent fan-out as a runtime primitive instead of a
>    dev-time pattern.

---

## 1. Goals & Non-Goals

### Goals
1. **Platform-grade multi-tenancy** — single corlinman process serves
   N tenants with strict data isolation, per-tenant evolution budgets,
   per-tenant user models, opt-in cross-tenant skill sharing.
2. **Hardened sandbox** — docker-isolated ShadowTester for the high-risk
   kinds (`prompt_template` / `tool_policy` / `new_skill`) we shelved
   in Phase 3 W2-C. With sandbox, those flip on safely.
3. **Recursive self-improvement** — Engine generates proposals that
   modify Engine config / prompts / clustering thresholds. Strict
   one-level recursion (a meta-proposal cannot itself generate
   meta-proposals). Operator-only approval for meta kinds.
4. **Cross-channel user identity** — same human contacted via QQ +
   Telegram + native iOS app maps to one `user_id` with merged trait
   state. Privacy-preserving (operator opts in per-channel).
5. **MCP interop** — corlinman speaks Model Context Protocol both as
   server (Claude Desktop / mcp-aware clients use corlinman as a
   knowledge/tool provider) and as client (corlinman uses external MCP
   servers as plugins).
6. **Native client surfaces** — minimal but real iOS / macOS / Android
   apps backed by the gateway. Canvas Host renders code/diagrams.
   Subagent delegation as a runtime API agent loops can call.
7. **Long-horizon cognition** — episodic memory of "events" (not just
   chunks); goal hierarchies (agent has its own short/medium/long-term
   goals); trajectory replay for both debugging and offline learning.

### Non-Goals
- No model fine-tuning. Phase 4 still ships only inference-time evolution.
- No autonomous code generation against `rust/` or `python/` source.
- No public-internet-facing multi-tenant deployment without enterprise
  auth (OAuth2 / SAML) shipping first — out of scope for this phase.
- No browser-extension corlinman client. MCP via Claude Desktop is the
  browser-adjacent surface.
- No native iOS/Android apps written here from scratch — Phase 4 ships
  the **contracts** (gRPC / OpenAPI / push-notification protocol) plus
  one reference Swift client. Actual app development is parallel.
- No "agent self-rewrites its scheduler config" — the recursion guard
  excludes scheduler / hook bus / config loader. Those stay
  human-mutable.

---

## 2. Architecture Overview (vs Phase 3)

```
                       [ Phase 3 baseline ]
   hooks → Observer → signals → Engine → proposals → Applier → history
                                   │
                            ShadowTester
                            AutoRollback
                            Budget gate
   + cognition: memory decay · user model · persona

                       [ + Phase 4 deltas ]

   Multi-tenant boundary:
     all SQLite paths get tenant prefix; AdminState carries TenantId;
     all hook events / signals / proposals scoped per tenant.

   Recursive self-improvement:
     new EvolutionKind variants — engine_config / engine_prompt /
     observer_filter / cluster_threshold — go through a separate
     "meta proposals" queue with stricter approval rules.

   Sandbox hardening:
     ShadowTester gains a docker backend (image: corlinman-sandbox).
     Prompt/tool kinds run there before the queue.

   Cross-channel identity:
     UserIdentityResolver maps (channel, sender_id) → canonical
     user_id, deduping across channels with opt-in proof
     (verification phrase exchanged once per channel pair).

   MCP layer:
     - server: corlinman exposes /mcp WebSocket; tools/skills/memory
       are MCP-discoverable
     - client: corlinman-plugins gains an MCP-stdio adapter; existing
       plugin-manifest gets a new `kind = "mcp"` shape

   Native surfaces:
     gateway gains gRPC + push-notification protocol; one Swift
     reference client lives under apps/swift-mac as a working example;
     Canvas Host implements the renderer (not just the protocol stub
     from Phase 1)

   Subagent delegation:
     agent_loop gains spawn_child(agent_card, task) returning a future;
     children share parent's memory_host federation but get fresh
     persona; lifetime bounded by parent

   Long-horizon cognition:
     episodes table (event-level memory above chunks); goals table
     (agent's own short/mid/long-term plans); trajectory_replay CLI
     reconstructs any past session for debugging or replay-training
```

---

## 3. Phase 3.1 Status (sync as of 2026-04-29)

Phase 4 was drafted on 2026-04-24. Five days later three Phase 3.1
patches landed (PR #148 / #149 / #150) that pre-paid part of Wave 1's
bill. This section captures what Wave 1 inherits versus what still
needs new code.

### Already landed — trims Wave 1 scope

- **`tenant_id` schema seed** (Tier 3 / S-2): `user_traits` and
  `agent_persona_state` carry `tenant_id NOT NULL DEFAULT 'default'` +
  composite indexes; `ALTER` is idempotent via `PRAGMA table_info`,
  pre-3.1 DBs converge on first boot; Python CLIs (`persona`,
  `user_model`) accept `--tenant-id`. *Remaining for 4-1A:* Rust
  `TenantId` newtype, `AdminState` / hook / config wiring, the other
  four SQLite files (`evolution`, `kb`, `sessions`, `agent_state`),
  per-tenant directory layout, gateway middleware tenant-scoping.
- **Sandbox path canonicalize** (Tier 3 / S-5): `MemoryOpSimulator`
  and `SkillUpdateSimulator` canonicalize `kb_path` on entry, assert
  it lives under tempdir, re-canonicalize parent before `fs::write`
  — TOCTOU symlink defense with regression test. *Remaining for
  4-1C:* the docker backend itself (image build, `network=none`,
  cgroup limits, drop-all caps, non-root). In-process sandbox is now
  safe enough for the deterministic pre-flight; docker is still
  required before `prompt_template` / `new_skill` because those can
  call out to a live LLM.
- **PII redactor expansion** (Tier 3 / S-1): intl-phone, national-ID
  (X), Luhn-bank-card, IPv4, QQ regexes; LLM-distilled output runs
  the redactor a second time; `evidence` column dropped before
  persistence. Phase 4's episode store (4-4A) and cross-channel
  trait merge (4-2B) reuse this redactor — **no separate redaction
  work item needed**.
- **`inverse_diff` trust contract** (Tier 3 / S-3): revert paths
  whitelist `prior_namespace`, length-cap path / name / target,
  charset-validate, reject control / null bytes; tampered rows
  surface as `ApplyError::Tampered` and the monitor records a
  corruption-skip. Meta proposals (4-2A) reuse this on apply.
- **Apply intent log** (Tier 3 / S-10): `intent → kb mutation →
  committed` for every forward apply; half-committed rows
  (`committed_at` and `failed_at` both NULL) surface at gateway
  startup via `tracing::warn` + `apply.half_committed` hook event.
  The audit primitive Phase 4 meta proposals were going to need is
  therefore already in place.
- **Scheduler wired into prod** (Tier 1): cron jobs were dead code
  before; gateway `main` now spawns `corlinman_scheduler` after the
  observer is up, and the prod image ships `shadow-tester` +
  `auto-rollback` CLIs. Phase 4's daily episode / goal jobs (4-4A,
  4-4B) attach to this same scheduler instead of standing up a new
  one.
- **W3-A correctness floor** (Tier 2): decay zero-point (COALESCE
  `last_recalled_at` / `created_at`), status-aware dedup,
  `inverse_diff.prior_consolidated_at`, cold-start cooling period
  (`cooling_period_hours`, default 24h). Episode distillation
  (4-4A) sits on top of W3-A — this prerequisite is now actually
  stable instead of nominally shipped.

### Implicit decisions (now baked-in)

- §7 Q1 (tenant id format): 3.1 chose `TEXT` column with `'default'`
  as the legacy value — **slug-shape is the de-facto standard**.
  Phase 4 inherits rather than re-litigates.

### Wave 1 focus, post-3.1

- Rust-side multi-tenancy abstraction (`TenantId` newtype + scoping
  helpers + middleware), admin UI tenant switcher, schema convergence
  on the remaining four SQLite files, per-tenant directory layout
- ShadowTester docker backend (image / network / cgroup) and the
  switches that turn on `prompt_template` / `tool_policy` /
  `new_skill`
- 4-1D itself (the W2-C unblocker) — unchanged, still gated on 4-1C
  finishing

### Wave 1 outcome (closed 2026-04-30, PR [#1](https://github.com/ymylive/corlinman/pull/1))

16 atomic commits land the full Wave 1 surface end-to-end. Wave 1
acceptance is met:

- ✅ 3-tenant deployment boots clean on a single gateway
- ✅ `corlinman tenant create acme` (CLI) and `POST /admin/tenants`
  (route) both create the per-tenant data dir + admin login
- ✅ Operator scoped to tenant A can't see tenant B's proposals —
  middleware returns HTTP 403 `tenant_not_allowed`; covered by 8
  integration cases in `tests/tenant_isolation.rs`
- ✅ All three Phase 4 high-risk kinds (`prompt_template`,
  `tool_policy`, `agent_card`) wired Python proposer → ShadowTester
  gating → Rust applier → AutoRollback revert

Test surface as of merge candidate:

- `cargo test --workspace`: green, 0 failures
- `cargo clippy --workspace --all-targets -- -D warnings`: clean
- `uv run pytest python/packages/corlinman-evolution-engine/`: 90
  passed (24 of which are Phase 4 W1 4-1D additions)
- `pnpm test` in `ui/`: 277 passed (32 Phase 4 W1 4-1B additions)

**Deferred to Wave 1.5 follow-up** (tracked in
[`phase4-next-tasks.md`](./phase4-next-tasks.md) section A):

- Item 2b — `HookEvent.tenant_id` propagation through the chat
  request lifecycle (cross-cutting refactor; Wave 1 acceptance is
  read-path scoping which is already enforced)
- Operator-initiated rollback HTTP route + UI button
- DriftMismatch UX plumbing (applier emits actual mode; route layer
  doesn't yet plumb it through)
- `GET /admin/tenants/:tenant/{prompt_segments,agent_cards}/:name`
  for the operator UI's diff view
- Shared slug-validation spec (Rust `TenantId::new` + TS
  `tenants.ts` regex are duplicated; codegen or shared spec when a
  third caller appears)

---

## 4. Wave Structure

Four waves, each ~3-4 weeks. W1 + W2 partially parallel; W3 needs W2
done (sandbox hardening unlocks risky kinds); W4 mostly independent.

### Wave 1 — Multi-Tenancy + Sandbox Hardening (3-4 weeks)

Goal: corlinman ships as a platform, not a pet. Unblock Phase 3 W2-C.

| ID | Title | Stack | Wkload |
|---|---|---|---|
| **4-1A** | **Tenant boundary** — `TenantId` newtype carried through `AdminState` / hooks / config / SQLite path; per-tenant `evolution.sqlite` / `kb.sqlite` / `agent_state.sqlite` / `user_model.sqlite`; admin auth carries tenant claim. *(Schema seed for `user_traits` + `agent_persona_state` and Python CLI `--tenant-id` flags already landed in Phase 3.1 Tier 3 / S-2; see §3.)* | Rust + Python | 5-7d |
| **4-1B** | **Tenant admin UI** — `/tenants` page (operator only); per-tenant `/evolution`, `/memory`, `/user-model`, `/agent-state` views scope by `?tenant=` query | UI | 4-5d |
| **4-1C** | **Docker shadow sandbox** — ShadowTester gains a `[evolution.shadow.sandbox] kind = "docker"` mode; runs evals in a frozen `corlinman-sandbox:vN` image; prompt/tool kinds require this sandbox. *(In-process simulators got TOCTOU canonicalize in Phase 3.1 Tier 3 / S-5; this work item is now the docker backend only — image build, `network=none`, cgroup, drop-all caps, non-root.)* | Rust + DevOps | 4-5d |
| **4-1D** | **W2-C unblocker — agent_card + prompt_template + tool_policy** — Engine handlers + Applier extensions + UI surface; gated on docker sandbox passing eval set | Python + Rust + UI | 6-8d |

**Wave 1 acceptance**: a fresh corlinman boots clean as a 3-tenant
deployment; running `corlinman tenant create acme` creates the tenant's
data dir + admin login; an operator scoped to tenant A can't see tenant
B's proposals. A `prompt_template` proposal goes through docker shadow
with measurable metric deltas before reaching the operator's queue.

### Wave 2 — Recursive Self-Improvement + Cross-Channel Identity (4-5 weeks)

Goal: agent improves the system that improves it. Same human across
channels = same user model.

| ID | Title | Stack | Wkload |
|---|---|---|---|
| **4-2A** | **Meta proposal kinds** — new `EvolutionKind` variants `engine_config` / `engine_prompt` / `observer_filter` / `cluster_threshold`; meta-proposals route to a separate `meta_pending` UI tab; stricter approval rules (operator-only, double-confirm UI for `engine_prompt`); recursion guard prevents meta-proposals from generating further meta-proposals | Python + Rust + UI | 7-9d |
| **4-2B** | **Cross-channel `UserIdentityResolver`** — `user_identity.sqlite` schema; verification phrase exchange protocol (operator triggers from one channel, user pastes in the other); merged trait state via SQL union with confidence-weighted dedup | Python | 6-8d |
| **4-2C** | **Per-tenant evolution federation (opt-in)** — operator-flagged "share-with-tenants" skill_update proposals get rebroadcast as proposals to opted-in tenants; receiving tenant must approve as if local | Rust + Python | 5-7d |
| **4-2D** | **Trajectory replay CLI** — `corlinman replay <session_id>` reconstructs a past session deterministically; useful for debugging plus offline replay against new prompts before live deploy | Rust | 4-5d |

**Wave 2 acceptance**: agent files an `engine_prompt` proposal that
rewrites its own clustering prompt for higher signal-to-noise; goes
through approval queue with double-confirm; applied; metrics show
proposal yield-per-week up. A user verified across QQ + Telegram has
unified `{{user.interests}}` returning combined traits weighted by
confidence.

### Wave 3 — MCP Interop + Native Surfaces (3-4 weeks)

Goal: corlinman is reachable from where humans actually live (Claude
Desktop, native apps, Canvas), not just admin UI.

| ID | Title | Stack | Wkload |
|---|---|---|---|
| **4-3A** | **MCP server** — `/mcp` WebSocket; expose tools/skills/memory as MCP capabilities; tested against Claude Desktop's MCP client | Rust | 5-7d |
| **4-3B** | **MCP plugin adapter** — `corlinman-plugins` accepts `kind = "mcp"` plugins (any MCP-stdio server becomes a corlinman tool); registry, sandbox, manifest v3 | Rust | 5-7d |
| **4-3C** | **Canvas Host renderer** — Phase 1 stubbed the protocol; this implements the actual code-block / diagram / table renderer service; Tidepool aesthetic | Rust + UI | 5-7d |
| **4-3D** | **Reference Swift macOS client** — minimal SwiftUI app under `apps/swift-mac/`; gRPC bindings to gateway; receives push notifications via APNs (or stubbed local socket for dev); demonstrates the contract for iOS/Android teams | Swift + Rust | 7-10d |

**Wave 3 acceptance**: Claude Desktop adds corlinman as an MCP server
and can call its tools / read its memory. The reference Swift client
sends a message → gets a streamed response → memory persists across
launches. Canvas Host renders a code block from agent output as
syntax-highlighted HTML inside the admin UI.

### Wave 4 — Long-Horizon Cognition (3-4 weeks; partially parallel)

Goal: agent has structured memory above the chunk level and goals of
its own.

| ID | Title | Stack | Wkload |
|---|---|---|---|
| **4-4A** | **Episodic memory** — new `episodes` table (event-level summaries, distilled from session ranges); episode = "what happened" not "what was said"; queryable as `{{episodes.last_week}}` | Python + Rust | 6-8d |
| **4-4B** | **Goal hierarchies** — agent has short-term (today), mid-term (this week), long-term (this quarter) goals stored in `agent_goals.sqlite`; reflection job grades self-progress; goals influence prompt construction via `{{goals.*}}` placeholders | Python | 5-7d |
| **4-4C** | **Subagent delegation runtime** — agent loop gains `spawn_child(agent_card, task) → Future<TaskResult>`; children inherit memory_host federation, fresh persona, time-bounded; results merge back into parent's context | Rust + Python | 7-10d |
| **4-4D** | **Voice surface (alpha)** — gateway `/voice` endpoint accepts realtime audio (whisper-compatible); replies via TTS; one provider (OpenAI realtime / Gemini live) wired; gated under `[voice.enabled]` flag because cost | Rust + Python | 5-7d |

**Wave 4 acceptance**: agent on session 30 reports `{{goals.weekly}}`
showing a 4-item list distilled from its own actions over the past 7
days. A complex query ("research topic X, summarize, draft 3 angles")
fans out via subagent delegation and returns aggregated results faster
than serial. The agent has a recallable episode "on 2026-04-22 the
operator approved a skill_update for web_search that fixed timeout"
queryable via natural language.

### Bonus / Stretch
- **Browser extension via MCP-WS** — corlinman as a Claude.ai-tab tool source
- **Federated learning across operator's deployments** — opt-in (deferred from W2-C)
- **Reflection-driven prompt mutations on per-skill basis** (over W2 framework)
- **Self-paced curriculum** — agent picks which skill to deepen this week based on its own goal hierarchy

---

## 5. Architecture Deltas (concrete)

### New crates / packages
- `corlinman-tenant` (Rust) — `TenantId` newtype, scoping helpers, multi-DB pool wrapper
- `corlinman-sandbox` (Rust) — docker-backed shadow execution
- `corlinman-mcp` (Rust) — server + client adapter for Model Context Protocol
- `corlinman-canvas` (Rust) — actual renderer, was stub in Phase 1
- `corlinman-episodes` (Python) — event-level memory above chunks
- `corlinman-goals` (Python) — goal hierarchy + self-grading
- `corlinman-voice` (Python) — voice provider abstraction (whisper / openai-realtime / etc.)

### Extended packages
- `corlinman-evolution-engine` — meta kinds + recursion guard
- `corlinman-vector` — episode index alongside chunk index
- `corlinman-user-model` — multi-channel resolver
- `corlinman-gateway` — TenantId middleware, MCP routes, voice routes
- `corlinman-channels` — push-notification adapter for native clients

### New schemas
- `tenants.sqlite` (admin DB, single root): tenant rows + admin claims
- Per-tenant DBs renamed: `tenants/<tenant_id>/{kb,evolution,sessions,user_model,agent_state,episodes,agent_goals}.sqlite`
- `user_identity.sqlite` (per-tenant): canonical `user_id` + `(channel, sender_id)` mappings + verification proofs

### Configuration
```toml
[tenants]
enabled = true
default = "default"  # legacy single-tenant compat

[evolution.shadow.sandbox]
kind = "docker"   # 'in_process' | 'docker'
image = "ghcr.io/ymylive/corlinman-sandbox:v1"
network = "none"
mem_mb = 512
timeout_secs = 60

[evolution.meta]
enabled = true
recursion_guard = "one_level"
require_double_confirm = ["engine_prompt"]

[user_identity]
verification_required = true
verification_phrase_ttl_secs = 600

[mcp.server]
enabled = true
bind = "127.0.0.1:18791"

[mcp.client]
enabled = true

[canvas]
host_endpoint_enabled = true   # Phase 4 flips Phase 1's default off → on
renderer_kind = "in_process"   # 'in_process' | 'subprocess'

[voice]
enabled = false   # cost gate; opt-in

[episodes]
enabled = true
schedule = "0 6 * * * *"   # daily 06:00 UTC distill from sessions

[goals]
enabled = true
schedule = "0 7 * * * *"   # 07:00 UTC: grade + refresh after episode distill
```

---

## 6. Risk Matrix

| Risk | Likelihood | Mitigation |
|---|---|---|
| Multi-tenant data leakage via shared in-memory cache | High | All caches keyed by `(tenant_id, ...)`; static analysis audit at end of W1 |
| Recursive self-improvement loop diverges (engine prompt regression amplifies) | Medium-High | Strict one-level guard + double-confirm UI + AutoRollback monitors meta proposals on tighter window (24h) |
| Cross-channel verification phrase phishing (operator tricked into pairing wrong account) | Medium | Mutual phrase exchange in both channels; 10min TTL; admin-UI shows pending pairings before confirm |
| Docker sandbox misconfigures and runs evals against host | Critical | Image is read-only + `network=none` + non-root + drop-all caps + tested via security review before W1-D enables prompt kind |
| MCP exposes corlinman tools to untrusted Claude Desktop on shared machine | High | MCP server bound to loopback only; auth token required; documented "do not expose to network without TLS+auth" |
| Canvas Host XSS from agent-rendered HTML | High | sanitize-html / DOMPurify in renderer; CSP locked-down sandbox iframe |
| Episode distillation embeds PII into long-term memory | Medium | Same redaction pipeline as user_model; episodes' content goes through redactor before write |
| Subagent delegation runaway (parent → spawns 100 children) | Medium | Per-session subagent budget; default cap 5; explicit operator override |

---

## 7. Open Questions (decision before W1)

1. **Tenant id format.** UUID? Slug like `acme-corp`? Lean: **slug** — operators read these in URLs and configs; uniqueness checked at create-time.
2. **Cross-tenant skill sharing default.** Off by default per skill, or off by default per tenant? Lean: **off by default per tenant**; operator opt-in toggles each skill independently.
3. **Meta-proposal naming.** "meta" feels jargony. Alternatives: "self" / "system" / "internal". Lean: **system_** prefix (`system_prompt`, `system_threshold`, etc.).
4. **MCP auth model.** Static token in config / per-client OAuth / mTLS? Lean: **static token in config** for v1; mTLS in Phase 5 if we go enterprise.
5. **Voice cost gating.** Hard cap per day? Per-user budget? Lean: **per-tenant daily $-cap** in config; over-budget falls back to text gracefully.
6. **Episode granularity.** One episode per session? Per topic shift? Per N hours? Lean: **per session** for v1; topic-shift detection is a Phase 5 refinement.
7. **Goal source of truth.** Operator-set / agent-self-proposed / hybrid? Lean: **hybrid** — operator sets long-term ("become competent at infrastructure topics"); agent decomposes into mid/short.
8. **Subagent persona inheritance.** Inherit parent? Fresh from agent-card? Custom? Lean: **fresh from agent-card** — child gets a clean mood/fatigue start; parent's persona unaffected by children.

---

## 8. Success Criteria for Phase 4 Exit

After 12-16 weeks:

- ✅ 3+ tenants running on a single deployment with verified data isolation
- ✅ All 8 EvolutionKinds (Phase 3) PLUS the 4 meta kinds active and producing approved proposals weekly
- ✅ ≥ 1 `engine_prompt` meta-proposal applied + observed to improve proposal quality (signal: more proposals reach approved/applied vs denied within a 14-day window)
- ✅ MCP server reachable from Claude Desktop with at least 5 tools + memory query exposed
- ✅ Reference Swift client demonstrates round-trip text + push notification
- ✅ Episode count across all tenants ≥ 100; agent can answer "what happened last week" in natural language with citations
- ✅ Subagent fan-out demonstrably faster on a parallel-decomposable benchmark task
- ✅ Voice opt-in surface works for at least one provider with cost gate enforced

If any miss → stabilize, do not bump to Phase 5.

---

## 9. Anti-Goals (Phase 4 will NOT)

- Touch model weights / fine-tuning. Inference-only evolution stays.
- Auto-generate code in `rust/` or `python/` source.
- Replace operator approval with full automation for any kind.
- Have agent self-modify the EvolutionEngine's recursion guard. The
  guard is human-immutable.
- Ship a "DreamSystem" feature — same anti-goal as Phase 3.
- Run cross-tenant federation without explicit operator opt-in per
  proposal.
- Expose MCP server to non-loopback network without TLS + auth (deferred
  to a separate enterprise phase).
- Implement the actual native iOS / Android apps. Phase 4 ships
  contracts + one Swift mac reference; client teams pick it up.
- Implement enterprise SSO (OIDC / SAML). That blocks public deployment
  but isn't in this phase.

---

## 10. Phase 5 Preview (out of scope)

After Phase 4, if the platform proves itself in operator hands:

- **Public-internet-facing multi-tenant deployment**: TLS-fronted, OIDC
  + SAML auth, per-tenant rate limiting, billing hooks
- **Federated agent training data**: opt-in trajectory collection feeds
  back into model fine-tuning (out-of-process — corlinman emits, doesn't
  train)
- **Browser extension** that surfaces corlinman as a Claude.ai tool
- **Cross-deployment learning** — one operator's lessons benefit
  another's, mediated by signed proposal exchanges
- **Self-curriculum** — agent picks what to learn next based on its
  goal hierarchy + observed weaknesses
- **Real-time collaborative editing** in Canvas Host (multiple operators
  + agent on the same document)
- **Topic-shift detection for episode boundaries** (instead of per-
  session granularity)
- **Continuous voice presence** (always-on listening mode with explicit
  consent + indicator)

These are explicitly *not* Phase 4. Phase 4's job is to make Phase 5
even thinkable.
