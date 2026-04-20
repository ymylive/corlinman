# Milestones 进展跟踪

单一页面跟踪 corlinman 里程碑实况。每次滩头推进后更新此文件；里程碑**定义**（目标、完成标准、工期估算）见计划文件 [§11](/Users/cornna/.claude/plans/openclaw-rust-python-corlinma-graceful-meerkat.md)，本文只记"当前跑到哪"。

**状态图例**：✅ 已完成 / 🚧 部分完成（active）/ ⏳ 未开工 / ⚠️ blocked

## 总览表（updated 2026-04-20）

| 里程碑 | 目标（一句话） | FT 工期 | 状态 | 实际完成比 | 关键 evidence |
| --- | --- | --- | --- | --- | --- |
| **M0** Pre-work | workspace / toolchain / CI / proto 骨架 | 1.5 周 | ✅ | 100% | 9 crate + 5 py pkg + 6 proto + 14 UI route + Dockerfile + CI + hooks + 5 docs |
| **M1** Gateway skeleton | axum `/health` + 非流式 chat 透传 + Python agent 起来 | 2 周 | 🚧 | ~50% | `/health` + SIGTERM 143 ✅；core placeholder engine ✅；Python grpc.aio server ✅；chat_completions 透传 WIP |
| **M2** Streaming + tool-call | SSE + OpenAI tool_calls 解析 + Python reasoning loop | 3 周 | ⏳ | 0% | — |
| **M3** Plugin runtime | manifest + JSON-RPC stdio + sync/async/service 3 类型 | 3 周 | 🚧 | ~40% | manifest 发现 + registry + sync stdio runtime + CLI list/inspect/invoke/doctor 已就位；async/service/沙箱/审批 WIP |
| **M4** Vector/RAG | HNSW + FTS5 BM25 + RRF 融合 + 可选 rerank | 3 周 | 🚧 | ~25% | sqlx + usearch 基础 CRUD ✅；RRF 融合 + rerank WIP |
| **M5** Channels QQ/OneBot | 正向 WS + Agent 分派 + 关键词过滤 | 2 周 | 🚧 | ~40% | 正向 WS 客户端 + OneBot v11 parse + router + mock integration test ✅；gateway chat pipeline 对接 WIP |
| **M6** Admin UI | Next.js 替代 AdminPanel | 3 周（并行 M3-M5） | 🚧 | ~92% | S1-S5 骨架 + auth/config/logs/approvals 真联调 ✅；S6 新增 rag/channels/scheduler/models 4 路由 + Monaco config + plugin invoke + agent 编辑 + Playwright admin-full E2E ✅；剩余：scheduler cron 运行时需 M7 + QQ runtime 状态回填 |
| **M7** Observability + CLI ops | doctor / onboard / metrics / Prometheus / OTel | 2 周 | 🚧 | ~92% | S7 closed: OTel SDK+OTLP exporter (Rust + Python, W3C traceparent propagation), plugin/agent/vector real metrics wired, doctor 20 checks (<1s on empty env), /health real probes, grafana dashboard JSON + obs compose (prometheus + grafana + jaeger) |
| **M8** 1.0 Release | docs + docker image + 全绿 QA | 2 周 | 🚧 | ~90% | S8 closed: CHANGELOG.md + release-notes v0.1.0 + README quickstart/badges/screenshot-ref ✅; `corlinman qa run` runner implemented (in-process axum + scripted ChatBackend, python stdio plugin, hybrid RAG); 8 scenarios authored — 7 pass offline, 1 (`fresh-install`) marked `requires_live`; `corlinman qa bench` + `docs/perf-baseline-1.0.md` ✅; v0.1.0 tag + gh release created. Deferred to 0.1.1: docker image + installation screencast + release comms. |

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

## M1 还差什么（WIP 清单）

- [ ] `/v1/chat/completions` 非流式路径（gateway → agent-client → Python）
- [ ] provider failover 分类（5+ 种 FailoverReason 映射）
- [ ] `config.toml` schema 校验 + 错误诊断
- [ ] insta 快照 20 个 chat response

## 更新约定

- 每次滩头推进（新 milestone 跑通 / 风险状态变化 / 新 evidence）更新本文件
- 不要删除历史信息，过时状态用删除线而非真删除
- 日期一律用 `YYYY-MM-DD` 格式
- 一级状态变化（⏳→🚧→✅）同步更新计划文件 §11 表格
