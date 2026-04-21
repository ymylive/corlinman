# Milestones 进展跟踪

单一页面跟踪 corlinman 里程碑实况。每次滩头推进后更新此文件；里程碑**定义**（目标、完成标准、工期估算）见计划文件 [§11](/Users/cornna/.claude/plans/openclaw-rust-python-corlinma-graceful-meerkat.md)，本文只记"当前跑到哪"。

**状态图例**：✅ 已完成 / 🚧 部分完成（active）/ ⏳ 未开工 / ⚠️ blocked

## 总览表（updated 2026-04-21）

**1.0 已发布** — M0–M8 全部关闭。`v0.1.0` tag 于 2026-04-21 打出，随后
`v0.1.1`（部署 hotfix）、`v0.1.2`（admin UI 重设计）陆续落地。后续演进
按 sprint 继续跑，见文末 "Post-1.0" 节。

| 里程碑 | 目标（一句话） | FT 工期 | 状态 | 实际完成比 | 关键 evidence |
| --- | --- | --- | --- | --- | --- |
| **M0** Pre-work | workspace / toolchain / CI / proto 骨架 | 1.5 周 | ✅ | 100% | 9 crate + 5 py pkg + 6 proto + 14 UI route + Dockerfile + CI + hooks + 5 docs |
| **M1** Gateway skeleton | axum `/health` + 非流式 chat 透传 + Python agent 起来 | 2 周 | ✅ | 100% | S1 closed: `/v1/chat/completions` 非流式 + 流式，provider failover 分类，config.toml schema 校验，chat_completions 透传稳定 |
| **M2** Streaming + tool-call | SSE + OpenAI tool_calls 解析 + Python reasoning loop | 3 周 | ✅ | 100% | S1 closed: SSE 流 + OpenAI JSON tool_calls + Python reasoning_loop（含 tool_result 反向注入 + cancel + approval hook） |
| **M3** Plugin runtime | manifest + JSON-RPC stdio + sync/async/service 3 类型 | 3 周 | ✅ | 100% | S2 closed: 3 种插件类型 + Docker 沙箱（bollard）+ 审批闸（SQLite + SSE）+ hot reload（notify）+ CLI `list/invoke/doctor` |
| **M4** Vector/RAG | HNSW + FTS5 BM25 + RRF 融合 + 可选 rerank | 3 周 | ✅ | 100% | S3 closed: usearch HNSW + SQLite FTS5 BM25 + RRF 融合 + 可选 cross-encoder rerank + tag filter + LRU unload + migration v1→v4 + CLI `vector stats/query/rebuild` |
| **M5** Channels QQ/OneBot | 正向 WS + Agent 分派 + 关键词过滤 | 2 周 | ✅ | 100% | S4 closed: QQ OneBot v11（正向 WS + 多模态 + 速率限制 + 关键词过滤 + ChannelBinding 回填）+ Telegram（teloxide long-poll） |
| **M6** Admin UI | Next.js 替代 AdminPanel | 3 周（并行 M3-M5） | ✅ | 100% | S5/S6 closed: auth/login + config 热重载 + logs SSE + approvals UI + RAG/channels/scheduler/models 4 新路由 + Monaco config 编辑 + plugin invoke + agent 编辑 + Playwright admin-full E2E。v0.1.2 完整视觉重设计（Linear 风 + framer-motion + cmdk）。 |
| **M7** Observability + CLI ops | doctor / onboard / metrics / Prometheus / OTel | 2 周 | ✅ | 100% | S7 closed: OTel SDK+OTLP exporter (Rust + Python, W3C traceparent propagation), plugin/agent/vector real metrics wired, doctor 21 checks (<1s on empty env), /health real probes, grafana dashboard JSON + obs compose (prometheus + grafana + jaeger) |
| **M8** 1.0 Release | docs + docker image + 全绿 QA | 2 周 | ✅ | 100% | S8 closed: CHANGELOG.md + release-notes v0.1.0 + README quickstart/badges/screenshot-ref ✅; `corlinman qa run` runner + 8 scenarios（7 offline pass，`fresh-install` 标 `requires_live`）; `corlinman qa bench` + `docs/perf-baseline-1.0.md` ✅; v0.1.0 tag + gh release created. Docker image 已在 v0.1.1 补齐（本地构建 + 生产跑通 @ corlinman.cornna.xyz）。 |

## M0 交付物清单

以下是 M0 已着陆的文件、目录和组件，供新贡献者找切入点。

**Rust workspace**：`/Users/cornna/project/corlinman/Cargo.toml` + 9 crate 骨架

```
rust/crates/corlinman-core/
rust/crates/corlinman-proto/
rust/crates/corlinman-gateway/
rust/crates/corlinman-channels/
rust/crates/corlinman-plugins/
rust/crates/corlinman-vector/
rust/crates/corlinman-agent-client/
rust/crates/corlinman-scheduler/
rust/crates/corlinman-cli/
```

**Python workspace**：`/Users/cornna/project/corlinman/pyproject.toml` + 5 package 骨架

```
python/packages/corlinman_grpc/
python/packages/corlinman_providers/
python/packages/corlinman_agent/
python/packages/corlinman_embedding/
python/packages/corlinman_server/
```

**Proto**：`/Users/cornna/project/corlinman/proto/corlinman/v1/` 下 6 个 .proto（common / llm / embedding / vector / plugin / agent）。

**UI**：14 个 App Router route 骨架 in `/Users/cornna/project/corlinman/ui/app/`。

**Docker**：`/Users/cornna/project/corlinman/docker/Dockerfile` 多阶段（cargo-chef + uv + pnpm + tini）。

**CI**：GitHub Actions + `.git-hooks/pre-commit`（fmt + clippy + ruff + mypy + typecheck），`FAST_COMMIT=1` 逃生舱。

**Docs**：`docs/architecture.md` / `plugin-authoring.md` / `runbook.md` / `README.md` / `milestones.md` 五份基础文档。

## S6 evidence（2026-04-20 landed）

- Rust: 4 new admin routes (`rag`, `channels`, `scheduler`, `models`) + `GET /admin/config/schema` + `POST /admin/plugins/:name/invoke` + `GET/POST /admin/agents/:name`. 127 gateway tests (+22 new, all green).
- UI: `@monaco-editor/react` wired. 6 pages wired to live APIs (`rag`, `channels/qq`, `scheduler`, `models`, `config`, plus detail pages for plugin invoke + agent edit). Plugin list / agent list now link to `/plugins/detail?name=` / `/agents/detail?name=` (query-param routing to keep Next static export intact).
- Test: `ui/tests/e2e/admin-full.spec.ts` covers plugins→detail→invoke, agents→detail→save, config→save with stubbed routes. Gated behind `CORLINMAN_E2E=1` until a live gateway harness exists; unit-level coverage of the underlying Rust + TS surfaces is already part of the default gate.
- Known gap: scheduler cron runtime + QQ runtime-status reporting remain M7 work. Admin surface is honest about it (`runtime: "unknown"`, trigger returns 501 with history record).

## Post-1.0 发布动作

### v0.1.1 — 2026-04-21 部署 hotfix

初次把 v0.1.0 image 拉到生产机时踩出来的一串 Dockerfile / boot-path 问题。
release notes: [`docs/release-notes-v0.1.1.md`](release-notes-v0.1.1.md)。

- `pnpm -C ui export` 去除（Next.js 14 `output:"export"` 自动 emit）
- Rust base `1.85-slim` → `1.95-slim`（对齐 rust-toolchain.toml，cargo-chef 要求 rustc 1.88+）
- rust-builder 层加 `binutils` + `g++` + `RUSTFLAGS=-C link-arg=-fuse-ld=bfd`（lld 在 Rosetta 2 / QEMU user-mode 下 SIGSEGV）
- CLI binary COPY 路径修正（cargo 产出 `corlinman`，不是 `corlinman-cli`）
- gateway `BIND` env 支持，默认 `127.0.0.1`（dev 安全）、容器设 `0.0.0.0`
- runtime image 随带 python 源树（uv 对 workspace 成员无视 `--no-editable`，.pth 指 `/build/python/...`）

### v0.1.2 — 2026-04-21 admin UI 重设计

纯前端，无 Rust/Python/Dockerfile 改动。release notes:
[`docs/release-notes-v0.1.2.md`](release-notes-v0.1.2.md)。

- Linear/Vercel 风：单 indigo 高亮、Geist 字体、border-over-shadow、6-8px radius
- 新 Dashboard 首页（stat cards + SSE 活动流 + 7-check health panel）
- 侧栏 240↔56px 可折叠，active indicator 动画（framer-motion `layoutId`）
- `⌘K` 命令面板（cmdk）+ 主题切换 + 登出 + test-chat drawer
- 10 个页面全翻新，motion 统一（200ms 转场 + skeleton + sonner toasts）
- 新 login 页面（二列布局 + 星座背景 SVG）
- 新增 deps: `framer-motion`, `cmdk`, `geist`, `sonner`

### 生产部署就绪（2026-04-21）

参考部署跑在 <https://corlinman.cornna.xyz>：

- Debian 12 VM + docker 29
- Nginx 反代 `/admin|/v1|/health|/metrics|/plugin-callback` → `127.0.0.1:6005`
- Nginx 直 serve UI 静态 bundle（从 `/opt/corlinman/ui-static/`，rsync 上传）
- TLS: Let's Encrypt ECC via acme.sh **DNS-01 over Cloudflare API**（不需 HTTP-01 / 80 端口）
- 容器内 `docker/start.sh` supervisor 同时起 `corlinman-python-server` + `corlinman-gateway`

详见 [`docs/runbook.md`](runbook.md)。

## 更新约定

- 每次滩头推进（新 milestone 跑通 / 风险状态变化 / 新 evidence）更新本文件
- 不要删除历史信息，过时状态用删除线而非真删除
- 日期一律用 `YYYY-MM-DD` 格式
- 一级状态变化（⏳→🚧→✅）同步更新计划文件 §11 表格
