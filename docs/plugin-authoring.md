# Plugin authoring

本文给想给 corlinman 写插件的开发者。读完你应能：写出一个能被 `corlinman plugins list` 列出并被 Agent 调用的插件；知道 manifest 每个字段什么意思；知道沙箱和审批怎么配。

**前置**：熟悉命令行和至少一门脚本语言（Python / Node / Rust / Bash 任一）。

## 1. 插件类型

corlinman 只有 **3 种插件类型**，对应 `manifest.toml` 里的 `plugin_type`：

| plugin_type | 协议 | 典型用途 | 何时用 |
| --- | --- | --- | --- |
| `sync` | JSON-RPC 2.0 over stdio | 一次调用返一次结果 | 默认选它（90% 场景） |
| `async` | JSON-RPC 2.0 stdio + HTTP callback | 首次返 `task_id`，任务后台跑完 POST 回 gateway | 单次超过 30s 的任务 |
| `service` | gRPC on `$CORLINMAN_PLUGIN_ADDR` | 长驻进程，gateway 启动时 spawn 并连入 | 需要持久化状态、fan-in 流量、反向调 gateway |

**绝大多数插件写 `sync`**。其他两种有协议扩展，本文主要讲 `sync`；`async` 在 §5、`service` 在 §6 各自说明。

## 2. Manifest schema

`manifest.toml` 放在 `~/.corlinman/plugins/<your-plugin>/` 下。完整 schema：

```toml
name = "my-plugin"
version = "0.1.0"
description = "Does something useful"
author = "Alice"
plugin_type = "sync"                  # "sync" | "async" | "service"

[entry_point]
command = "python"
args = ["main.py"]
# 工作目录自动设为 manifest 所在目录

[communication]
timeout_ms = 30000                    # sync/async 超时；service 忽略此项

[[capabilities.tools]]
name = "greet"
description = "Greet someone by name"

[capabilities.tools.parameters]
type = "object"
required = ["name"]

[capabilities.tools.parameters.properties.name]
type = "string"
description = "The person to greet"

[capabilities]
disable_model_invocation = false      # true 时禁止 LLM 主动触发；只能 admin/scheduler 调

[sandbox]
memory = "256m"
cpus = 0.5
read_only_root = true
cap_drop = ["ALL"]
network = "none"                      # "none" | "bridge" | "host"
binds = []                            # Docker -v 形式，如 ["/host:/container:ro"]

[meta]
last_touched_version = "0.1.0"
last_touched_at = "2026-04-20T10:00:00Z"
```

**字段解释**：

- `plugin_type` — 决定运行时协议：`sync`/`async` 走 stdio JSON-RPC；`service` 走 gRPC
- `entry_point.command + args` — `Command::spawn` 起子进程
- `communication.timeout_ms` — 硬超时 + `CancellationToken` 同时发信号
- `capabilities.tools[*]` — 对 LLM 暴露的工具清单，parameters 是标准 JSON Schema（OpenAI function calling 格式）
- `capabilities.disable_model_invocation` — 高危工具置 `true`；approval middleware 硬拒 LLM 触发
- `sandbox` — 留空则不沙箱；填了走 Docker（bollard 组装 HostConfig）
- `meta.last_touched_*` — UI 写回时自动填

## 3. Hello World

4 种语言的最小 `sync` 插件。

### 3.1 Python

`~/.corlinman/plugins/hello-python/manifest.toml`：

```toml
name = "hello-python"
version = "0.1.0"
plugin_type = "sync"

[entry_point]
command = "python3"
args = ["main.py"]

[[capabilities.tools]]
name = "greet"
description = "Greet a person"

[capabilities.tools.parameters]
type = "object"
required = ["name"]

[capabilities.tools.parameters.properties.name]
type = "string"
```

`main.py`：

```python
import json, sys

for line in sys.stdin:
    req = json.loads(line)
    if req.get("method") == "tools/call":
        name = req["params"]["arguments"].get("name", "world")
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"content": f"hello, {name}"}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
```

### 3.2 Node

`main.js`：

```javascript
const readline = require('readline');
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  const req = JSON.parse(line);
  if (req.method === 'tools/call') {
    const name = req.params?.arguments?.name ?? 'world';
    const resp = { jsonrpc: '2.0', id: req.id, result: { content: `hello, ${name}` } };
    process.stdout.write(JSON.stringify(resp) + '\n');
  }
});
```

manifest `entry_point` 改 `command = "node"`, `args = ["main.js"]`。

### 3.3 Rust

`src/main.rs`：

```rust
use std::io::{self, BufRead, Write};
use serde_json::{json, Value};

fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    for line in stdin.lock().lines() {
        let req: Value = serde_json::from_str(&line?).unwrap();
        if req["method"] == "tools/call" {
            let name = req["params"]["arguments"]["name"].as_str().unwrap_or("world");
            let resp = json!({"jsonrpc": "2.0", "id": req["id"], "result": {"content": format!("hello, {name}")}});
            let mut out = stdout.lock();
            writeln!(out, "{}", resp)?;
            out.flush()?;
        }
    }
    Ok(())
}
```

manifest `entry_point` 改 `command = "./target/release/hello-rust"`。

### 3.4 Bash

`main.sh`：

```bash
#!/usr/bin/env bash
while IFS= read -r line; do
    id=$(echo "$line" | jq -r '.id')
    name=$(echo "$line" | jq -r '.params.arguments.name // "world"')
    printf '{"jsonrpc":"2.0","id":%s,"result":{"content":"hello, %s"}}\n' "$id" "$name"
done
```

manifest `entry_point` 改 `command = "bash"`, `args = ["main.sh"]`。

### 3.5 手动测试

不需要跑 Agent，直接用 CLI：

```bash
corlinman plugins invoke hello-python.greet --args '{"name":"Ada"}'
# 期待输出：{"content": "hello, Ada"}
```

`corlinman plugins list` 能看到；看不到就 `corlinman plugins inspect hello-python` 看 discovery 给出的原因。

## 4. JSON-RPC 协议（sync / async）

**请求**（stdin 单行 JSON + `\n`）：

```jsonc
{ "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {
    "name": "greet",
    "arguments": { "name": "Ada" },
    "session_key": "qq:self:group:123:user:456",
    "request_id": "req_abc123",
    "trace_id": "0af7651916cd43dd8448eb211c80319c"
  } }
```

**响应**（stdout 单行 JSON + `\n`）：

```jsonc
// success
{ "jsonrpc": "2.0", "id": 1, "result": { "content": "hello, Ada" } }

// error
{ "jsonrpc": "2.0", "id": 1, "error": { "code": -32602, "message": "missing required param: name" } }
```

**可选的 notifications**（插件可主动发 progress，服务端不 id 回复）：

```jsonc
{ "jsonrpc": "2.0", "method": "notifications/progress",
  "params": { "request_id": "req_abc123", "percent": 50, "message": "half done" } }
```

**其他 methods**（`tools/list` / `tools/describe` 等）遵循 MCP（Model Context Protocol）的子集约定，gateway 会在 discovery 阶段调用 `tools/list` 发现插件能力。

**stderr 规定**：插件不应向 stdout 写非 JSON-RPC 消息；调试写 stderr，gateway 以 `subsystem=plugin.<name>` 的 `debug` 级日志汇入。

## 5. Async 插件

`plugin_type = "async"` 时，`tools/call` 响应可以直接返 `content`（同步完成），也可以返 `task_id`（后台异步）：

```jsonc
{ "jsonrpc": "2.0", "id": 1, "result": { "task_id": "task_xyz789" } }
```

gateway 记 pending，插件后台跑任务，完成后 HTTP POST 回 gateway：

```bash
curl -X POST http://localhost:6005/plugin-callback/task_xyz789 \
  -H 'Content-Type: application/json' \
  -d '{"status":"success","result":{"content":"done"}}'
```

gateway 根据 `task_id` 找到等着的 `oneshot::Sender` 唤醒 Agent loop。超时由 manifest `communication.timeout_ms` 和 `CancellationToken` 联合控制；超时后即使 callback 也会被拒。

**回调鉴权**：`task_id` 是一次性凭据，无需 Bearer。插件不要把 `task_id` 写日志。

## 6. Service 插件

`plugin_type = "service"` 时，gateway 启动时 spawn 插件，设 env `CORLINMAN_PLUGIN_ADDR=/tmp/corlinman-plugin-<name>.sock`，插件连到这个 UDS 跑 gRPC：

- 插件 implement `PluginBridge::Execute(ToolCall) returns (stream ToolEvent)` server
- gateway 是 client 发 ToolCall，收 ToolEvent（`progress` / `result` / `error` / `awaiting_approval`）

适用场景：持久化状态（比如内置向量索引）、fan-in 多个 tool 到同一服务、反向调 gateway 的能力。绝大多数插件不需要 service，写 `sync` 就够。

## 7. 沙箱

`sandbox` 段**默认留空等于不沙箱**。生产插件建议都填，至少：

```toml
[sandbox]
memory = "256m"
cpus = 0.5
read_only_root = true
cap_drop = ["ALL"]
network = "none"
binds = []
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `memory` | string | Docker 内存上限；OOM 直接 kill |
| `cpus` | float | CPU 配额 |
| `read_only_root` | bool | 容器 rootfs 只读；写临时文件必须用 tmpfs 或 bind |
| `cap_drop` | string[] | Linux capabilities 丢哪些 |
| `network` | string | `"none"` / `"bridge"` / `"host"` |
| `binds` | string[] | Docker `-v` 形式 |

**OOM 观测**：metric `corlinman_plugin_execute_total{plugin=X,status=oom}`。频繁 OOM 就调大 `memory` 或优化代码。

## 8. 审批

`~/.corlinman/config.toml` 的 `[[approvals.rules]]` 段控制哪些工具需要审批：

```toml
[[approvals.rules]]
plugin = "file-ops"
tool = "file-ops.write"
mode = "prompt"            # "auto" | "prompt" | "deny"

[[approvals.rules]]
plugin = "file-ops"
tool = "file-ops.read"
mode = "auto"

[[approvals.rules]]
plugin = "dangerous-plugin"
mode = "deny"
```

- `mode = "auto"` — 自动通过（最常见）
- `mode = "prompt"` — gateway SSE 发 `awaiting_approval`，等 admin 在 UI 点通过；超时等同 deny
- `mode = "deny"` — 直接拒绝给 LLM 返错误
- `allow_session_keys = ["qq:self:group:123:*"]` — 特定 session 免审批的白名单（可选）

规则匹配顺序：精确 `plugin+tool` > 仅 `plugin` > 默认 `"auto"`。

**首次执行强制 prompt**：非 Bundled origin 的插件第一次被调用时，即使配置了 `"auto"`，gateway 也会强制走一次 `prompt`。通过后自动写入本地信任名单。

## 9. 调试

**日志**：

```bash
docker logs -f corlinman | grep "subsystem=plugin.hello-python"

# 或本地 dev
corlinman dev --log-level debug
# 插件 stderr 会以 "subsystem=plugin.<name> level=debug" 输出
```

**手动触发**：

```bash
corlinman plugins invoke <name>.<tool> --args '{"k":"v"}'
# 完全走 registry + runtime，不涉及 LLM
```

**列出和检查**：

```bash
corlinman plugins list                 # 所有 manifest
corlinman plugins inspect <name>       # 一个插件详情 + origin + 校验结果
corlinman plugins doctor               # 整体健康检查
```

**hot-reload**：manifest 文件改了 `notify` watcher 在 60s 内重新发现，不用重启 gateway。entry_point 代码本身的改动下次调用生效（每次 spawn 新进程）。

## 10. 常见问题

**Q: 我的插件 `plugins list` 看不到**  
A: `corlinman plugins doctor` 看 manifest 解析 / origin 冲突 / 文件权限。

**Q: 插件执行超时**  
A: `manifest.toml` 的 `communication.timeout_ms` 调大；若真是 long-running 改成 `plugin_type = "async"`。

**Q: 沙箱里装不上 Python 包**  
A: 沙箱 `network = "none"` 时 pip 不能下载；在插件根目录先 `pip install --target=./vendor` 把依赖打包，manifest 里设 `binds = ["./vendor:/workspace/vendor:ro"]`，main.py 开头 `sys.path.insert(0, "/workspace/vendor")`。

**Q: 如何让插件访问 GPU**  
A: `sandbox.binds` 加 `["/dev/nvidia0:/dev/nvidia0", "/dev/nvidiactl:/dev/nvidiactl"]`，compose 里给 gateway 加 `deploy.resources.reservations.devices`。

## 延伸阅读

- Manifest schema 的 Rust 源头：`rust/crates/corlinman-plugins/src/manifest.rs`
- Origin ranking 和 discovery 细节：计划文件 §7
- 运维视角（OOM、审批堆积、超时）：[runbook.md](runbook.md)
- 整体架构里插件的位置：[architecture.md](architecture.md) §6 时序图
