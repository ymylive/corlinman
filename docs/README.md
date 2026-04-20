# corlinman docs

corlinman 是一个从零设计的自托管 LLM 工具箱：单机即可运行、统一管理多 provider、支持插件化工具执行、RAG 知识库、QQ 机器人、现代管理后台。

**设计原则**：精简优先——能用标准 OpenAI tool-use 就不造自研 marker；能用 HNSW+BM25 就不搞神经路由；能用 `{{name}}` 就不加多阶段 placeholder。

**工程基线**：类型化配置（pydantic + schemars）、结构化日志（structlog + tracing JSON）、分类错误（`FailoverReason` enum）、优雅退出（SIGTERM → 143）、doctor/onboard CLI、manifest-first 插件发现、docs-as-code。

## 文档导航

| 文档 | 读者 | 内容 |
| --- | --- | --- |
| [architecture.md](architecture.md) | 开发者 / 贡献者 | 高层架构、crate/package 图、proto 速览、关键跨进程流 |
| [plugin-authoring.md](plugin-authoring.md) | 插件作者 | Manifest schema、4 种语言 Hello World、JSON-RPC 协议、沙箱、审批 |
| [runbook.md](runbook.md) | 部署者 / 运维 | `doctor` / `/health` / 日志关联 / 常见故障 10 条 |
| [milestones.md](milestones.md) | 所有人 | 里程碑进展跟踪（单页） |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 想提 PR 的人 | 提交流程、代码风格、PR 要求、分支策略 |
| [../README.md](../README.md) | 所有人 | 项目顶层说明 |

## 按身份推荐阅读顺序

**新用户**：
1. 顶层 [README.md](../README.md) —— 一段话说明
2. [architecture.md](architecture.md) §1 高层图 + §2 为什么 Rust+Python
3. 按需翻 [plugin-authoring.md](plugin-authoring.md) 或 [runbook.md](runbook.md)

**贡献者**（准备提 PR）：
1. [../CONTRIBUTING.md](../CONTRIBUTING.md) —— 流程、风格、hooks
2. [architecture.md](architecture.md) 全文 —— 特别是 §3 crate 图、§5 proto 速览
3. 改到哪个 crate 就翻那个 crate 的 `README.md`（各 crate 自带，M1 起）
4. `qa/scenarios/*.yaml` 看看改动会不会破坏契约

**运维 / 部署者**：
1. 顶层 [README.md](../README.md) 快速启动段
2. [runbook.md](runbook.md) —— 10 条常见故障 + doctor 使用
3. [milestones.md](milestones.md) —— 当前能用到什么程度

## 文档范围说明

这里写的是**用户和贡献者侧**的文档。以下内容不在这里：

- 计划文件（单一事实来源）：`/Users/cornna/.claude/plans/openclaw-rust-python-corlinma-graceful-meerkat.md`
  —— 里程碑、风险、测试门禁、命名约定放在那里，不在 docs 复述。
- API reference：从 `proto/corlinman/v1/*.proto` + rustdoc/pdoc 生成，托管在 `https://docs.corlinman.dev`（M8 上线）。
- 变更记录：`CHANGELOG.md`（根级别，M8 开始维护）。

## 文档状态

corlinman 在 M0 完成 → M1 在 flight。本目录下的 doc 会随里程碑推进持续修订：

| 里程碑 | 相关 doc 更新 |
| --- | --- |
| M0（已完成） | 本批初始 doc 建立骨架 |
| M1 Gateway | `architecture.md` §6 填入真实时序图数据 |
| M3 Plugin runtime | `plugin-authoring.md` 补真实可跑示例输出 |
| M4 Vector/RAG | 新增 `algorithms/hybrid-retrieval.md`（RRF 融合 + rerank 设计） |
| M7 Observability | `runbook.md` 填 metric 名与 Grafana 截图 |
| M8 1.0 Release | 总体定稿，发布文档 |

任何 doc bug（过时、错漏、不清楚）按普通 bug 对待：开 issue 或直接 PR。
