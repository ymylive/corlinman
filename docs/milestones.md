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
| **M6** Admin UI | Next.js 替代 AdminPanel | 3 周（并行 M3-M5） | 🚧 | ~20% | 14 路由骨架 ✅；3 页（plugins/agents/logs）mock 数据 ✅；真 API 对接 WIP |
| **M7** Observability + CLI ops | doctor / onboard / metrics / Prometheus / OTel | 2 周 | ⏳ | ~5% | 日志 JSON 到 stdout ✅（不含 OTel export） |
| **M8** 1.0 Release | docs + docker image + 全绿 QA | 2 周 | ⏳ | 0% | — |

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
