# Runbook

给自己部署 corlinman 的用户和运维工程师用的"出了问题看这里"手册。每条都是"一句问题 + 一段
解决"，按遇到频率从高到低排。

前置：你已经运行 `corlinman onboard` 完成了初装。

## 1. `corlinman doctor` 报问题该怎么修

`corlinman doctor` 分模块运行检查（config / upstream / manifest / channels / vector / scheduler）。
每个 check 输出 `OK / WARN / FAIL` + 简短 hint。

- **FAIL `config.toml` 解析失败**：看 hint 里的 key 和行号，通常是引号不匹配或缩进错误。
- **FAIL `upstream.anthropic` 429 / 5xx**：provider 不可达或 key 失效。`echo $ANTHROPIC_API_KEY` 确认环境变量真的注入进容器。
- **WARN `manifest.duplicates`**：发现同名 manifest 有多条；`corlinman plugins inspect <name>`
  看所有候选，删掉不要的。
- **WARN `channels.qq.gocq_heartbeat_missing`**：gocq 连上了但 30s 无心跳。多半 gocq 端异常，
  重启 gocq。
- **FAIL `vector.usearch.open`**：见第 6 条"RAG 检索不对"。

每一类 FAIL 都有对应的 run subcommand 做 deep-dive，如 `corlinman doctor --module upstream -v`。
`corlinman doctor --json` 输出结构化结果，适合 CI/监控吃。

## 2. `/health` 返回 degraded

`curl http://localhost:6005/health` 返回结构：
```json
{
  "status": "degraded",
  "checks": [
    {"name": "config", "status": "ok"},
    {"name": "agent_grpc", "status": "ok"},
    {"name": "sqlite", "status": "ok"},
    {"name": "usearch", "status": "warn", "detail": "index file mtime > 24h stale"},
    {"name": "plugin_registry", "status": "ok"},
    {"name": "channels.qq", "status": "fail", "detail": "ws disconnected"}
  ]
}
```

整体 status 取 worst：任何 `fail` → `unhealthy`；任何 `warn` 无 fail → `degraded`。排查
顺序：先看 `fail` 条目的 `detail`，基本能直接告诉你去哪查；再看 `warn`，通常可容忍。

外部健康探针建议只认 `unhealthy`（降流量），不认 `degraded`（继续吃流量，但告警）。

## 3. 用 `request_id` + `trace_id` 关联 Rust ↔ Python

每个请求在进 gateway 时生成 `request_id`（UUID v4），`traceparent` header 生成或继承 W3C trace
context 的 `trace_id`。Rust 侧通过 `tracing::info_span!` 注入，Python 侧通过
`structlog.contextvars` 注入，**字段名两端一致**。

排查流程：
```bash
# 1. 客户端报错，拿到 request_id（客户端应当日志它，如果没日志看响应 header X-Request-Id）
export RID=req_abc123

# 2. 在 gateway 日志捞
docker logs corlinman 2>&1 | grep "request_id=$RID"

# 3. 拿到 trace_id 后捞 Python 日志
export TID=0af7651916cd43dd8448eb211c80319c
docker logs corlinman 2>&1 | grep "trace_id=$TID"
# Python 日志也在同一个容器 stdout（gateway 汇聚）
```

`subsystem` 字段告诉你这条日志来自哪：`gateway.routes.chat` / `agent-client` /
`plugins.runtime` / `python.agent.reasoning_loop` / `python.providers.anthropic`。

## 4. 插件被 OOM kill

症状：Agent 调用某工具，gateway 返 "plugin execution failed"。

```bash
# 查指标
curl http://localhost:6005/metrics | grep corlinman_plugin_execute_total
# 看有没有 {plugin="X",status="oom"} 这个 series 且计数在涨
```

修复：
- 临时：在 `~/.corlinman/plugins/<name>/manifest.toml` 的 `sandbox.memory` 从 `"256m"` 调到 `"512m"` 或 `"1g"`，manifest watcher 60s 内自动 reload。
- 长期：看插件代码是不是有内存泄漏（未释放的 buffer）；或数据规模超预期。

## 5. upstream LLM 429，退避是否起效

gateway 的 `corlinman-agent-client::retry` 按 `DEFAULT_SCHEDULE = [5s, 10s, 30s, 60s]` 指数
退避。metric 验证：
```bash
curl http://localhost:6005/metrics | grep corlinman_backoff_retries_total
# corlinman_backoff_retries_total{reason="rate_limited"} 34
# corlinman_backoff_retries_total{reason="upstream_5xx"} 7
```

`reason` 字段取值见 `corlinman-core::error::FailoverReason` enum：`rate_limited` /
`upstream_5xx` / `upstream_timeout` / `upstream_invalid_response` / `network`。

如果 `rate_limited` 计数猛涨但最终请求还是失败（`corlinman_http_requests_total{status="5xx"}`
也涨），说明 4 档退避也扛不住，需要：
- 降低并发（客户端侧或在 gateway 加 rate limit，M7 引入）
- 切换到备用 provider：`ModelRedirect.json` 配好 fallback chain
- 临时提升 provider quota

## 6. RAG 结果不对，usearch 重建

症状：Agent 回答明显漏掉 dailynote 里有的内容、或检索出无关旧笔记。

步骤：
1. 先确认是检索问题还是 LLM 没读上下文：`CORLINMAN_LOG_LEVEL=debug`，搜
   `subsystem=python.agent.context_assembler` 看实际注入了哪些 hit。
2. 如果检索本身差：
   ```bash
   corlinman vector stats                  # 看文档数、索引 size、上次更新时间
   corlinman vector query "你的问题" -k 10  # 直接查索引
   ```
3. 确定索引过时或损坏，重建：
   ```bash
   corlinman vector rebuild --source ~/.corlinman/knowledge --confirm
   ```
   这会：新建 `.usearch.new` → 重跑 embedding → 原子 rename 到 `.usearch`。期间 gateway 仍用老索引。完成后读新索引。如出错原文件未动。
4. 如果是 `config.toml` 的 `[rag]` 段参数调错导致 RRF 融合偏移，`corlinman config diff` 对比 default。

## 7. QQ bot 重连循环

症状：`subsystem=channels.qq` 日志每几秒一条 `reconnecting...`。

排查顺序：
1. `curl /health` 看 `channels.qq` 是 `fail` 还是 `warn`。
2. 确认 gocq/Lagrange/NapCatQQ 端活着：看它自己的日志或 web 管理页。
3. 两边 WS URL 配对：`config.toml` 的 `[channels.qq] ws_url` 是不是正确指向 WS server。
4. 登录态：扫码过期会失败，重扫。
5. 确认没有两个 corlinman 实例在抢同一个 WS 连接。

## 8. 定时任务没触发

`corlinman-scheduler` 启动时把 `config.toml` 的 `[[scheduler.jobs]]` 里配的 cron job 注册到 `tokio-cron-scheduler`。排查：

1. Admin UI 的 `/admin/scheduler` 页看任务列表和下次触发时间。列表空的话 config 没读到。
2. 手动触发验证任务本身 OK：
   ```bash
   corlinman scheduler trigger <job-name>
   ```
3. 如果手动 OK 但定时不跑，检查时区：`TZ` 环境变量 + cron 表达式是否匹配。Docker 默认 UTC；
   国内用户通常要 `-e TZ=Asia/Shanghai`。
4. 日志搜 `subsystem=scheduler`，`level=warn` 以上的看有没有异常。

## 9. 优雅关机

corlinman 对 SIGTERM 的约定：**停接新请求 → 抽干 inflight（默认 5s） → 关 gRPC stream → flush
日志 → 退出码 143**。Docker 默认 `stop_grace_period=10s`，够用。

强制关停用：
```bash
docker kill -s SIGKILL corlinman
```

这会打断 inflight 请求（客户端看到连接断），紧急时才用。

**你的 compose**：设 `stop_grace_period: 15s`，留余量给 flush 和 Python 子进程收尾。

**退出码含义**：
- `0` —— 正常 shutdown
- `143` —— SIGTERM 正常处理
- `137` —— SIGKILL 强停
- 其他非 0 —— 异常崩溃，看日志最后几行 panic 信息

## 9.5 SSE 响应被 Nginx / 反代 buffer，客户端看到"憋一阵再一起下来" (added 2026-04-20)

症状：直连 gateway `:6005` 时 SSE 流畅，接入 Nginx / Traefik / 云厂商 LB 后客户端体验变成
"等几秒一次性返回一大段"。

根因：反代默认对 HTTP 响应开启 buffering，SSE 流被 buffer 吃掉了 per-event 边界。

**修法 1（gateway 侧，1.0.x 已内置）**：所有 `text/event-stream` 响应自动加
`X-Accel-Buffering: no` header。Nginx 识别此 header 跳过 buffering。若仍不生效检查 Nginx
是否 `proxy_pass_header X-Accel-Buffering` 开启（默认开）。

**修法 2（反代侧，稳妥）**：在 Nginx location 里显式配：
```nginx
location /v1/chat/completions {
    proxy_pass http://localhost:6005;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_http_version 1.1;
    chunked_transfer_encoding off;
}
```

**验证**：客户端看到第一个 `data:` event 的墙钟时间应 ≤ 200ms（upstream provider 已开流后）。
gateway 侧 metric `corlinman_chat_stream_duration_seconds` 的 first-byte 不会受反代 buffer
影响，但客户端肉眼能看到差异——这个差值就是反代 buffer 吃掉的 lag。

计划文件 §14 R9 对此有提及。

## 10. 升级新版本

最小停机升级（compose 场景）：
```bash
# 1. pull 新镜像
docker compose pull corlinman

# 2. 重启
docker compose up -d corlinman

# 3. 校验
corlinman doctor
curl http://localhost:6005/health
# 看客户端能否正常调用

# 4. 如有问题回滚
docker tag ghcr.io/<org>/corlinman:1.0.0 ghcr.io/<org>/corlinman:rollback
# 或在 compose 里暂时指定上一个版本号，docker compose up -d
```

**永远不要**跳过非 patch 版本升级（比如 1.0.x 直接跳到 1.2.0）。按 minor 顺序升，每次升完
跑 `corlinman doctor` 和一次真实请求。

**数据向后兼容**：1.x 任意版本的数据 1.x 任意版本都能读。2.0 会有一次 `corlinman migrate` 流程，届时补充 migration 文档。

## 延伸阅读

- `/metrics` 完整清单：计划文件 §9 可观测性
- 插件特定故障：[plugin-authoring.md §9 调试](plugin-authoring.md#9-调试)
- 哪层组件出问题去哪查：[architecture.md](architecture.md) §6 时序图
