# Architecture

本文给准备贡献代码的开发者用。读完你应能回答：消息从 HTTP 进来到 SSE 出去经过哪几个进程；一个新 Rust 模块该放哪个 crate；一个 proto 字段该加到哪个 service。

> **What's new since v1** — v0.2 adds four Rust crates (`corlinman-hooks`,
> `corlinman-skills`, `corlinman-wstool`, `corlinman-nodebridge`), one
> Python package (`corlinman-tagmemo`), seven admin UI pages
> (`/skills`, `/characters`, `/hooks`, `/playground/protocol`,
> `/channels/telegram`, `/nodes`, plus tagmemo/diary/canvas surfaces),
> a vector schema bump to v6 (hierarchical `tag_nodes` + `chunk_epa`
> cache), a manifest bump to v2 (`protocols` / `hooks` / `skill_refs`),
> and eight reserved placeholder namespaces. Full upgrade instructions
> live in [`migration/v1-to-v2.md`](migration/v1-to-v2.md).

**前置知识**：你看过顶层 [README.md](../README.md)，熟悉 async Rust 和 Python asyncio 的基本概念。

> 本文不重复计划文件里的决策、里程碑、风险。这些在
> `/Users/cornna/.claude/plans/openclaw-rust-python-corlinma-graceful-meerkat.md`
> 中单一事实来源，本文只描述"现在系统长什么样"。

## 1. 高层架构

```
                 +-----------------------------+
   Client -----> |     corlinman-gateway       | <-----  Next.js UI (REST + SSE)
 (HTTP/SSE)      |  (Rust, axum, listens 6005) |         /admin/*
                 +---+--------+--------+-------+
                     |        |        |
          gRPC bidi  |  gRPC  |  gRPC  |   JSON-RPC stdio / gRPC
   +-----------------+  +-----+  +-----+   +---------+
   |                 |  |     |  |     |   |         |
   v                 |  v     |  v     |   v         v
+-------------------+|+---+   |+---+   | +----------------+
| corlinman_agent   |||Emb|   ||Vec|   | | plugins        |
| (Python, reasoning|||bd |   ||tor|   | | (nodejs/py/    |
|  loop + provider) |||(py|   ||(rs|   | |  rust/bash,    |
+-------------------+|+---+   |+---+   | |  optional      |
                     |        |        | |  Docker sandbox)|
                     |        |        | +----------------+
                     v        v        v
              +-----------------------------+
              | upstream LLM providers      |
              | (Anthropic / OpenAI /       |
              |  Google / DeepSeek / ...)   |
              +-----------------------------+

 旁路：
   - corlinman-channels (QQ/OneBot v11 正向 WS)     -> gateway internal ChatRequest
   - corlinman-scheduler (tokio-cron-scheduler)     -> gateway AppState
```

客户端只需要知道 `:6005`。其他都是内部的。Python 进程是 gateway 通过 `Command::spawn` 拉起的子进程（单容器单进程树），通过 stdin/stdout 继承 + `/tmp/corlinman-py.sock`（UDS）上跑 gRPC。

## 2. 为什么 Rust + Python 分工

**Rust 擅长**：长连接 I/O（axum + hyper + tonic）、背压控制、取消/超时（`CancellationToken` + `tokio::select!`）、文件系统 watch（notify）、Docker 交互（bollard）、usearch FFI、日志结构化开销、SIGTERM 协议退出。网关和插件运行时全是这些，放 Rust。

**Python 擅长**：LLM provider SDK 的生态（anthropic / openai / google-genai 官方 SDK 在 Python 侧迭代最快）、sentence-transformers 和 torch（本地 embedding）、快速迭代 prompt 工程的脚本级表达力。Agent reasoning loop 和 provider 胶水放 Python。

**为什么用 gRPC 而不是 HTTP**：

1. **流式天然双向**：Agent 的 Chat 是 `stream ClientFrame ↔ stream ServerFrame`，REST/SSE 做不到真正的双向，需要 client 额外发 POST 回传 tool result，时序复杂
2. **schema 强类型**：proto 定义一次，Rust 和 Python 两端代码生成，不会漂移；HTTP/JSON 靠 docs 同步等于没同步
3. **取消语义**：gRPC 有内建 cancellation，客户端 drop stream 服务端立刻收到；裸 HTTP 要自己约定
4. **metadata 通道**：`traceparent` / `request_id` 走 metadata，不污染 payload

**不选其他**：MessagePack over UDS 要自写 framing；ZeroMQ 没 schema；REST + WebSocket 要维护两套。

## 3. Rust crate 图

所有 crate 在 `/Users/cornna/project/corlinman/rust/crates/`。workspace 清单在 `/Users/cornna/project/corlinman/Cargo.toml`。

```
                        +---------------------+
                        |   corlinman-core    |    (纯库，无 I/O)
                        |  config/error/      |
                        |  placeholder/       |
                        |  channel_binding/   |
                        |  cancel/backoff     |
                        +----------+----------+
                                   ^
             +---------------------+---------------------+
             |                     |                     |
   +---------+--------+   +--------+--------+   +--------+---------+
   | corlinman-proto  |   | corlinman-plugins|   | corlinman-vector |
   | (tonic-build out)|   | discovery/       |   | HNSW + BM25      |
   +---------+--------+   | runtime/sandbox  |   | RRF fusion       |
             ^            +---+----------+---+   +--------+---------+
             |                ^          ^                ^
   +---------+--------+       |          |                |
   | corlinman-agent- |<------+          |                |
   | client (gRPC)    |                  |                |
   +---------+--------+                  |                |
             ^                           |                |
             |                           |                |
   +---------+---------------------------+----------------+--+
   |                      corlinman-gateway                  |
   |     axum binary: routes / ws / middleware / state       |
   +----+------------------+--------------------+------------+
        ^                  ^                    ^
        |                  |                    |
   +----+-----+    +-------+-------+    +-------+---------+
   |corlinman-|    | corlinman-    |    | corlinman-cli   |
   |channels  |    | scheduler     |    | (main + doctor/ |
   |(QQ/WS)   |    |               |    |  onboard/etc)   |
   +----------+    +---------------+    +-----------------+
```

依赖方向自下而上。`corlinman-core` 谁都能依赖但它不依赖任何 crate；`corlinman-gateway` 是终点 binary，它依赖除 `corlinman-cli` 外的所有 crate；`corlinman-cli` 是另一个终点 binary，它也依赖 `corlinman-gateway`（doctor 会拉起简化版 gateway 跑 self-check）。

职责详单（一句话版）：

| crate | 一句话 |
| --- | --- |
| `corlinman-core` | 共享类型、`CorlinmanError` + `FailoverReason`、`combineAbortSignals`、backoff schedule |
| `corlinman-proto` | `tonic-build` 产物，`include_proto!` 暴露给其他 crate |
| `corlinman-gateway` | axum binary，`/v1/chat/completions` + `/admin/*` + `/health` + `/metrics` + WS |
| `corlinman-channels` | QQ/OneBot v11 正向 WS，`ChannelBinding → session_key` 路由 |
| `corlinman-plugins` | manifest-first 发现 + origin-ranked 去重 + JSON-RPC stdio + gRPC service + Docker 沙箱 |
| `corlinman-vector` | HNSW 索引（usearch）+ SQLite FTS5（BM25）+ RRF 融合 + 可选 rerank |
| `corlinman-agent-client` | gRPC client 包装（背压、重试、cancel 级联、分类错误） |
| `corlinman-scheduler` | `tokio-cron-scheduler` 封装 + `Job` trait，shutdown cascade |
| `corlinman-cli` | `corlinman` 主 binary：`onboard` / `doctor` / `plugins` / `config` / `dev` / `qa` |
| `corlinman-hooks` | 进程内 hook bus（`[hooks]` 配置），manifest `hooks=[...]` 订阅事件 |
| `corlinman-skills` | `skills/*.md` openclaw 风格加载器 + 注入到 system prompt |
| `corlinman-wstool` | 本地 WebSocket 工具总线（`[wstool]`），loopback 默认 |
| `corlinman-nodebridge` | Node.js worker bridge 监听器（`[nodebridge]`） |

## 4. Python package 图

所有包在 `/Users/cornna/project/corlinman/python/packages/`。workspace 清单在 `/Users/cornna/project/corlinman/pyproject.toml`。

```
              +---------------------+
              |   corlinman_grpc    |  (grpcio-tools 产物 + py.typed)
              +----------+----------+
                         ^
       +-----------------+-----------------+
       |                 |                 |
+------+---------+ +-----+-------+ +-------+---------+
| corlinman_     | | corlinman_  | | corlinman_      |
| providers      | | embedding   | | agent           |
| (anthropic/    | | (local pool | | (reasoning_loop |
|  openai/google/| |  + remote   | |  + context_     |
|  deepseek/qwen/| |  client)    | |  assembler +   |
|  glm)          | +------+------+ |  session +     |
| failover.py    |        |        |  approval_gate)|
+-------+--------+        |        +-------+--------+
        ^                 |                ^
        |                 |                |
        +-----------------+----------------+
                          |
                 +--------+---------+
                 | corlinman_server |  (grpc.aio server 入口)
                 | main.py          |
                 | middleware.py    |
                 | shutdown.py      |
                 +------------------+
```

职责详单：

| package | 一句话 |
| --- | --- |
| `corlinman_grpc` | grpcio-tools 生成 stub + `py.typed`，其他包从这里 import |
| `corlinman_providers` | `CorlinmanProvider` Protocol 和 per-vendor 实现 + registry + failover |
| `corlinman_agent` | `reasoning_loop.py`（自建，不依赖 LangChain）+ context_assembler + session |
| `corlinman_embedding` | 本地 `ProcessPoolExecutor` 绕 GIL，或走 remote embedding 服务 |
| `corlinman_server` | `grpc.aio.server()` 主入口、traceparent middleware、SIGTERM 143 |
| `corlinman_tagmemo` | EPA basis fitting + pyramid build，feeds `chunk_epa` cache（v0.2 起） |

统一规约：配置用 pydantic v2 strict，日志用 structlog + JSON，异常继承 `CorlinmanError`。

## 5. proto 服务速览

所有 `.proto` 在 `/Users/cornna/project/corlinman/proto/corlinman/v1/`。`tonic-build` 和 `grpcio-tools` 在各自的构建步骤消费同一份 IDL。

| service | 方向 | 作用 |
| --- | --- | --- |
| `common.proto` | 无 service，只有类型 | `Message` / `Role` / `Usage` / `TraceContext` / `ErrorInfo` / `ChannelBinding` / `FailoverReason` enum |
| `Agent` (agent.proto) | Rust client → Python server | **核心**。`rpc Chat(stream ClientFrame) returns (stream ServerFrame)`，承载 chat 流水线 |
| `LLMProvider` (llm.proto) | Python 内部 | `Chat` / `Complete`，provider 抽象 |
| `Embedding` (embedding.proto) | Rust client → Python server | `Embed(text)` / `EmbedBatch(stream)` |
| `Vector` (vector.proto) | Python client → Rust server（**反向**） | `Query(RagQuery) → RagResult` / `Upsert(stream)`；Python 侧 context_assembler 调 |
| `PluginBridge` (plugin.proto) | Python client → Rust server（**反向**） | `Execute(ToolCall) returns (stream ToolEvent)`；Python 收到 LLM 返回的 tool call 后调这里执行 |

**反向 gRPC**：gateway 同时是 Python 的 client（调 Agent/Embedding）和 server（注册 Vector/PluginBridge）。两种 service 共用同一个 tonic Server 实例监听 UDS。

**字段规范**：`args_json` / `result_json` / `payload_json` 一律用 `bytes` 零拷贝 JSON，不用 `google.protobuf.Any` 也不用 `Struct`——避免 proto runtime 反复解析 / 序列化。`traceparent` 和 `request_id` 走 gRPC metadata 而非 payload。

`agent.proto` 的 `ClientFrame` / `ServerFrame` oneof 设计参考计划文件 §6，本文不复述。

## 6. 关键跨进程流：`/v1/chat/completions` streaming

这是整个系统最热的路径，值得逐跳看一遍。细节见计划文件 §5.1，此处画时序图。

```
 Client         gateway              agent-client       Python agent     provider         plugin
   |                |                      |                  |              |              |
   | POST /v1/chat  |                      |                  |              |              |
   |--------------->|                      |                  |              |              |
   |                | auth + trace span    |                  |              |              |
   |                | model 路由解析       |                  |              |              |
   |                | CancellationToken 建  |                  |              |              |
   |                |--Chat(bidi open)---->|                  |              |              |
   |                |                      |--ClientFrame::-->|              |              |
   |                |                      |  Start(msgs,tools, session_key, |              |
   |                |                      |        placeholders, trace)     |              |
   |                |                      |                  | context_     |              |
   |                |                      |                  |  assembler   |              |
   |                |                      |                  |  (RAG 注入 / 占位符一次替换) |
   |                |                      |                  |-provider.chat_stream------->|
   |                |                      |<-ServerFrame::TokenDelta--------|              |
   |<------SSE------|                      |                  |              |              |
   |  data: {delta} |                      |                  |              |              |
   |                |                      |<-ServerFrame::ToolCall----------|              |
   |                |                      |  (OpenAI 标准 tool_calls JSON)  |              |
   |                | approval middleware  |                  |              |              |
   |                |  (config.toml)       |                  |              |              |
   |                |  若 prompt 模式:     |                  |              |              |
   |                |   SSE awaiting_approval                 |              |              |
   |                |   阻塞 oneshot       |                  |              |              |
   |                |--registry.execute--->|                  |              |              |
   |                |                      |              JSON-RPC stdio / gRPC              |
   |                |                      |                      |------------------------>|
   |                |                      |                      |<------------------------|
   |                |<-ToolResult(call_id, payload_json)-----------|                         |
   |                |--ClientFrame::ToolResult--------------->|                              |
   |                |                      |                  | provider 续 loop            |
   |                |                      |<-TokenDelta / ToolCall / Done------------------|
   |<------SSE------|                      |                  |                             |
   |  ...           |                      |                  |                             |
   |<------SSE------|                      |                  |                             |
   | data: [DONE]   |                      |                  |                             |
```

**背压**：Rust 用 `mpsc::channel(16)`，Python 用 `asyncio.Queue(maxsize=16)`。16 是个起始值，M2 做 SSE 基线测试时重新标定。

**Cancellation 全链路**：客户端断开 TCP → axum 的 `Request::canceled()` → `CancellationToken::cancel()` → `tokio::select!` 走另一分支退出 → gRPC stream 关闭 → Python 抛 `CancelledError` → `asyncio.timeout` 上下文退出 → provider client 的 aiohttp session `close()`。测试矩阵里有一个 1000 次 500ms 随机断链的 soak job 保证无僵尸。

**失败路径**：provider 报 429/5xx → Python `corlinman_providers.failover` 按 `FailoverReason` 分类 → 回 `ServerFrame::ErrorInfo{retryable=true}` → Rust `corlinman-agent-client::retry` 按 `DEFAULT_SCHEDULE = [5,10,30,60]s` 指数退避。超最后一档还不行直接 500 给客户端。

## 7. 数据与配置组织

corlinman 数据默认放 `~/.corlinman/`：

```
~/.corlinman/
├── config.toml                    # 主配置
├── agents/                        # Agent markdown + frontmatter
│   └── <name>.md
├── plugins/                       # 插件目录
│   └── <plugin-name>/
│       ├── manifest.toml
│       └── ...
├── knowledge/                     # RAG 知识库原文
│   └── <collection>/
│       └── *.md
├── vector/                        # 向量索引
│   ├── index.usearch
│   └── chunks.sqlite              # chunks + FTS5
└── logs/                          # rolling daily
    └── corlinman.log.YYYY-MM-DD
```

可用 `--data-dir` 或 `CORLINMAN_DATA_DIR` 覆盖。Docker 默认挂到 `/data`。

`config.toml` 分段示例：

```toml
[server]
port = 6005
bind = "0.0.0.0"

[admin]
username = "admin"
password_hash = "$argon2id$..."

# Providers are a free-form `BTreeMap<String, ProviderEntry>` — the table
# key is operator-chosen and the `kind` field selects the wire shape. The
# six legacy slot names (anthropic / openai / google / deepseek / qwen /
# glm) infer their kind for backwards compatibility; any other name must
# set `kind = "..."` explicitly. Full reference: docs/providers.md.
[providers.openai]
kind = "openai"
api_key = { env = "OPENAI_API_KEY" }
base_url = "https://api.openai.com/v1"
enabled = true

[providers.openrouter]
kind = "openai_compatible"
api_key = { env = "OPENROUTER_API_KEY" }
base_url = "https://openrouter.ai/api/v1"
enabled = true

[models]
default = "claude-sonnet-4-5"

[[approvals.rules]]
plugin = "file-ops"
tool = "file-ops.write"
mode = "prompt"

[channels.qq]
enabled = true
ws_url = "ws://127.0.0.1:3001"
self_ids = [123456789]
```

## 8. M0 / M1 现状 (updated 2026-04-20)

**M0 已完成**：

- 9 个 Rust crate 骨架（`corlinman-{core,proto,gateway,channels,plugins,vector,agent-client,scheduler,cli}`）
- 5 个 Python package（`corlinman_{grpc,providers,agent,embedding,server}`）
- 6 份 `.proto` 文件 + tonic-build / grpcio-tools 生成管道
- 14 个 Next.js App Router route 骨架
- 多阶段 Dockerfile（cargo-chef + uv + pnpm）
- CI（fmt + clippy + nextest + pytest + pnpm test）+ pre-commit hooks

**M1 已部分跑通**：

- Gateway `/health` 端点
- SIGTERM → 143
- core::placeholder 引擎（`{{namespace.name}}` 一次替换）
- Python grpc.aio server 起来
- Rust ↔ Python IPC：UDS `/tmp/corlinman-py.sock`，env `CORLINMAN_PY_SOCKET` 覆盖；TCP 回退 `127.0.0.1:50051`

**M1 仍 WIP**：`/v1/chat/completions` 非流式透传、provider failover 分类。

**M2–M8 尚未开工**，见计划文件 §11。

## 9. 可观测性现状 (updated 2026-04-20)

**日志**：Rust 侧 `tracing_subscriber` + JSON 输出到 stdout，Python 侧 `structlog` + `python-json-logger` 输出到 stdout（由 gateway 通过 `Command::spawn` 继承 Python 的 stdout/stderr 再汇聚到容器日志）。两端字段名对齐：`request_id` / `trace_id` / `subsystem` / `level` / `ts` / `msg`。

**traceparent 占位**：W3C `traceparent` header 目前作为字符串在 gRPC metadata 中透传，**尚未正式接入 OpenTelemetry export**（无 collector、无 span exporter）。M7 会正式接 OpenTelemetry SDK + OTLP exporter，届时 trace 可在 Jaeger/Tempo 上可视化。当前排查只能靠 `trace_id` 字符串关联跨进程日志（见 runbook §3）。

**metrics**：`/metrics` 端点 M1 期间仍是空 registry，M7 才正式填入计划文件 §9 列出的清单。

**Docker ENTRYPOINT**：`tini -- corlinman-gateway`，tini 负责转发信号；SIGTERM 传到 gateway 后级联关 Python 子进程（gateway 的 shutdown handler 向 Python UDS socket 写关闭帧后 `wait()` Python 进程）。

## Protocols reserved for device clients

The gateway ships one wire contract for future device-class clients
(iOS / Android / macOS / Linux / Electron):

- **NodeBridge v1** — WebSocket + JSON at `config.nodebridge.listen`
  (default `127.0.0.1:18788`). Registration + heartbeat + dispatch +
  telemetry. Implemented by the stub crate `corlinman-nodebridge`; no
  native client is built from this repo. See
  [`protocols/nodebridge.md`](protocols/nodebridge.md).

## 延伸阅读

- Provider 配置 reference + 14 种 `kind` 表 + 常见 recipe（Ollama/OpenRouter/SiliconFlow）：[providers.md](providers.md)
- 跨进程通道更多细节：`proto/corlinman/v1/agent.proto` 的注释（M0 写）
- 插件运行时的 trait 层次：[plugin-authoring.md](plugin-authoring.md)
- 每个 crate 的内部模块：该 crate 目录下的 `README.md`（M1 起每个 crate 维护）
- 一张图回答"一个请求花在哪"：`/metrics` 的 `corlinman_chat_stream_duration_seconds` histogram 加 `label=stage`（M7 引入）
- 当前里程碑进展表：[milestones.md](milestones.md)
- Canvas Host 协议（B5-BE1，`POST /canvas/session` / `POST /canvas/frame` / SSE `GET /canvas/session/:id/events`）：见 [openapi/canvas.yaml](openapi/canvas.yaml)，默认受 `[canvas] host_endpoint_enabled = false` 屏蔽。

## See also

操作面 doc（W5.2 新增）：

- [Quickstart](quickstart.md) — 60 秒首启动 + 默认密码轮换 + skip-to-mock 路径
- [Profiles](profiles.md) — 多 agent 隔离实例（persona + memory + skills + state）
- [Credentials](credentials.md) — provider key 管理页（EnvPage 风格）
- [Evolution & Curator](evolution-curator.md) — hermes-agent 自我进化机制移植
