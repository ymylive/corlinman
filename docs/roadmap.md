# corlinman 详细开发路线图

> 面向**每一波可执行的 sprint**，每个 task 都能独立派给一名开发者或 agent。时间用 FT（Focus Time，每周 4 天专注）估算。

最后更新：2026-04-20。当前 HEAD `2cbe322`。

---

## 1. 当前 baseline

| 维度 | 完成比 | 证据 |
| --- | --- | --- |
| M0 Pre-work | ✅ 100% | 9 crate + 5 pkg + 6 proto + 14 UI route + Dockerfile + CI |
| M1 Gateway skeleton | ~70% | `/health` + plugin exec 接入 + `/v1/chat/completions` 非流式/流式 |
| M2 Streaming + tool-call | ~60% | OpenAI JSON tool_calls + SSE + reasoning loop Python 侧 |
| M3 Plugin runtime | ~50% | manifest + JSON-RPC stdio sync + CLI list/invoke/doctor |
| M4 Vector/RAG | ~40% | HNSW + SQLite FTS5 BM25 + RRF hybrid |
| M5 Channels | ~60% | QQ OneBot 闭环 + mock gocq E2E |
| M6 Admin UI | ~30% | 3 admin endpoints + basic auth + UI env switch |
| M7 Observability | ~40% | Prometheus 7 metrics + doctor 8 checks |
| M8 1.0 Release | ⏳ 0% | — |

**已通过**：193 Rust tests / 36 Python tests / clippy clean / mypy 0 issues / ruff clean / UI typecheck & lint clean.

---

## 2. 路线图总览

| Sprint | 目标 | FT 工期 | 输出 | 阻塞/依赖 |
| --- | --- | --- | --- | --- |
| **S1** | M1/M2 收尾 + M3 async 初步 | 1.5 周 | Python servicer 完整循环 + /plugin-callback + ModelRedirect | 无 |
| **S2** | M3 完整化 | 2 周 | Service plugin + Docker sandbox + 审批闸 + hot reload | S1 |
| **S3** | M4 RAG 完整化 | 2 周 | LRU unload + migration + upsert debounce + tag filter + CLI | 无 |
| **S4** | M5 Channels 增强 | 1 周 | 多模态 + 速率限制 + ChannelBinding 回填 | S2 审批闸（可选） |
| **S5** | M6 Admin 认证 + config 热重载 | 1.5 周 | Login 页 + session + config POST + logs SSE | S2 审批闸 |
| **S6** | M6 Admin UI 真联调 | 2 周 | 6 个 UI 页真 API + plugin invoke + Monaco | S5 |
| **S7** | M7 可观测性完整 | 1.5 周 | OTel + Grafana + doctor 20+ + 三端真埋点 | 无 |
| **S8** | M8 1.0 Release | 2 周 | Changelog + Docker image + QA 8 scenarios + 录屏 + v0.1.0 tag | S1-S7 全 |

**总计**：FT ≈13.5 周（约 3 个月）到 1.0 发布。

**并行机会**：S3 ∥ S2（无共代码）；S4 ∥ S3；S6 ∥ S7（UI 和 observability 独立）。

**P1/P2 长期**见 §11-12。

---

## 3. Sprint 1 — M1/M2 收尾 + M3 async 初步（1.5 FT 周）

**目标**：把 M1/M2 的 plugin 执行闭环完全走通（Python servicer 真正处理 ToolResult），加上 async plugin 的 callback 路由。

### S1.T1 — Python servicer 读 inbound ClientFrame（1.5 天）

**范围**：`python/packages/corlinman-server/src/corlinman_server/agent_servicer.py`

现在 `Chat` 方法只读首帧 `ChatStart`，后续帧全丢。需要：
1. 在 `async for request_frame in request_iterator` 里持续读
2. 识别 `ClientFrame.tool_result` → 调 `loop.feed_tool_result(call_id, result_json, is_error)`
3. 识别 `ClientFrame.cancel` → `loop.cancel()`
4. 识别 `ClientFrame.approval` → 留 TODO（S5 做）

**验收**：
- `uv run pytest python/packages/corlinman-server/tests/test_agent_servicer.py` 保持全绿
- 新加一个 test：mock provider 发 tool_call → 测 client 发 ToolResult → 验证 loop 进入第二轮 provider 调用

### S1.T2 — Async plugin /plugin-callback 真路由（2 天）

**范围**：`rust/crates/corlinman-gateway/src/routes/plugin_callback.rs`, `corlinman-plugins/src/async_task.rs`

1. `async_task.rs` 维护 `DashMap<task_id: String, oneshot::Sender<PluginOutput>>`
2. `RegistryToolExecutor` 遇到 `PluginOutput::AcceptedForLater { task_id }` 时：注册 oneshot receiver，等待（带 timeout 5 分钟）
3. `/plugin-callback/:task_id` POST 接受 JSON body → 查 map → `sender.send()` 唤醒等待
4. 超时自动清理 + 返 `{is_error: true, code: "timeout"}`

**验收**：
- 新集成测试：写一个 async Python plugin（返 task_id 后 2s 内 HTTP POST callback）→ E2E 通过
- metric `corlinman_plugin_execute_total{plugin,status="async_complete"}` 有 1

### S1.T3 — ModelRedirect 真生效（0.5 天）

**范围**：`rust/crates/corlinman-gateway/src/routes/chat.rs`

收到请求时先用 `config.models.aliases` 把 `req.model` 映射成真正的 provider model id。

**验收**：单测：配置 `aliases = { "smart": "claude-opus-4-7" }`，发 `model: "smart"` 的请求，断言 gateway 透给 Python 的 `ChatStart.model == "claude-opus-4-7"`。

### S1.T4 — Session 上下文保持（1.5 天）

**范围**：`rust/crates/corlinman-core/src/session.rs` (新建), `corlinman-gateway` 集成

1. 新 `SessionStore` trait：`get(key) / append(key, message) / delete(key)`
2. 实现 `SqliteSessionStore`（`data_dir/sessions.sqlite`）
3. Gateway 在请求进入时 prepend 历史 messages 给 ChatRequest，响应结束时追加 assistant message
4. session_key 由 ChannelBinding 或客户端显式传入

**验收**：
- 发两条对话（间隔 2 秒），第二条能引用第一条内容
- SQLite 表有 `session_key, seq, role, content, ts` 索引

---

## 4. Sprint 2 — M3 完整化（2 FT 周）

**目标**：service plugin、Docker sandbox、审批闸、hot reload 全部落地，M3 达到 90%。

### S2.T1 — Service plugin（gRPC 长驻）（3 天）

**范围**：`rust/crates/corlinman-plugins/src/runtime/service_grpc.rs`, `corlinman-gateway/src/services/plugin_supervisor.rs`

1. Gateway 启动时扫 `plugin_type == "service"` 的 manifest → spawn 子进程 + 传 `CORLINMAN_PLUGIN_ADDR=/tmp/corlinman-plugin-<name>.sock`
2. 插件作为 `PluginBridge` gRPC server 监听该 UDS
3. Gateway 注册 tonic client 到 registry
4. RegistryToolExecutor 对 service 插件走 gRPC 而非 stdio
5. 插件崩溃自动重启（带 backoff）

**验收**：E2E test 写一个 service plugin（Python grpc.aio），gateway 自动起它 + 发 ToolCall + 收 ToolResult → byte-identical。

### S2.T2 — Docker sandbox 真接（2.5 天）

**范围**：`rust/crates/corlinman-plugins/src/sandbox/docker.rs`, `runtime/jsonrpc_stdio.rs`

1. Manifest `sandbox` 非空时，用 `bollard::container::Config` 包装：
   - `memory` → `host_config.memory`
   - `cpus` → `host_config.nano_cpus`
   - `read_only_root` → `host_config.readonly_rootfs`
   - `cap_drop` → `host_config.cap_drop`
   - `network` → `host_config.network_mode`
   - `binds` → `host_config.binds`
2. stdio 改走容器 stdin/stdout via `docker attach`
3. OOM 触发 metric `corlinman_plugin_execute_total{plugin,status="oom"}`

**验收**：
- 测试插件设 `memory=128m` + 分配 500MB → 被 OOM kill
- 测试 `network=none` → 插件无法 DNS 解析
- 测试 `read_only_root` → 插件写 `/tmp` 失败但写 `binds` 挂载 OK

### S2.T3 — 审批闸完整（2.5 天）

**范围**：`rust/crates/corlinman-gateway/src/middleware/approval.rs`, `routes/admin/approvals.rs`, `corlinman-vector/src/sqlite.rs`（加 `pending_approvals` 表）

1. SQLite 表 `pending_approvals(id, session_key, plugin, tool, args_json, requested_at, decided_at, decision)`
2. Middleware 拦 `ToolCall` → 查 `config.approvals.rules` → 若 `mode=prompt`，写 pending + SSE push + 等 `ApprovalDecision`
3. `/admin/approvals` endpoints：`GET`（列队列）、`POST /:id/decide`（approve/deny）
4. UI `(admin)/approvals/page.tsx` 接真 API

**验收**：
- 配置 `mode=prompt` 规则 → curl chat → SSE 收 `awaiting_approval` → curl decide approve → loop 续
- 超时（5 分钟默认）→ 自动 deny + metric `corlinman_approvals_total{decision="timeout"}`

### S2.T4 — Plugin hot reload（1 天）

**范围**：`rust/crates/corlinman-plugins/src/registry.rs`

1. `notify` crate watch `plugin_dirs`
2. 文件改动 → debounce 500ms → 重新 discover 该子目录 + 更新 registry（Arc<RwLock>）
3. 正在执行的 plugin 不打断，新请求用新 manifest
4. 删除 manifest → service plugin 优雅停

**验收**：
- 运行时改 manifest 的 `capabilities.tools`，60s 内 `corlinman plugins list` 反映新 tool
- Service plugin 删 manifest 后 30s 内子进程退出

---

## 5. Sprint 3 — M4 RAG 完整化（2 FT 周，可 ∥ S2）

### S3.T1 — IndexCache LRU unload（1 天）

**范围**：`rust/crates/corlinman-vector/src/usearch_index.rs`

`DashMap<String, Arc<RwLock<UsearchIndex>>> + DashMap<String, Instant>` 跟踪 last-used；后台 task 每 10 分钟扫一次，> 2h 未用的 unload（save 后 drop in-memory）。

**验收**：单测 mock Instant，验证超时后 map 里对应条目不存在；metric `corlinman_vector_index_evictions_total`。

### S3.T2 — Migration registry（1.5 天）

**范围**：`corlinman-vector/src/migration.rs`

1. `MigrationScript { from, to, up, down }` trait
2. 启动时读 `kv_store.schema_version`，迭代 scripts 直到 target
3. 失败 rollback（调 down）
4. `.usearch` header probe + convert-on-mismatch（如果 header 改过）

**验收**：
- 跑 `schema_version=0` 的 SQLite → 启动自动迁到 `version=2`
- 故意 inject 坏 migration → rollback 后原 version 不动

### S3.T3 — Duplicate upsert + save debounce（1 天）

**范围**：`corlinman-vector/src/usearch_index.rs`

1. `upsert(key, vec)` = `remove(key) ? + add(key, vec)`
2. Save debounce：标记 dirty，500ms 内多次 upsert 合并成一次 `save()`
3. graceful shutdown 前 flush

**验收**：压测 1000 条并发 upsert → 最后 count 正确 + save 调用 ≤ 10 次。

### S3.T4 — Tag filter 下推到 FTS5（1 天）

**范围**：`corlinman-vector/src/hybrid.rs`

当 RagQuery 带 `tag_filter` 时：BM25 侧 `WHERE chunks.id IN (SELECT chunk_id FROM tags ...)`；HNSW 侧 post-filter（因为 usearch 不支持谓词）。

**验收**：单测：10 个 chunk 分 2 个 tag，带 filter 查询 → 只返该 tag 的 chunk。

### S3.T5 — Vector CLI（1 天）

**范围**：`corlinman-cli/src/cmd/vector.rs`

子命令：
- `corlinman vector stats` — 总 chunks/files/tags + 索引大小
- `corlinman vector query "<text>" [-k 10] [--tag <name>]` — 直接查
- `corlinman vector rebuild [--source <dir>] [--confirm]` — 从 knowledge 原文重建
- `corlinman vector export / import`（1.1 做）

**验收**：`corlinman vector stats` 输出 JSON 可 grep。

### S3.T6 — Cross-encoder rerank（1.5 天，可选）

**范围**：`corlinman-vector/src/rerank.rs`, Python `corlinman_embedding/rerank_client.py`

1. 配置 `[rag.rerank] model = "bge-reranker-v2-m3"` 开启
2. `hybrid.search` 拿 top_k*3 → gRPC 调 Python rerank → 按新 score 排序 → 截 top_k
3. 缺 API key 时 graceful fallback（skip rerank，记 warn）

**验收**：单测：mock rerank 反转排序，验证最终结果顺序变。

---

## 6. Sprint 4 — M5 Channels 增强（1 FT 周）

### S4.T1 — 多模态 segments（2 天）

**范围**：`corlinman-channels/src/qq/{onebot,message}.rs`, `corlinman-gateway-api/src/lib.rs`

1. `InternalChatRequest.attachments: Vec<Attachment>`（新字段）
2. `Attachment { kind: "image"|"audio"|"file", url: String, mime: String, bytes: Option<Bytes> }`
3. QQ `MessageSegment::Image` → 下载 url → base64 + gRPC 传 Python
4. Python provider 接 multimodal（anthropic content block type `image`）

**验收**：E2E 测试 mock gocq 发图 → gateway → Python anthropic multimodal → 文本响应。

### S4.T2 — 速率限制（1 天）

**范围**：`corlinman-channels/src/router.rs`

`per_group + per_sender` token bucket：
- 配置 `[channels.qq.rate_limit] group_per_min=20, sender_per_min=5`
- 超限：静默 drop + metric `corlinman_channels_rate_limited_total{channel,reason}`

**验收**：单测 21 条消息 1 分钟，只 20 条进 dispatch。

### S4.T3 — ChannelBinding 回填（0.5 天）

**范围**：`corlinman-channels/src/router.rs`, `corlinman-gateway-api/src/lib.rs`

`InternalChatRequest.binding: Option<ChannelBinding>` → proto `ChatStart.binding` 真填。

**验收**：Python servicer 验证收到 binding 字段。

### S4.T4 — Telegram 适配器（1.5 天，选做）

**范围**：`corlinman-channels/src/telegram/`（新模块）

用 `teloxide` crate；复用 `ChannelRouter` trait；`config.channels.telegram` 新段。

**验收**：mock Telegram API 测试，支持群/私聊 text 消息。

---

## 7. Sprint 5 — M6 认证 + config 热重载（1.5 FT 周）

### S5.T1 — Login 页 + session cookie（2 天）

**范围**：`rust/crates/corlinman-gateway/src/{routes/admin/auth.rs,middleware/admin_auth.rs}`, `ui/app/login/page.tsx`, `ui/app/(admin)/layout.tsx`

1. `POST /admin/login` 验 username+password → gen session token → `DashMap<token, {user, created_at, last_used}>` + set cookie `corlinman_session=<token>` HttpOnly Secure
2. `admin_auth` middleware 支持 cookie 或 basic（basic 保留给 CLI/Prometheus scrape）
3. `POST /admin/logout` 删 token
4. UI login 页表单 + 登录成功跳 `/plugins`
5. `(admin)/layout.tsx` 未登录 redirect `/login`

**验收**：Playwright test：访问 `/plugins` 未登录 → 被跳 `/login` → 登录后能看列表。

### S5.T2 — `POST /admin/config` live reload（1.5 天）

**范围**：`routes/admin/config.rs`, `AppState` 的 `config: Arc<ArcSwap<Config>>`

1. `POST /admin/config` 接 TOML body → parse → validate → 若全 ok `ArcSwap::store(new)`
2. 订阅者（channels / scheduler / plugin discovery）通过 `config.load()` 每次读都是快照，自然 pick up 新配置
3. 部分字段改动需要重启（`server.port` / `channels.qq.enabled`），validator 识别并返 `requires_restart: true`

**验收**：运行时 POST 改 `models.default` → 新请求生效，不重启。

### S5.T3 — `/admin/logs/stream` SSE（2 天）

**范围**：`routes/admin/logs.rs`, `corlinman-gateway/src/tracing_broadcast.rs`

1. 自定义 `tracing::Layer`，把每条 event 的 JSON 塞进 `tokio::sync::broadcast::Sender<LogRecord>`
2. `/admin/logs/stream` SSE 订阅（每客户端独立 `Receiver`）
3. 支持 URL param `level=info&subsystem=gateway` filter
4. UI `(admin)/logs/page.tsx` 接真 SSE

**验收**：curl `/admin/logs/stream` 能收到实时 JSON 日志流。

### S5.T4 — Plugin approval UI（1 天）

**范围**：`ui/app/(admin)/approvals/page.tsx`

1. Poll `GET /admin/approvals` + SSE 通知（复用 logs broadcast）
2. Approve/Deny 按钮 → `POST /admin/approvals/:id/decide`
3. 历史记录 tab（已决定的）

**验收**：手动流程：触发 prompt 工具 → UI 弹条目 → 点 approve → loop 续。

---

## 8. Sprint 6 — M6 Admin UI 真联调（2 FT 周，可 ∥ S7）

### S6.T1 — RAG 管理页（2 天）

**范围**：`ui/app/(admin)/rag/page.tsx`, `routes/admin/rag.rs`

- `GET /admin/rag/stats` - 总 chunks/files/tags + 索引状态
- `GET /admin/rag/query?q=...&k=10` - 调试查询，返 RagHit 列表
- `POST /admin/rag/rebuild` - 触发异步重建 job
- UI：stats 卡 + 查询调试框 + rebuild 按钮（确认对话框）

### S6.T2 — Channels 管理页（1.5 天）

**范围**：`ui/app/(admin)/channels/qq/page.tsx`, `routes/admin/channels.rs`

- `GET /admin/channels/qq/status` - 连接状态 + 最近 10 条消息
- `POST /admin/channels/qq/reconnect` - 手动断连重连
- `POST /admin/channels/qq/keywords` - 更新 `group_keywords`
- UI：连接灯（green/yellow/red）+ 关键词表格编辑 + 重连按钮

### S6.T3 — Scheduler 管理页（1.5 天）

**范围**：`ui/app/(admin)/scheduler/page.tsx`, `routes/admin/scheduler.rs`

- `GET /admin/scheduler/jobs` - cron 列表 + 下次触发时间
- `POST /admin/scheduler/jobs/:name/trigger` - 手动触发
- `GET /admin/scheduler/history` - 最近 100 次执行结果
- UI：jobs 表格 + 手动触发按钮 + 历史 modal

### S6.T4 — Config 编辑页（2 天）

**范围**：`ui/app/(admin)/config/page.tsx`

用 Monaco editor 真实装（`@monaco-editor/react`）：
- 左侧分段（server/admin/providers/.../logging）导航
- 右侧 TOML 编辑器 + JSON Schema 提示（从 `/admin/config/schema` 拉）
- 底部 Save 按钮 → `POST /admin/config` → 成功 toast + 失败显示 issues 列表

### S6.T5 — Models 路由页（1 天）

**范围**：`ui/app/(admin)/models/page.tsx`, `routes/admin/models.rs`

- `GET /admin/models` - 已配 providers + aliases 表
- `POST /admin/models/aliases` - CRUD aliases
- UI：provider 列表（带 enabled toggle）+ alias 表

### S6.T6 — Plugin invoke 按钮 + Agent Monaco editor（2 天）

**范围**：`ui/app/(admin)/plugins/[name]/page.tsx`, `ui/app/(admin)/agents/[name]/page.tsx`

- 插件详情页加 "Test invoke" 表单（按 tool schema 渲染字段）→ `POST /admin/plugins/:name/invoke`
- Agent 详情页 Monaco 编辑 frontmatter + 正文 → `POST /admin/agents/:name`

### S6.T7 — Playwright admin E2E（1 天）

**范围**：`ui/tests/e2e/admin-full.spec.ts`

覆盖：login → plugins 列表 → plugin detail → agent 编辑 → config 保存 → logout。

---

## 9. Sprint 7 — M7 可观测性完整（1.5 FT 周，可 ∥ S6）

### S7.T1 — OTel SDK + OTLP exporter（2 天）

**范围**：Rust `corlinman-gateway/src/telemetry.rs`, Python `corlinman_server/telemetry.py`

1. Rust：`opentelemetry` + `tracing-opentelemetry` + `opentelemetry-otlp`，`OTEL_EXPORTER_OTLP_ENDPOINT` env 控
2. Python：`opentelemetry-api` + `opentelemetry-exporter-otlp`，structlog bind span_id/trace_id
3. gRPC metadata 透传 `traceparent`

**验收**：起 jaeger + corlinman，发一个 /v1/chat/completions，在 jaeger 看完整 trace 跨 Rust/Python。

### S7.T2 — Grafana dashboard JSON（1 天）

**范围**：`ops/dashboards/corlinman.json`

覆盖 7 metric families：panels 包含 QPS / latency p50/p99 / tool call rate / backoff / inflight / RAG query stages / plugin exec duration heatmap。

**验收**：`grafana` + `prometheus` compose profile 起来，导入 JSON，看到实时数据。

### S7.T3 — 三端真埋点（1.5 天）

**范围**：`corlinman-plugins::runtime` + `corlinman-agent-client` + `corlinman-vector`

- Plugin：execute 前后测 duration + status 标签
- Agent-client：retry 每次 `BACKOFF_RETRIES{reason}` inc；stream 开 `AGENT_GRPC_INFLIGHT++`，结束 `--`
- Vector：`VECTOR_QUERY_DURATION{stage="hnsw"|"bm25"|"fuse"|"rerank"}` observe

**验收**：跑 10 个请求 → curl /metrics 看数据分布合理。

### S7.T4 — Doctor 扩到 20+ checks（2 天）

**范围**：`corlinman-cli/src/cmd/doctor/checks/`

新增：`manifest_duplicates` / `agent_grpc_ping` / `provider_https_smoke` / `disk_space` / `log_rotation` / `docker_daemon` / `python_subprocess_health` / `memory_usage` / `startup_time` / `scheduler_next_triggers` / `pending_approvals_overflow` / `broken_symlinks`.

**验收**：每个 check 带 1-2 个单测；总 check 数 ≥ 20；空环境跑 `corlinman doctor` 用时 < 5 秒。

### S7.T5 — /health 真实装（0.5 天）

**范围**：`corlinman-gateway/src/routes/health.rs`

当前只返 `checks: []`。接入实际 check：config / agent_grpc / sqlite / usearch / plugin_registry / channels.qq。整体 status 取 worst。

**验收**：kill Python subprocess → /health 马上 `unhealthy`，restart 后恢复。

---

## 10. Sprint 8 — M8 1.0 Release（2 FT 周）

### S8.T1 — CHANGELOG + release notes（0.5 天）

- 新建 `CHANGELOG.md`（keep-a-changelog 格式）
- 写 1.0.0 section：所有主要 feature / breaking changes / 已知 issue

### S8.T2 — QA scenarios 8 个（3 天）

**范围**：`qa/scenarios/*.yaml`

- `chat-nonstream.yaml` / `chat-stream.yaml` / `toolcall-loop.yaml` / `plugin-sync-echo.yaml` / `plugin-async-callback.yaml` / `rag-hybrid-retrieval.yaml` / `onebot-echo.yaml` / `fresh-install.yaml`

每个 scenario 带：前置 fixture、操作步骤、期望断言、清理。用 `corlinman qa run` 执行。

### S8.T3 — Docker image build + push（1 天）

- `docker build -t ghcr.io/ymylive/corlinman:0.1.0 -f docker/Dockerfile .`
- `docker build -t ghcr.io/ymylive/corlinman:0.1.0-ml --build-arg FEATURES=ml ...`
- 测试 `docker run` + `curl /health`
- push ghcr.io（需 GITHUB_TOKEN）

### S8.T4 — 安装录屏（0.5 天）

fresh Ubuntu 22.04 VM → `curl get.corlinman.sh | bash`（新做一个一键脚本）→ `corlinman onboard` → 发请求 → 看 UI → 加一个插件 → 发第二请求调用插件。30 分钟完整录屏，上传到 Asciinema。

### S8.T5 — Performance baseline bench（1 天）

`corlinman qa bench` 跑标准负载：
- `/v1/chat/completions` p50/p99（mock provider）
- RAG hybrid p50/p99（10k chunks fixture）
- Plugin exec roundtrip p50/p99（echo plugin）
- 冷启 /health ≤ 4s

数据落 `docs/perf-baseline-1.0.md`，以后 PR 若退化 >20% 挂 CI。

### S8.T6 — README + Quickstart（0.5 天）

- 顶层 `README.md` 加 "30-second quickstart" 段（docker one-liner）
- 加 badges（CI status / version / license）
- 加 screenshot（UI dashboard）

### S8.T7 — GitHub release tag v0.1.0（0.5 天）

- `git tag -a v0.1.0 -m "1.0.0"`
- `git push origin v0.1.0`
- GitHub release page：upload CHANGELOG 摘要 + release notes + Docker image links + 录屏链接

### S8.T8 — 1.0 发布沟通（0.5 天）

- 发 blog post（或 README 精简版）
- 发中文社区（知乎 / Hacker News）
- 发英文（r/selfhosted / r/LocalLLaMA）

---

## 11. Post-1.0 P1 演进（估 6-10 FT 周）

### 生态扩展

- **Plugin SDK 官方包**：`@corlinman/plugin-sdk`（npm）/ `corlinman-plugin-sdk`（PyPI）/ `corlinman-plugin`（crates.io）提供 JSON-RPC 模板、schema helpers、test harness
- **Plugin registry/marketplace**：中央 JSON 目录，`corlinman plugins install <name>` 从 URL 拉 + verify signature
- **MCP (Model Context Protocol) 兼容层**：把 corlinman plugins 暴露为 MCP tools 给 Claude Desktop/Cursor
- **ACP bridge**：`corlinman acp` stdio 桥接 Zed/Claude Code
- **OIDC 登录**：替代 basic auth，接 Auth0 / Keycloak / Google Workspace
- **Audit log**：SQLite `audit_log` 表（谁何时调用什么工具、改了什么配置）

### 质量门完整

- **Boundary 测试**：CI 强制跑 `cargo-deny` + `importlinter` 层序
- **Live-lane 三档**：`cargo test --features live-llm/live-transport` 每周跑（预算控制）
- **24h soak test**：客户端每 500ms 杀连接，验证零僵尸 worker
- **Fuzz corpus**：`cargo-fuzz` nightly 10min/parser
- **性能回归 CI**：`corlinman qa bench` 对比 baseline，>20% 挂

### 协议扩展

- **WebSocket chat endpoint**：除 SSE 外提供 WS（有些客户端偏好）
- **Batch API**：OpenAI `/v1/batches` 兼容
- **Embedding `/v1/embeddings`** endpoint 真生效
- **Files API**：multimodal 上传 → `data_dir/files/`
- **Circuit breaker**：upstream 连续失败短路

### 运维扩展

- **Config 文件 watcher**（替代手动 POST）
- **Backup / restore**：`corlinman data backup/restore`
- **Cost tracking**：按 provider/model/user 记 token 消耗 + dashboard
- **Rate limit** 中间件：per-ip / per-token + Redis 分布式

---

## 12. Post-1.0 P2 长期（估 ≥15 FT 周）

### 架构升级

- **Multi-tenant**：单实例多用户隔离（data_dir per tenant + RBAC）
- **Distributed mode**：多 gateway 实例 + Redis/Postgres 共享状态
- **SQLite → PostgreSQL** 迁移路径（大规模场景）
- **External vector DB**：Qdrant / Milvus 后端（取代/旁路 usearch）
- **Scheduler 升级**：复杂 DAG 场景走 Temporal / Airflow

### 高级 RAG

- **Cross-encoder rerank**：真接入 bge-reranker / cohere rerank
- **自动参数调优**：HNSW ef / RRF k / BM25 权重按 query 自适应
- **Multimodal embedding**：图/音向量化 + 混合检索
- **Graph RAG**：知识图谱 + 结构化检索
- **Agentic RAG**：多轮检索 + self-critique

### 客户端生态

- **VS Code extension**：内嵌 chat + RAG 浏览
- **JetBrains plugin**：同上
- **Tauri desktop app**（macOS/Windows/Linux）：轻量桌面客户端
- **Tauri mobile**（iOS/Android）：app 版
- **Canvas 渲染器**：LLM 返 React/HTML 组件，scoped iframe 执行
- **Voice**：Whisper STT in / edge-tts TTS out

### 社区与品牌

- **docs 站点**：`docs.corlinman.dev` (mdBook 或 Nextra)
- **示例 gallery**：开源 10+ 精品插件（计算器 / 搜索 / 截图 / 日历 / 邮件 / PDF / Excel / Git / Docker）
- **Contributor credit**：plugin 原作者 + 维护者名单
- **中文社区推广**：知乎 / B 站 / 小红书 tech 内容
- **英文社区**：r/selfhosted / r/LocalLLaMA / HN Show

---

## 13. 验收门禁（每 sprint 结束前必须通过）

所有 sprint 结束前，本地运行：

```bash
cd /Users/cornna/project/corlinman
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

**10 条必须全过**才能合 main。

---

## 14. 派 agent 节奏建议

按 sprint 内 task 数量 / 独立性，推荐并行度：

| Sprint | 推荐并行 agent 数 | 备注 |
| --- | --- | --- |
| S1 | 3-4 | T1/T2/T3/T4 基本独立 |
| S2 | 2-3 | T1+T2 可以并行（sandbox 不依赖 service）；T3+T4 串行在 T1 后 |
| S3 | 3-4 | 6 个 task 高独立 |
| S4 | 2-3 | 多模态和速率限制可并行 |
| S5 | 2 | T1 做 auth 全链路，T2+T3 动 admin 路由；T4 依赖 T1 |
| S6 | 3-4 | 6 个 UI 页各一个 agent |
| S7 | 2-3 | OTel 和 doctor/埋点独立 |
| S8 | 1-2 | 主串行；但 QA scenarios 8 个可并行 |

---

## 15. 更新约定

- 每完成一个 task，勾掉 checkbox + 在 `docs/milestones.md` 里更 milestone 完成比
- 每完成一个 sprint，在 `CHANGELOG.md` 加一段（M8 后强制）
- roadmap.md 不追加"已完成"清单——用 `docs/milestones.md` 存历史
- 这份 roadmap 只描述**计划**，实况读 `milestones.md`
