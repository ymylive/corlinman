# corlinman

自托管的 LLM 工具箱：统一管理多 provider（Anthropic / OpenAI / Google / DeepSeek / Qwen / GLM）、插件化工具执行、RAG 知识库、QQ 机器人通道、管理后台。**一台机器跑起来就能用**，重视可观测性和运维友好度。

## 架构一览

- **Rust** — 网关（axum）、WebSocket、通道适配（QQ/OneBot v11）、插件运行时、向量存储（HNSW + BM25）、CLI
- **Python** — LLM provider（anthropic/openai/google/china）、Agent reasoning loop、embedding。仅暴露 gRPC
- **前端** — Next.js 15 + React 19 + shadcn/ui
- **IPC** — 单一 gRPC 总线（tonic ↔ grpcio），W3C traceparent 跨语言 tracing

## 设计原则

**精简优先**。同类项目常见的过度设计（多种特殊协议 marker、多阶段 placeholder、神经路由 rerank、多种插件类型）在 corlinman 里都砍到最少：

- 工具调用用 **OpenAI 标准 JSON tool_calls**，不造自研 marker
- Placeholder 语法统一 `{{namespace.name}}`，启动时一次替换
- 插件只有 **3 种类型**（sync / async / service），统一 JSON-RPC 2.0 stdio 协议或 gRPC（service）
- RAG 是 **HNSW + BM25 + RRF 融合 + 可选 cross-encoder rerank**

## 快速开始（dev）

```bash
./scripts/dev-setup.sh            # 装 hooks、同步依赖、生成 proto
corlinman onboard                  # 首次配置向导
corlinman dev                      # 启动 gateway + Python agent + UI
```

数据默认在 `~/.corlinman/`，也可以 `--data-dir` 或 `CORLINMAN_DATA_DIR` 覆盖。

## 仓库结构

```
rust/crates/*       Rust crates（gateway / plugins / vector / cli / ...）
python/packages/*   Python packages（providers / agent / embedding / server）
proto/              Protocol Buffers（跨语言 gRPC IDL）
ui/                 Next.js 管理后台
qa/scenarios/       可执行 YAML 测试剧本
docker/             多阶段 Dockerfile + compose profiles
docs/               架构 / 插件作者 / 运维手册
.git-hooks/         pre-commit（FAST_COMMIT=1 逃生舱）
scripts/            dev-setup、gen-proto
```

## 文档

- [架构](docs/architecture.md)
- [插件作者指南](docs/plugin-authoring.md)
- [运维手册](docs/runbook.md)
- [里程碑进展](docs/milestones.md)

## 状态

Pre-alpha — M0 已完成，M1 在 flight。完整计划见 `/Users/cornna/.claude/plans/openclaw-rust-python-corlinma-graceful-meerkat.md`。

## License

MIT
