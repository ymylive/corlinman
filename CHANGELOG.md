# Changelog

All notable changes to corlinman are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-04-21

First tagged release. The 1.0 release prep sprint (S8) wraps seven prior
implementation sprints (M0–M7) into a shippable self-hosted LLM toolbox.

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
  --release -p corlinman-gateway -p corlinman-cli`; the `ghcr.io/ymylive/corlinma:0.1.0`
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
