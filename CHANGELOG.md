# Changelog

All notable changes to corlinman are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/spec/v2.0.0.html).

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
