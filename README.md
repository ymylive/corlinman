# corlinman

[![CI](https://img.shields.io/github/actions/workflow/status/ymylive/corlinman/ci.yml?branch=main&label=CI)](https://github.com/ymylive/corlinman/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.4.0-orange)](CHANGELOG.md)
[![Docs](https://img.shields.io/badge/docs-architecture-informational)](docs/architecture.md)

**A self-hosted intelligent-agent platform.** Give a language model durable
memory, real tools, multiple chat channels, and an operations plane — all
in one binary you can run on your own hardware, audit end-to-end, and
govern with human-in-the-loop approvals.

![corlinman — 60-second product tour: five pillars, multi-provider agent loop, sandboxed plugins, human-in-the-loop approvals, hybrid RAG memory, first-class channels, Tidepool admin day & night, and a one-second doctor check](docs/assets/tidepool-hero.gif)

> _Live deployment reference: <https://corlinman.cornna.xyz>._
> _中文介绍章节见文末 ["中文速览"](#中文速览)。_

---

## Why corlinman

Most LLM infrastructure today is either a **thin API wrapper** (you send
prompts, you read tokens, you integrate nothing) or a **workflow toolbox**
(drag and drop nodes, marketplace plugins, zero opinion on how they compose).

corlinman takes a third stance: **the agent is the product.** The reasoning
loop, the tools it calls, the memory it retains across turns, the channels
it hears from, and the operator surface that governs it — all live in one
coherent system that is opinionated about correctness, observability, and
safety.

What you get out of the box:

- **One agent loop, many providers.** OpenAI tool-call semantics on top of
  Anthropic, OpenAI, Google, DeepSeek, Qwen, or GLM — with per-model aliases
  and hot-swap without restart.
- **Tools are real plugins, not prompt templates.** Sync, async, and
  long-lived "service" tools over JSON-RPC 2.0 stdio or gRPC, with optional
  Docker sandboxing for untrusted code and a human-approval gate for
  dangerous actions.
- **Memory that survives conversations.** Per-session message history in
  SQLite; a hybrid-retrieval knowledge base (HNSW + BM25 + RRF fusion) with
  optional cross-encoder rerank for agent-grade RAG, not a glorified grep.
- **Channels are first-class agent I/O.** Production adapters for QQ
  (OneBot v11) and Telegram, a scheduler for cron-driven tasks, an
  OpenAI-compatible HTTP/SSE endpoint for your own clients.
- **An admin plane that treats operations seriously.** A warm-amber
  glass web console (**Tidepool** design system, day + night themes) for
  plugin management, RAG inspection, live log streaming, approval
  queues, config live-reload, and model routing — plus OTel traces,
  Prometheus metrics, and a 20+ check `doctor` command.

If you want something you can hand your teammate a URL to, then audit on
Sunday morning without reverse-engineering twenty repos — that's corlinman.

---

## Architecture at a glance

```
                      ┌────────────────────────────────────┐
   HTTP + SSE ──────▶ │        corlinman-gateway           │ ◀─── Next.js admin UI
   (clients, UI,      │   Rust · axum · tonic · listens    │     (static export,
    channels)         │   on :6005; routes /v1, /admin,    │      served by nginx)
                      │   /health, /metrics, /plugin-cb)   │
                      └──┬──────────┬──────────┬──────────┘
                         │          │          │
              gRPC bidi  │  gRPC    │  gRPC    │ JSON-RPC / gRPC
                         ▼          ▼          ▼
                    ┌────────┐  ┌────┐    ┌───────────┐
                    │ agent  │  │emb │    │ plugin    │
                    │ Python │  │(py)│    │ runtimes  │
                    │ loop   │  └────┘    │ (py / node/
                    │ + LLM  │            │  rust /   │
                    │ SDKs   │            │  bash +   │
                    └───┬────┘            │  docker)  │
                        │                 └───────────┘
                        ▼
              ┌──────────────────────┐
              │ upstream providers    │
              │ Anthropic · OpenAI ·  │
              │ Google · DeepSeek ·   │
              │ Qwen · GLM · custom   │
              └──────────────────────┘

   Side-bus:
     • corlinman-channels ── QQ / OneBot v11 · Telegram ──▶ internal ChatRequest
     • corlinman-scheduler ── tokio-cron-scheduler ──────▶ gateway AppState
     • corlinman-vector ──── HNSW + SQLite FTS5 ─────────▶ /admin/rag + agent memory
```

Two runtimes, one bus. **Rust owns the network:** axum gateway, tonic gRPC,
Docker integration via `bollard`, `notify`-driven hot reload, usearch FFI,
structured logging, signal-correct shutdown. **Python owns the LLM craft:**
the provider SDK ecosystem (anthropic, openai, google-genai, etc.), the
reasoning loop, and fast prompt iteration. The two sides talk exclusively
over a strongly-typed gRPC bus with W3C `traceparent` propagation, so a
single request has one span tree end to end.

Deep dive: [`docs/architecture.md`](docs/architecture.md).

---

## Quickstart

### Fastest: one-line installer (recommended)

Two install paths, same `install.sh`, same onboarding wizard at the end.
As of **v0.6.7** the prebuilt Linux x86_64 binary is built inside
`manylinux_2_28` (glibc 2.28 baseline), so the **native** mode works on
every currently-supported mainstream Linux distro — Docker is no longer
the only portable option.

| Path | One-liner | Where it runs | When to pick it |
| --- | --- | --- | --- |
| **Pre-built binary (`--mode native`)** | `curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh \| bash -s -- --mode native` | Debian 11+, Ubuntu 20.04+, RHEL/AlmaLinux/Rocky 8+ (glibc ≥ 2.28); macOS aarch64. Installs to `/opt/corlinman/bin`, runs under `systemd`. No Docker on the host. | You want the smallest footprint, native systemd integration, and a single binary you can audit. |
| **Pre-built docker image (`--mode docker`)** | `curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh \| bash -s -- --mode docker` | Anywhere Docker Engine 24+ with the compose v2 plugin runs. Pulls `ghcr.io/ymylive/corlinman:<tag>` and brings up corlinman + newapi together. | You want container isolation, you're on a distro with an unusual or pre-2.28 glibc, or you already manage everything via compose. |

> Heads up: **Linux aarch64** (Graviton / Ampere) prebuilts are not yet
> published — upstream numkong NEON SDOT cross-compile blocker. The
> installer exits cleanly with a `cargo build --release` instruction on
> that arch. Use source build (`scripts/build-release.sh`) or the docker
> image (multi-arch, includes `linux/arm64`).

On first run either path opens
`http://localhost:6005/onboard` — the 4-step wizard: admin
account → newapi connection (token + base URL) → pick default
LLM / embedding / TTS models → confirm. The wizard writes a
complete `config.toml` atomically.

See [`deploy/install.sh`](deploy/install.sh) for environment overrides
(`CORLINMAN_VERSION`, `CORLINMAN_PREFIX`, `CORLINMAN_DATA_DIR`,
`CORLINMAN_PORT`).

### From source

```bash
# Build locally. Host needs docker + buildx. Output is a single image.
git clone https://github.com/ymylive/corlinman && cd corlinman
docker buildx build --platform linux/amd64 \
  -f docker/Dockerfile -t corlinman:latest --target runtime --load .

# Start the stack (gateway + embedded python agent + static admin bundle).
docker compose -f docker/compose/docker-compose.yml up -d
```

Visit `http://127.0.0.1:6005/health` to confirm, then open the admin UI.
On first run, walk the 4-step onboard wizard at `/onboard`, or use the
CLI wizard:

```bash
docker exec -it corlinman corlinman onboard
```

### Native (build from source)

Requirements: Rust 1.95+, Python 3.12, `uv`, Node 20+, `pnpm`, `protoc`.

```bash
./scripts/dev-setup.sh                              # deps + proto + git hooks
cargo build --release -p corlinman-gateway -p corlinman-cli
uv sync --frozen
pnpm -C ui install && pnpm -C ui build

./target/release/corlinman onboard                  # interactive wizard
./target/release/corlinman dev                      # gateway + python + UI
```

Data lives in `~/.corlinman/` by default; override with `--data-dir` or
`CORLINMAN_DATA_DIR`. See [`docs/runbook.md`](docs/runbook.md) for the
production deployment playbook (nginx reverse proxy + DNS-01 TLS via
acme.sh + systemd or docker-compose).

---

## Core concepts

### The agent

An agent in corlinman is a Python `reasoning_loop` wrapped around a
provider SDK. It takes a message history, emits tokens + tool calls,
consumes tool results, and iterates until the model signals `stop`. The
Rust gateway owns the transport, multiplexes channels onto it, persists
sessions, and enforces governance (rate limits, approvals, timeouts).

Agents are defined as **frontmatter-headed Markdown**
(`~/.corlinman/agents/<name>.md`), hot-editable from the admin UI's
Monaco editor, and routed by the `model` field or per-channel binding.

### Tools (plugins)

Tools are not prompts-in-a-template. Every tool corlinman exposes is a
real program that runs in its own sandbox, communicates over
JSON-RPC 2.0 on stdio (or gRPC for long-lived "service" plugins), and
publishes a JSON Schema that the agent sees directly via OpenAI
tool_call semantics. Three plugin types:

| Type      | Transport                         | Lifetime                               | Use case                                |
| --------- | --------------------------------- | -------------------------------------- | --------------------------------------- |
| `sync`    | JSON-RPC stdio                    | Spawned per call                       | Calculator, HTTP fetch, shell one-shots |
| `async`   | JSON-RPC stdio + `/plugin-callback` | Spawn → return task_id → webhook back | Long jobs (image gen, LLM sub-calls)    |
| `service` | gRPC over UDS                     | Long-lived supervised child            | Stateful integrations (DB pools, Git)   |

Plugins can be written in **any language** (Python, Node, Rust, bash,
Go…) because the contract is stdio/gRPC + JSON, not a Python import
hook. Optional Docker sandboxing (bollard-driven) enforces memory, CPU,
read-only root, network isolation, and capability drops. Untrusted
plugins can demand a human approval before every call.

Full authoring guide: [`docs/plugin-authoring.md`](docs/plugin-authoring.md).

### Memory

Two layers of persistence, both auditable:

- **Conversation memory.** Per-session append-only message history in
  SQLite (`sessions.sqlite`), trimmed to a configurable message cap,
  keyed by channel binding or client-supplied `session_key`.
- **Knowledge memory (RAG).** Hybrid retrieval over a usearch HNSW index
  (dense vectors) and SQLite FTS5 BM25 (keyword), fused with Reciprocal
  Rank Fusion. Optional cross-encoder rerank (`bge-reranker-v2-m3` by
  default) on top. Filter by tag, debug-query from the admin UI, rebuild
  from source via the CLI.

Neither is a black box: every chunk has a `source_path`, every message
has a timestamp, every retrieval scores through the UI.

### Channels

A channel is any producer of `ChatRequest`. corlinman ships with:

- **HTTP + SSE** — OpenAI-compatible `/v1/chat/completions` (stream and
  non-stream), `/v1/embeddings`, `/v1/models`.
- **QQ (OneBot v11)** — forward WebSocket bridge with image/audio
  multimodal forwarding, keyword filtering, per-group / per-sender
  rate limits, and real bindings back to the gateway's session store.
- **Telegram** — `teloxide`-based long-poll adapter for group + private
  chats.
- **Scheduler** — `tokio-cron-scheduler` that fires an agent at a cron
  expression with a canned prompt template (for daily digests, alerting
  bots, etc.).

Each channel shares the same agent loop — switch models mid-flight with
a config reload, no channel restart.

### Governance

- **Approvals.** Configurable per tool: `allow` / `deny` / `prompt`.
  `prompt` parks the tool call, pushes a notification via the SSE
  broadcast, and waits for a human click in the admin UI (or a 5-min
  timeout → auto-deny).
- **Rate limits.** Per-group and per-sender token buckets on channel
  adapters.
- **Config live-reload.** `POST /admin/config` accepts a TOML body,
  validates it, and atomically swaps in the new config without restart
  (restart-required fields are flagged in the response).
- **Observability.** OTel OTLP export (traces + logs; Rust and Python
  share `traceparent`), Prometheus `/metrics` with 7 metric families
  covering QPS, latency, tool-call rate, backoff, stream inflight,
  RAG stage timings, and plugin execution duration. A bundled
  Grafana dashboard lives in `ops/dashboards/corlinman.json`.
- **Doctor.** `corlinman doctor` runs 21 local checks (manifest
  duplicates, Python subprocess health, disk space, log rotation,
  Docker daemon reachability, scheduler next triggers, pending
  approval overflow, broken symlinks…) in under 1 s on an empty
  environment.

---

## Providers

| Provider   | Chat | Streaming | Tool calls | Embeddings | Status       |
| ---------- | :--: | :-------: | :--------: | :--------: | ------------ |
| Anthropic  |  ✅  |    ✅     |     ✅     |    n/a     | production   |
| OpenAI     |  ✅  |    ✅     |     ✅     |     ✅     | production   |
| Google     |  ✅  |    ✅     |     ✅     |     ✅     | production   |
| DeepSeek   |  ✅  |    ✅     |     ✅     |    n/a     | production   |
| Qwen       |  ✅  |    ✅     |     ✅     |    n/a     | production   |
| GLM        |  ✅  |    ✅     |     ✅     |    n/a     | production   |
| _OpenAI-compatible_ (local vLLM, Ollama, SiliconFlow, any gateway speaking the spec) |  ✅  | ✅ | ✅ | ✅ | works via `providers.openai.base_url` |
| **newapi** ([QuantumNous/new-api](https://github.com/QuantumNous/new-api)) | ✅ | ✅ | ✅ | ✅ | sidecar pools LLM + embedding + audio TTS channels behind one URL; managed via `/admin/newapi` and the onboard wizard. MIT licence. |

Custom providers are a ~200-line Python class: subclass
`corlinman_providers.base.CorlinmanProvider`, register a model-name
prefix in `registry.py`, and you're in the agent loop. See
[`python/packages/corlinman-providers/`](python/packages/corlinman-providers/).

---

## Admin UI

A Next.js 15 static-export bundle served by nginx (or directly from the
gateway at `/`). **Tidepool** design system — warm-amber glass with
day + night themes (sun/moon pill in the top nav), Instrument Serif
hero display over Geist sans/mono, `⌘K` command palette, framer-motion
page transitions, live SSE dashboards.

Ten pages covering the full control plane:

- **Dashboard** (`/`) — stat cards + live activity feed (SSE from
  `/admin/logs/stream`) + 7-check system health panel.
- **Plugins** — list with status dots, detail with a schema-driven
  "Test invoke" form that hits `POST /admin/plugins/:name/invoke`.
- **Agents** — list + Monaco editor for agent Markdown with
  frontmatter validation.
- **RAG** — stats cards, debug query box with score bars, confirm-gated
  rebuild trigger.
- **Channels** — per-adapter status lights, connection reset button,
  inline keyword editor, recent-message transcript.
- **Scheduler** — job table with live next-trigger countdown, manual
  trigger button, execution history modal.
- **Approvals** — pending tab (SSE live) + history tab.
- **Models** — provider cards with enabled toggle, inline alias CRUD.
- **Config** — Monaco TOML editor with section nav, JSON-schema hints,
  validation issues panel sliding in from the bottom.
- **Logs** — virtualized list of SSE events with level + subsystem
  filters and a pause-stream toggle.

Every page plays nice with the keyboard and passes WCAG AA contrast
in both themes. Internationalisation (zh-CN / en) ships in 0.1.3.

---

## Configuration

corlinman boots from `$CORLINMAN_DATA_DIR/config.toml` (annotated
example: [`docs/config.example.toml`](docs/config.example.toml)). A
minimum production config looks like:

```toml
[server]
port = 6005
bind = "0.0.0.0"
data_dir = "/data"

[admin]
username = "admin"
# Generate via the onboard wizard, or:
# echo -n 'your-password' | argon2 "$(openssl rand -hex 8)" -id -m 15 -t 2 -p 1 -l 32 -e
password_hash = "$argon2id$v=19$m=32768,t=2,p=1$..."

# Providers are a free-form `BTreeMap<String, ProviderEntry>`. The table
# key is operator-chosen; `kind` selects the wire shape. Full reference:
# docs/providers.md (14 supported kinds + recipes).
[providers.openai]
kind = "openai"
api_key = { env = "OPENAI_API_KEY" }
base_url = "https://api.openai.com/v1"
enabled = true

[providers.anthropic]
kind = "anthropic"
api_key = { env = "ANTHROPIC_API_KEY" }
enabled = true

# Need a CN endpoint or a niche aggregator? Add an OpenAI-compat entry
# with a chosen name — no Rust changes required. See docs/providers.md §3.
# [providers.openrouter]
# kind = "openai_compatible"
# api_key = { env = "OPENROUTER_API_KEY" }
# base_url = "https://openrouter.ai/api/v1"
# enabled = true

[models]
default = "gpt-4o-mini"

[models.aliases]
smart = "claude-opus-4-7"
cheap = "gpt-4o-mini"

# Optional: QQ bot channel
# [channels.qq]
# enabled = true
# forward_ws_url = "ws://127.0.0.1:6700"
# group_allowlist = [123456789]
```

Everything is hot-reloadable via `POST /admin/config` or the **Config**
page in the admin UI. Restart-required fields (bind address, port,
channel enablement) return `requires_restart: true` and are flagged in
the response.

---

## Production deployment

The deployment reference setup, as used in the hosted demo:

```
Internet ──[HTTPS]──▶ Cloudflare (CDN + edge TLS + DDoS)
                          │
                          ▼
              ┌─────────────────────────┐
              │   nginx on the VM        │
              │  TLS: LE ECC via acme.sh │
              │  DNS-01 (no port 80      │
              │  exposed to ACME)        │
              │                          │
              │  location /admin|/v1...  │── 127.0.0.1:6005 ──▶ corlinman container
              │  location /              │── /opt/corlinman/ui-static/ (static files)
              └─────────────────────────┘
```

- **TLS** lives at both the Cloudflare edge (universal SSL) and the
  origin (Let's Encrypt ECC, auto-renewed by acme.sh via a Cloudflare
  API token — DNS-01 challenge, so you never need to punch port 80 out
  for HTTP-01).
- **Static bundle** is served directly by nginx from
  `/opt/corlinman/ui-static/` (rsync target from `ui/out/` on the build
  host). The gateway never fights nginx for static bytes.
- **Upgrade path** for the UI: rebuild locally, rsync, done — no
  container restart. For the gateway: rebuild the image, transfer via
  `docker save | ssh docker load`, `docker compose up -d`.

Full runbook with nginx config, acme.sh commands, healthcheck wiring,
and rollback procedure: [`docs/runbook.md`](docs/runbook.md).

---

## Development workflow

```bash
# Clone + set up hooks, deps, and proto generation.
./scripts/dev-setup.sh

# Run the whole stack in dev mode with hot reload.
corlinman dev

# Full gate (what CI + pre-commit run).
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
uv run ruff check python/packages/
uv run mypy python/packages/
uv run pytest -m "not live_llm and not live_transport"
pnpm -C ui typecheck
pnpm -C ui lint
pnpm -C ui build
bash scripts/gen-proto.sh && git diff --exit-code python/packages/corlinman-grpc/src/corlinman_grpc/_generated/
```

Coding expectations, branch + commit conventions, live-lane tests, and
boundary checks all live in [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Repository layout

```
rust/crates/*       Rust crates (gateway / plugins / vector / cli / ...)
python/packages/*   Python packages (providers / agent / embedding / server)
proto/              Protocol Buffers (cross-language gRPC IDL)
ui/                 Next.js admin console (static export)
qa/scenarios/       Executable YAML test scenarios
docker/             Multi-stage Dockerfile + compose profiles
docs/               Architecture / plugin authoring / runbook / roadmap
.git-hooks/         pre-commit (FAST_COMMIT=1 escape hatch)
scripts/            dev-setup.sh, gen-proto.sh
ops/                Grafana dashboard + observability compose
```

---

## Documentation map

- [Architecture](docs/architecture.md) — message flow, crate/package graph, gRPC bus
- [Providers reference](docs/providers.md) — 14 supported `kind`s + recipes (Ollama / OpenRouter / SiliconFlow / Groq)
- [Plugin authoring](docs/plugin-authoring.md) — write your own sync / async / service plugin
- [Skills, agents & the variable cascade](docs/guides/skills-and-agents.md) — author `skills/`, `agents/`, `TVStxt/*`
- [v0.1 → v0.2 migration](docs/migration/v1-to-v2.md) — manifest v2, vector v6, new config sections, block protocol
- [Runbook](docs/runbook.md) — production deployment + incident handling
- [Milestones](docs/milestones.md) — per-milestone status
- [Roadmap](docs/roadmap.md) — sprint-level plan beyond 1.0
- [Changelog](CHANGELOG.md) — release-by-release
- [Performance baseline](docs/perf-baseline-1.0.md) — p50/p99 numbers

---

## Roadmap + status

**1.0 is released.** Milestones M0–M8 all closed; tagged `v0.1.0`
2026-04-21, followed by `v0.1.1` (deployment hotfixes) and `v0.1.2`
(admin UI redesign). Post-1.0 work is tracked in
[`docs/milestones.md`](docs/milestones.md) and
[`docs/roadmap.md`](docs/roadmap.md).

Near-term (P1):

- Plugin SDK packages on npm / PyPI / crates.io
- MCP (Model Context Protocol) compatibility layer — expose corlinman
  plugins as MCP tools to Claude Desktop / Cursor
- OIDC login (replace basic auth)
- Audit log of all admin + tool actions
- 24 h soak test + fuzz corpus in CI

Longer-term (P2):

- Multi-tenant (data_dir per tenant + RBAC)
- Distributed mode (multi-gateway + Redis/Postgres shared state)
- External vector DB backends (Qdrant / Milvus)
- Canvas renderer + voice I/O
- VS Code / JetBrains / Tauri desktop clients

---

## Contributing

Contributions welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
architecture invariants (e.g. _no Anthropic-specific types leaking
into the provider trait_), test-lane conventions, and the pre-commit
hooks you'll install. Issues on GitHub for bugs / features.

---

## License

MIT. See [`LICENSE`](LICENSE).

---

## 中文速览

**corlinman 是一个可自托管的智能体平台。** 不只是 LLM 的 API 代理，也不是拖拽工作流的工具箱——它是一套有主张的运行时：让语言模型拥有**持久记忆**、**真实工具**、**多通道接入**、**可审计的运维面板**，全部跑在你自己的机器上。

**核心能力**：

- **一个 agent 循环，多家 provider**：在 Anthropic / OpenAI / Google / DeepSeek / Qwen / GLM 上跑 OpenAI 标准 tool_call 语义；配置热重载、按模型别名路由。
- **真工具，不是 prompt 模板**：同步 / 异步 / 常驻三种插件类型，统一 JSON-RPC 2.0 stdio 或 gRPC 通信，可选 Docker 沙箱 + 人工审批闸。
- **跨会话的记忆**：SQLite 会话历史 + HNSW/BM25 混合检索（RRF 融合 + 可选 cross-encoder rerank），RAG 是智能体内置能力而非外挂。
- **通道作为一等公民**：QQ (OneBot v11) / Telegram / 定时任务 / OpenAI 兼容 HTTP/SSE 并行接入，共享同一 agent 循环。
- **严肃的运维面板**：**Tidepool** 暖橙玻璃风格 Next.js 管理界面（日 / 夜双主题，插件 / 知识库 / 日志 / 审批 / 配置 / 调度器 / 模型路由），OTel + Prometheus 埋点，21 项 `doctor` 体检。

**在线 demo**：<https://corlinman.cornna.xyz>

**架构**：Rust 掌网络（axum gateway + tonic + 插件 runtime + 向量引擎 + CLI），Python 掌 LLM（provider SDK + reasoning loop + embedding），两边只通过强类型 gRPC 总线通信，W3C `traceparent` 贯穿全链路。

**快速开始**：

```bash
git clone https://github.com/ymylive/corlinman && cd corlinman
docker buildx build --platform linux/amd64 \
  -f docker/Dockerfile -t corlinman:latest --target runtime --load .
docker compose -f docker/compose/docker-compose.yml up -d
docker exec -it corlinman corlinman onboard
```

数据默认落在 `~/.corlinman/`，通过 `CORLINMAN_DATA_DIR` 覆盖。完整生产部署（nginx + acme.sh DNS-01 + Cloudflare）见 [`docs/runbook.md`](docs/runbook.md)，架构细节见 [`docs/architecture.md`](docs/architecture.md)。
