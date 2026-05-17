# Changelog

All notable changes to corlinman are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-05-17 — multi-agent

Headline: parallel sibling agents, a shared trace-scoped blackboard,
a deterministic Pareto scorer for prompt-template variants, and
BuildKit cache mounts that drop incremental Docker rebuilds from
~12 min to ~90 s. Inspired by Nous Research's
[hermes-agent](https://github.com/NousResearch/hermes-agent) (true
multi-agent + GEPA prompt evolution) and
[openclaw](https://github.com/openclaw/openclaw) (pre-warmed pool
pattern). Full notes:
[`docs/release-notes-v0.7.0.md`](docs/release-notes-v0.7.0.md).

### Added

- **`subagent.spawn_many`** tool. Dispatches up to 3 sibling children
  concurrently under one parent context via `asyncio.gather`. The
  supervisor's existing per-parent concurrency cap (default 3)
  still governs live siblings; fan-outs exceeding the cap reject
  up-front with a clean args-invalid envelope.
- **Shared blackboard** (`blackboard.read` / `blackboard.write`).
  Trace-scoped, append-only sqlite scratchpad for sibling agents to
  coordinate. Writes never overwrite; reads return the latest value at
  call time; trace isolation is the security boundary.
- **`agents/orchestrator.yaml`**: new planner persona that
  decomposes → dispatches → reduces.
- **GEPA-lite Pareto scorer** (`corlinman_evolution_engine.score_variants`).
  Deterministic, no LLM-judge, no DSPy dependency — token Jaccard
  against the episodes that already succeeded.
- **Builtin-tool interception** in the agent servicer routes the four
  new tools in-process rather than through the Rust plugin registry.
- **BuildKit cache mounts** on the rust-builder + py-builder stages
  for cargo registry / git / target and uv wheel cache.

### Deferred to v0.7.1

- Pre-warmed Python agent runner pool (OpenClaw-style). Designed in
  [`docs/multi-agent-release-plan.md`](docs/multi-agent-release-plan.md) §2.3.

## [Unreleased] — targets v0.5.0

Free-form named providers + 7 new market `kind`s, **plus a BREAKING swap
from `sub2api` to `newapi`** as the channel-pool sidecar. Full notes:
[`docs/release-notes-v0.5.0.md`](docs/release-notes-v0.5.0.md).

### Removed (BREAKING)

- **`ProviderKind::Sub2api` removed.** The `kind = "sub2api"` provider entry
  is no longer recognised. Replace with `kind = "newapi"` pointing at a
  [QuantumNous/new-api](https://github.com/QuantumNous/new-api) instance.
  Run `corlinman config migrate-sub2api --apply` to rewrite legacy entries
  automatically. See [`docs/migration/sub2api-to-newapi.md`](docs/migration/sub2api-to-newapi.md).

### Added

- **`ProviderKind::Newapi`** + new-api admin client crate
  (`corlinman-newapi-client`). MIT-licensed sidecar that pools channels
  (LLM / embedding / audio TTS) behind one OpenAI-wire endpoint. Replaces
  the LGPL-3.0 sub2api integration.
- **4-step interactive onboard wizard** (account → newapi connect →
  pick defaults → confirm). The gateway calls new-api's `/api/channel`
  to populate model dropdowns; the operator only types the URL + token
  once.
- **`/admin/newapi` connector page** with live channel health, usage
  quota, token TTL, and a 1-token round-trip test button.
- **`corlinman config migrate-sub2api [--dry-run|--apply]`** CLI
  subcommand that rewrites legacy `kind = "sub2api"` entries to
  `kind = "newapi"` in place (with backup).
- **Full i18n coverage (zh-CN + en)** for the new onboard wizard and
  admin newapi page.
- **Free-form `[providers.*]` configuration**: the providers section is
  now a `BTreeMap<String, ProviderEntry>` keyed by an operator-chosen
  name. Add OpenRouter, SiliconFlow, Ollama, vLLM, or any other
  OpenAI-wire-compatible vendor by writing two TOML lines — no Rust
  patch required. The six legacy slot names (`anthropic`, `openai`,
  `google`, `deepseek`, `qwen`, `glm`) continue to infer their `kind`
  for backwards compatibility.
- **Seven new `ProviderKind` variants**: `mistral`, `cohere`,
  `together`, `groq`, `replicate`, `bedrock`, `azure`. The first five
  route through the shared `OpenAICompatibleProvider` Python adapter
  with documented default base URLs; `bedrock` and `azure` are
  declared but raise `NotImplementedError` at build time pending real
  SigV4 / deployment-routing support.
- **Validator**: free-form names without an explicit `kind` produce a
  `missing_kind` error pointing at the offending entry, listing every
  valid kind in the message.

### Docs

- New: [`docs/providers.md`](docs/providers.md) — provider model + 14
  supported `kind`s + four end-to-end recipes (OpenRouter + OpenAI
  embedding, fully-local Ollama, CN-resident SiliconFlow, Groq
  alongside OpenAI).
- Updated: [`docs/config.example.toml`](docs/config.example.toml) leads
  with `[providers.openai]` plus six commented-out vendor recipes; adds
  named-provider `[embedding]` and full-form `[models.aliases.*]`
  examples.
- Updated: [`docs/architecture.md`](docs/architecture.md) §7 inline
  sample reflects the free-form shape; reading list links the new
  providers reference.
- Updated: [`README.md`](README.md) Configuration section shows the
  new `kind = "..."` shape; documentation map links the new doc.

### Migration notes

- No data migration. Existing configs with first-party slot names
  parse unchanged.
- New entries MUST set `kind` explicitly; `corlinman config validate`
  surfaces any missing `kind` field with a one-line fix hint.
- `bedrock` and `azure` parse and validate but raise at adapter-build
  time today — declare `kind = "openai_compatible"` against a
  compatible proxy until the real adapters ship.

## [0.4.0] — 2026-04-23

Admin UI redesign: **Tidepool** design system. Warm-amber glass
aesthetic, day+night themes, and a reusable primitive library power a
from-scratch re-skin of all 15 admin pages. Backend and API unchanged —
this is a pure frontend release.

### Added

- **Design tokens** (`ui/app/globals.css`): `--tp-*` namespace for
  amber / ember / peach accents, ink ramp, glass layers, edge colours,
  gradients, shadows, and row alternation. Day and night palettes share
  every variable name; `data-theme="light|dark"` (mirrored to the
  `.dark` class for Tailwind compatibility) selects the active set.
- **12 new UI primitives** (`ui/components/ui/`):
  `<GlassPanel>` (soft/strong/subtle/primary variants respecting the
  ≤5 blur-layer/viewport budget), `<AuroraBackground>`,
  `<ThemeToggle>` (sun/moon pill with no-FOUC boot script),
  `<MiniSparkline>`, `<StreamPill>`, `<FilterChipGroup>`,
  `<StatChip>` (tick-up animation + ambient sparkline),
  `<JsonView>` (syntax-highlighted), `<LogRow>`, `<DetailDrawer>`,
  `<CommandPalette>` (configurable via `PaletteGroup[]`), plus
  `<UptimeStreak>`.
- **Motion tokens** (`ui/lib/motion.ts`): `tickUp` and `paletteIn`
  framer-motion variants alongside existing `fadeUp` / `stagger` /
  `springPop`. Continuous ambient animations (breathing, draw-in,
  just-now fades, badge pulses) live as CSS keyframes under `.tp-*`
  utility classes — cheaper than per-frame React work.
- **Typography**: Instrument Serif (display) loaded via `next/font`
  as `var(--font-instrument-serif)`, paired with existing Geist sans
  and Geist mono.
- **Theme persistence**: shared `corlinman-theme` storage key between
  `next-themes` and the inline boot script in `app/layout.tsx`.
  Hydration is race-free because the boot script writes
  `data-theme` + `.dark` before React mounts.
- **UI docs**: new "Tidepool design system" section in `ui/README.md`
  documenting tokens, primitive APIs, motion patterns, performance
  budget, and a new-page quick-start.

### Changed

- **All 15 admin pages retokened** onto Tidepool: Dashboard, Logs,
  Plugins, Approvals, Skills, Characters, Hooks, Scheduler, Nodes,
  Playground, Canvas, Tag Memo, Diary, Channels (QQ + Telegram),
  Config, Login, Models, Providers, Embedding, RAG, Agents. Direct
  colour/background classes replaced with `tp-*` tokens, `<Card>`
  uses swapped for `<GlassPanel>` where the glass treatment applies.
- **Admin layout** (`app/(admin)/layout.tsx`): `<AuroraBackground>`
  mounted once behind the sidebar + main grid; container spacing
  normalised to `gap-4 p-4`.
- **Command palette** (`components/cmdk-palette.tsx`): inner
  rendering delegated to the new `<CommandPalette>` primitive via a
  declarative `PaletteGroup[]` config. `useCommandPalette` hook,
  `CommandPaletteProvider`, `NAV_CMDS` registry, recent-routes, and
  test-chat drawer preserved.
- **i18n**: pages that gained Tidepool prose (hero copy, empty
  states, filter chips) now partition their new keys under a
  `<page>.tp.*` sub-namespace to keep diffs legible.

### Fixed

- **WCAG AA contrast**: darkened day-mode `--primary` to amber-800
  (`hsl(20 82% 33%)`) after `<Button>` primary text failed 4.5:1
  against foreground on the warm base. Night mode uses amber-400
  (`hsl(35 90% 65%)`) on dark ink.
- **Aurora visibility**: removed `bg-background` from `<body>` in
  `app/layout.tsx`; the admin layout now owns the backdrop, while
  the login route re-adds `bg-background` on its own root.
- **Offline-state HTML dumps**: plugins and scheduler pages detected
  backend HTML error responses (rather than JSON) and rendered the
  raw markup; `OfflineBlock` now suppresses dumps whose first line
  starts with `<`.
- **Telegram page `<dl>` a11y**: nested `<FilterStatCell>` broke
  definition-list semantics. Converted the wrapper to
  `<div>/<div>/<div>` so axe passes.

### Performance

- Dashboard blur-layer count dropped from 7 → 4 per viewport by
  defaulting non-primary `<StatChip>` instances to `<GlassPanel
  variant="subtle">` (tp-glass-inner, no `backdrop-filter`). Primary
  chip retains the full glass treatment to anchor the eye.
- All continuous animations (breathing dots, draw-in underlines,
  badge pulses, just-now fades) run as CSS keyframes gated by
  `@media (prefers-reduced-motion: reduce)`.

### Migration notes

- No backend changes. Existing deployments can upgrade by pulling the
  new `ui-static/` bundle only.
- Custom pages that used raw `bg-card` / `text-muted-foreground`
  continue to render — Tidepool tokens compose alongside legacy
  shadcn tokens rather than replacing them.
- Users with persisted theme preferences from the previous
  `next-themes` default key will see a one-time flip to dark on
  first visit; the new `corlinman-theme` key is then used
  consistently.

[0.4.0]: https://github.com/ymylive/corlinman/releases/tag/v0.4.0

## [0.3.0] — 2026-04-23

Sprint 9 (Batch 1–4) rollup: hierarchical tags + EPA cache in the
vector store, manifest v2, reserved placeholder namespaces, and
dual-track tool-call protocol. All additions are backwards-compatible.
Upgrade guide: [`docs/migration/v1-to-v2.md`](docs/migration/v1-to-v2.md).

### Added

- **Manifest v2** (`corlinman-plugins`): new `manifest_version`,
  `protocols`, `hooks`, `skill_refs` fields. Absent `manifest_version`
  is treated as v1 and auto-migrates to v2 in memory with default
  protocols `["openai_function"]`. Unknown `protocols` values are
  rejected at load; unknown `hooks` names warn but don't fail.
- **Vector schema v6** (`corlinman-vector`): new `tag_nodes`
  (hierarchical tag tree: `id / parent_id / name / path / depth`) and
  `chunk_epa` (per-chunk EPA projection cache). `chunk_tags` retargets
  its FK to `tag_nodes.id`; flat v5 tags materialise as depth-0 nodes
  so legacy queries keep working. Migration is idempotent and runs
  in-transaction on first open.
- **Config sections**: `[hooks]`, `[skills]`, `[variables]`,
  `[agents]`, `[tools.block]`, `[telegram.webhook]`, `[vector.tags]`,
  `[wstool]`, `[canvas]`, `[nodebridge]`. All `#[serde(default)]` —
  existing `config.toml` loads unchanged.
- **Placeholder namespaces**: reserved `var / sar / tar / agent /
  session / tool / vector / skill`. Cycle detection, async resolution,
  `{{角色}}` agent-card expansion with single-agent-gate semantics.
- **On-disk authoring surfaces**: `skills/*.md` (openclaw-style YAML
  frontmatter + Markdown), `agents/*.yaml` (character cards),
  `TVStxt/{tar,var,sar,fixed}/*.txt` (four-tier cascade variables).
  Sample files ship in-repo.
- **New Rust crates**: `corlinman-hooks` (in-process hook bus),
  `corlinman-skills` (openclaw skill loader + system-prompt injector),
  `corlinman-wstool` (local WebSocket tool bus), `corlinman-nodebridge`
  (Node.js worker bridge listener).
- **New Python package**: `corlinman-tagmemo` (EPA basis fitting +
  pyramid build; feeds `chunk_epa` cache).
- **Admin UI pages**: `/skills`, `/characters`, `/hooks`,
  `/playground/protocol`, `/channels/telegram`, `/nodes`, plus
  tagmemo / diary / canvas surfaces.
- **Dual-track tool invocation**: agents may emit tool calls as
  `<<<[TOOL_REQUEST]>>>` structured blocks (with `「始」…「末」`
  value fencing) in addition to OpenAI function-call JSON. Opt in per
  agent via manifest `protocols = ["block"]` + `[tools.block].enabled
  = true`. Legacy plugins remain reachable via
  `fallback_to_function_call = true`.

### Migration notes

- Legacy v1 plugin manifests parse unchanged.
- v5 vector DBs migrate forward on first open; there is no shipped
  down-path — rollback is "restore the pre-upgrade data-dir backup".
- Existing `config.toml` needs no edits.

[0.3.0]: https://github.com/ymylive/corlinman/releases/tag/v0.3.0

## [0.2.0] — 2026-04-21

Major release. Dynamic provider registry, per-alias model params,
first-class embedding config, and admin UI to manage all of it.
Full notes: [`docs/release-notes-v0.2.0.md`](docs/release-notes-v0.2.0.md).

### Added

- **Config**: `[providers.<name>].kind` enum + `params` map;
  `[models.aliases.<name>].params`; new `[embedding]` section.
  Backward-compatible — configs without `kind` on first-party
  providers still parse via inferred-kind defaults.
- **Rust admin routes**: `/admin/providers` (CRUD + 409 reference
  guard); `/admin/embedding` (GET/POST, benchmark stubbed to 501);
  `/admin/models/aliases` extended with single-row upsert + delete.
- **Python**: dynamic `ProviderRegistry` driven by `[providers.*]`
  specs; `params_schema()` on every provider; new
  `CorlinmanEmbeddingProvider` ABC with OpenAI-compatible + Google
  implementations; `benchmark_embedding()` helper (p50/p99 latency +
  cosine matrix).
- **UI**: `/providers` + `/embedding` pages, `/models` inline-accordion
  for params, hand-rolled `<DynamicParamsForm>` JSON-Schema renderer,
  ~145 new i18n keys across zh-CN + en.

### Fixed

- `/admin/approvals` returned 503 in production because `ApprovalGate`
  was never constructed at boot. `build_runtime_with_logs` now wires
  it from the live config handle + the RAG SQLite.

### Changed

- Docker image drops the `ui-builder` stage. Production serves the
  Next.js static export via nginx from `/opt/corlinman/ui-static/`;
  bundling it was dead weight and segfaulted node under Rosetta 2
  cross-builds.

### Known issues

- `/admin/embedding/benchmark` is a 501 stub until the Python helper
  is reachable over gRPC from Rust. UI handles the fallback.
- Rust gateway doesn't yet export `CORLINMAN_PY_CONFIG` to the Python
  subprocess; the legacy prefix-matching path keeps chats working
  while the config-driven registry integration lands.

[0.2.0]: https://github.com/ymylive/corlinman/releases/tag/v0.2.0

## [0.1.3] — 2026-04-21

zh-CN / en internationalisation + static-bundle API fix. Pure frontend
release — no Rust, Python, or Dockerfile changes.

### Added

- Full zh-CN / en i18n across every admin page, layout, login, dashboard,
  and `⌘K` palette. `react-i18next` + two TypeScript locale bundles
  (378 keys each, compile-time parity enforced).
- Language toggle in the topnav + command-palette action. Choice persists
  in `localStorage`; first-visit detection falls back to
  `navigator.language` (`zh*` → Chinese, else English).
- Inline pre-hydration boot script sets `<html lang>` so language
  selection applies before React mounts (no FOUC).

### Fixed

- **`GATEWAY_BASE_URL` default**: changed from `"http://localhost:6005"`
  to `""`. The static export used to bake localhost into the visitor's
  bundle, making every `/admin`, `/health`, `/v1` call from a deployed
  origin fail with `ERR_CONNECTION_REFUSED`. Relative URLs now resolve
  through the current origin, which nginx already reverse-proxies to
  the gateway. `NEXT_PUBLIC_GATEWAY_URL` remains the local-dev
  override; mock-server paths untouched.

### Dependencies

- Added: `i18next`, `react-i18next`, `i18next-browser-languagedetector`.

[0.1.3]: https://github.com/ymylive/corlinman/releases/tag/v0.1.3

## [0.1.2] — 2026-04-21

Admin UI redesign. Pure frontend release — no Rust, Python, or
Dockerfile changes.

### Changed

- **Admin UI fully redesigned in a Linear / Vercel aesthetic**: dark-first
  with a single indigo accent, Geist Sans / Mono typography, borders-over-shadows,
  compact 6–8 px radii. `next-themes` light/dark toggle preserved.
- **New dashboard landing page** (`/`): four stat cards with inline
  sparklines, SSE-driven recent-activity feed, and a 7-check system health
  panel backed by `/health`.
- **Sidebar + topnav**: 240 ↔ 56 px collapsible sidebar with an animated
  active-indicator (framer-motion `layoutId`); topnav adds auto
  breadcrumb, live health dot, theme toggle, and a `⌘K` search pill.
- **Global command palette** (`cmdk`): fuzzy navigation over all
  destinations, a test-chat drawer that POSTs to `/v1/chat/completions`,
  plus theme-toggle and logout actions. Recent commands persist in
  `localStorage`.
- **Motion language**: 200 ms page-transition fades, skeleton shimmers,
  `sonner` toasts, slide-up issues drawer on the config page. No bouncy
  spring animations.
- **Refined pages**: Plugins, Agents, RAG, Channels, Scheduler, Approvals,
  Models, Config, Logs — consistent status dots, inline-edit affordances,
  virtualised logs list with pause-stream toggle, live scheduler countdowns.
- **New login page**: two-column layout with a constellation backdrop
  SVG and inline error with shake micro-animation.

### Added

- `framer-motion`, `cmdk`, `geist`, `sonner` as UI dependencies.
- `fetchHealth()` + `HealthStatus` type in `ui/lib/api.ts`.

### Stability

- Playwright E2E selectors audited and preserved.
- Vitest suite (including Chinese login-form labels) still green.
- No API contracts changed.

[0.1.2]: https://github.com/ymylive/corlinman/releases/tag/v0.1.2

## [0.1.1] — 2026-04-21

Deployment hotfix. Surfaced the first time the 1.0 image was built
against a real server. All changes are docker / runtime fixes — no
code behaviour changes outside the boot path.

### Fixed

- **`docker/Dockerfile`**: drop stale `pnpm -C ui export` step —
  Next.js 14 removed the `next export` command; `output: "export"` in
  `ui/next.config.ts` already emits the static bundle during
  `next build`.
- **`docker/Dockerfile`**: bump rust base from `1.85-slim` to
  `1.95-slim` to match the project's `rust-toolchain.toml`.
  `cargo-chef 0.1.77` transitively raised its MSRV to `rustc 1.88`.
- **`docker/Dockerfile`**: add `binutils` + `g++` to the rust-builder
  apt layer (required by `link-cplusplus`) and force the BFD linker via
  `RUSTFLAGS=-C link-arg=-fuse-ld=bfd`. `lld` SIGSEGVs under Rosetta 2
  / QEMU user-mode emulation when cross-building amd64 images from
  Apple Silicon hosts.
- **`docker/Dockerfile`**: correct runtime `COPY` of the CLI binary —
  cargo emits `/build/target/release/corlinman` (per `[[bin]] name`),
  not `corlinman-cli`.
- **`rust/crates/corlinman-gateway/src/main.rs`**: honour `BIND` env
  var (default `127.0.0.1`, containerised deploys set `0.0.0.0`).
  Previously the listener was hard-bound to `127.0.0.1` and docker
  port-publishing never reached it.
- **`docker/Dockerfile`**: carry the python source tree into the
  runtime image. `uv sync --no-editable` ignores workspace members, so
  venv `.pth` shims pointed at `/build/python/packages/*/src/` which
  don't exist in runtime — `corlinman-python-server` died at
  `ModuleNotFoundError`. Adding `COPY --from=py-builder /build/python
  /build/python` resolves the editable paths.

### Added

- **Runtime env knobs**: `BIND` (listener address) and `OPENAI_BASE_URL`
  (consumed by `AsyncOpenAI` when `[providers.openai].base_url` isn't
  threaded through — see Known Issues).

### Known issues carried over

- `corlinman_providers.registry.resolve()` still ignores `[providers.*]`
  settings from `config.toml`. Until a deeper fix lands, point non-default
  OpenAI-compatible backends at the right host via `OPENAI_BASE_URL`.
- Docker image does not supervise the python agent out of the box;
  production deploys use a startup script (`docker/start.sh` pattern)
  that spawns `corlinman-python-server` alongside `corlinman-gateway`.

[0.1.1]: https://github.com/ymylive/corlinman/releases/tag/v0.1.1

## [0.1.0] — 2026-04-21

First tagged release. The 1.0 release prep sprint (S8) wraps seven prior
implementation sprints (M0–M7) into a shippable self-hosted intelligent
agent platform.

### Added

- **Core gateway** (`rust/crates/corlinman-gateway`): OpenAI-compatible
  `/v1/chat/completions` (stream + non-stream), `/v1/embeddings`,
  `/v1/models`, WebSocket admin endpoints, and the full admin REST surface
  (`/admin/plugins`, `/admin/rag/*`, `/admin/approvals`, `/admin/scheduler/*`,
  `/admin/config`, `/admin/logs/stream`, `/admin/health/metrics`). Session
  history persisted to `~/.corlinman/sessions.sqlite` with a configurable
  trim cap.
- **Python agent plane** (`python/packages/corlinman-server`,
  `corlinman-agent`, `corlinman-providers`): gRPC `Agent.Chat` reasoning
  loop with streaming token deltas, tool-call loop, and providers for
  Anthropic, OpenAI, Google, DeepSeek, Qwen, and GLM.
- **Plugin runtime** (`rust/crates/corlinman-plugins`): three plugin
  types (sync / async / service) over JSON-RPC 2.0 stdio or gRPC.
  Includes manifest parser, `plugin-manifest.toml` validation, async
  task callback registry (`/plugin-callback/:task_id`), approval gate
  for human-in-the-loop tool execution, hot reload of the plugin
  registry, and a Docker sandbox runner for untrusted plugins.
- **RAG** (`rust/crates/corlinman-vector`): SQLite + FTS5 BM25,
  usearch HNSW dense recall, reciprocal-rank fusion, optional
  gRPC-backed cross-encoder rerank, tag-filter pushdown, LRU unload,
  and multi-step schema migrations (v1 → v4).
- **Channels** (`rust/crates/corlinman-channels`): QQ (go-cqhttp /
  OneBot v11) and Telegram adapters with rate limiting, multimodal
  uploads, user-to-session binding.
- **Observability** (M7): W3C `traceparent` propagation, OpenTelemetry
  OTLP exporter, three-tier Prometheus metrics (gateway / plugin /
  provider), `/health` probes driven by real component state, `corlinman
  doctor` with 20+ diagnostic checks (config / agent gRPC ping / SQLite
  / usearch / plugin registry / docker / disk / memory / log rotation /
  provider HTTPS smoke / manifest duplicates / broken symlinks /
  pending-approvals overflow / python subprocess health / …).
- **Admin UI** (`ui/`): Next.js 15 + React 19 dashboard for plugins,
  RAG, approvals, scheduler, config, logs, and health metrics.
  Playwright e2e coverage.
- **CLI** (`rust/crates/corlinman-cli`): `corlinman onboard`,
  `corlinman doctor`, `corlinman plugins`, `corlinman config`,
  `corlinman dev`, `corlinman vector`, and — new in this release —
  `corlinman qa run` + `corlinman qa bench`.

### Docs

- `docs/roadmap.md` — canonical sprint plan (through M8 and beyond).
- `docs/architecture.md`, `docs/plugin-authoring.md`, `docs/runbook.md`.
- `docs/perf-baseline-1.0.md` — p50 / p99 numbers for chat, RAG, and
  plugin exec roundtrips. Used by CI to detect ≥20 % regressions.
- `qa/scenarios/*.yaml` — 8 executable scenarios covering chat
  stream + non-stream, tool-call loop, plugin sync + async, RAG hybrid
  retrieval, OneBot echo, and a marked-live fresh-install walkthrough.

### Known gaps (deferred to 0.1.1)

- **No prebuilt docker image yet.** Build from source with `cargo build
  --release -p corlinman-gateway -p corlinman-cli`; the `ghcr.io/ymylive/corlinman:0.1.0`
  image is pending a v0.1.1 follow-up once a build host with docker is
  available.
- **Screenshot placeholder**: `README.md` references
  `docs/assets/dashboard.png`; the actual PNG will be added with the
  installation walkthrough screencast.
- **`fresh-install` QA scenario** is marked `requires_live: true` — it's
  exercised by the S8 T4 screencast rather than the offline CI runner.
- **1.0 release comms** (blog / Zhihu / Hacker News / r/selfhosted /
  r/LocalLLaMA) are a separate content-production task, not part of
  this release artefact.

### Reference

Commit history on the `main` branch:

- `sprint-1` through `sprint-3`: M1 / M2 / M3 / M4 scope
- `sprint-4` (M5 channels), `sprint-5` (M6 auth + logs + approvals),
  `sprint-6` (M6 admin UI + Playwright)
- `sprint-7` (M7 observability)
- `sprint-8` (this release — M8 1.0 prep)

[0.1.0]: https://github.com/ymylive/corlinman/releases/tag/v0.1.0
