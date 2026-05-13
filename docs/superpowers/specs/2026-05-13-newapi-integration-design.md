# newapi 集成设计（替换 sub2api）

Status: design, awaiting plan.
Author: brainstorming session (user goal 2026-05-13).
Date: 2026-05-13.
Supersedes: `docs/design/sub2api-integration.md`.
Decisions: see §0 below.

## 0. Decisions captured in brainstorming

| # | 决策 | 选项 |
|---|---|---|
| D1 | newapi 上游 | **QuantumNous/new-api**（MIT，主流活跃 fork，OAuth 池化能力齐全） |
| D2 | 范围拆分 | 6 个子项目 A–F，本 spec 仅覆盖 **A**。B（openclaw/vcp 调研）有独立草稿，C（回归测试）/ D（预编译流水线）/ E（GitHub 发版）/ F（服务器部署）各自后续 spec |
| D3 | 交互式一键配置形态 | **扩展 onboard 向导** + admin 独立 `/admin/newapi` 页同步；compose 不自动拉 newapi 容器 |
| D4 | sub2api 旧代码 | **硬移除**，CHANGELOG 公告 BREAKING；提供 `corlinman config migrate-sub2api` CLI |
| D5 | 集成形态 | **方案 A**：`kind = "newapi"` enum + admin 连接器页，与现有 `ProviderKind` 框架对齐 |
| D6 | A/D 关系 | A 与 D 各自一份 spec，**串行实施**：A → C → D → E → F |

## 1. Goal

把当前 `ProviderKind::Sub2api` 整体替换为 `ProviderKind::Newapi`，让 corlinman 通过
QuantumNous/new-api 集中管理 LLM/Embedding/TTS 三类模型的下发。

操作者在首次启动时一次性输入 newapi 连接信息（URL + token），UI
自动从 newapi 拉取频道清单并让操作者勾选默认 LLM/Embedding/TTS
频道；写盘后所有 chat / embed / voice 调用都通过 newapi 转发。

### Non-goals

- 重写 corlinman 的 free-form provider 模型（OpenAI/Anthropic/Google 等 13 个现有 kind 继续工作）。
- 鸟瞰式重构 evolution / TagMemo / canvas / channels 等子系统。
- newapi 容器自启动 —— compose 文件仅提供示例，`docker compose up corlinman` 不会默认拉 newapi。
- 多 newapi 实例 / 高可用 / 跨集群同步 —— 多 newapi 用现有「多 provider entry」即可表达。

## 2. 上游选型摘要

| 候选 | 语言 | 协议 | License | 上次更新 | 选择 |
|---|---|---|---|---|---|
| **QuantumNous/new-api** | Go + React | OpenAI Chat / Embedding / Audio TTS / Midjourney / Suno | MIT | 2026 active | **picked** |
| songquanpeng/one-api | Go + JS | OpenAI Chat / Embedding | MIT | 2026-01 last big push | runner-up |
| MartialBE/one-hub | Go + JS | OpenAI compat + 私有渠道 | MIT | active | 不选 |
| Wei-Shaw/sub2api | Go + Vue | OpenAI / Anthropic / Gemini wire | LGPL-3.0 | active | **被替换** |

**MIT vs LGPL-3.0**：上一轮 sub2api 选择留下了 LGPL-3.0 链接边界风险（虽然走 HTTP
不违反，但 CREDITS.md 必须额外书写、operator 须独立部署）。换 MIT 的 new-api
后 CREDITS.md 简化，license 注意事项归零。

## 3. 架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│  corlinman gateway (Rust)                                                │
│                                                                           │
│  ┌────────────────┐    ┌─────────────────────────────────────────┐       │
│  │ admin routes   │    │ runtime (chat/embed/voice)              │       │
│  │  • providers   │    │  ┌─────────────────────────────────┐    │       │
│  │  • newapi *NEW │────│  │ OpenAICompatibleProvider (py)  │    │       │
│  │  • onboard +   │    │  │   POST /v1/chat/completions    │────┼──→    │
│  │    newapi step │    │  │   POST /v1/embeddings          │    │       │
│  └────────┬───────┘    │  │   POST /v1/audio/speech        │    │       │
│           │            │  └─────────────────────────────────┘    │       │
│           │            └─────────────────────────────────────────┘       │
│           ▼                                                               │
│  ┌─────────────────────────────────────┐                                  │
│  │ newapi_client crate (Rust) *NEW     │  ← admin API only                │
│  │   GET /api/channel?type={1,2,8}     │                                  │
│  │   GET /api/user/self                │                                  │
│  │   POST /v1/chat/completions (probe) │                                  │
│  └────────────────────┬────────────────┘                                  │
│                       │                                                   │
└───────────────────────┼───────────────────────────────────────────────────┘
                        │ HTTP
                        ▼
              ┌───────────────────────────┐
              │ QuantumNous/new-api        │
              │  (sidecar, Docker or bare) │
              │  Postgres optional         │
              └───────────────────────────┘
```

**关键不变量**：runtime 路径不感知 newapi —— 它只看到一个 OpenAI 兼容 base_url
和 bearer token。所有 newapi 专属逻辑（频道发现、健康度）都在 admin 域。

## 4. 组件清单

### 4.1 Rust 改动

| 文件 | 性质 | 说明 |
|---|---|---|
| `rust/crates/corlinman-core/src/config.rs` | 改 | `ProviderKind::Sub2api` → `ProviderKind::Newapi`；`base_url_required` 列表替换；`as_str()` / `all()` 同步；测试 `(ProviderKind::Newapi, "newapi")` 替换；新增 `params.newapi_admin_key`（`SecretRef`）字段说明 |
| `rust/crates/corlinman-gateway/src/routes/admin/providers.rs` | 改 | 4 处 `Sub2api` 引用全部替换为 `Newapi`；测试 `upsert_rejects_sub2api_without_base_url` → `upsert_rejects_newapi_without_base_url`；`upsert_persists_sub2api_slot_and_renders_py_config` 同步迁移 |
| `rust/crates/corlinman-newapi-client/` | **新增 crate** | newapi admin API 客户端：`list_channels(type: ChannelType) -> Vec<Channel>`、`get_user_self() -> User`、`probe(base_url, token) -> ProbeOk`、`test_round_trip(base_url, key) -> Latency`。深度 ~250 行 `reqwest` + `serde` |
| `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs` | **新增** | `GET /admin/newapi` connection summary；`GET /admin/newapi/channels?type=` live channel list；`POST /admin/newapi/probe { base_url, token, admin_token }`；`POST /admin/newapi/test`；`PATCH /admin/newapi` connection update |
| `rust/crates/corlinman-gateway/src/routes/onboard.rs` | 改 | 现 onboard 是单 POST。重构为多步：`POST /admin/onboard/account` → `POST /admin/onboard/newapi` → `GET /admin/onboard/newapi/channels` → `POST /admin/onboard/finalize`。状态保存在 ephemeral server-side session（与现有 admin-credential mutex 同 store） |
| `rust/crates/corlinman-cli/src/migrate.rs` | **新增子命令** | `corlinman config migrate-sub2api`：扫描 `config.toml` 里所有 `kind = "sub2api"` 条目，dry-run 打 diff（默认）；加 `--apply` 重写为 `kind = "newapi"`，保留 `base_url` `api_key` `enabled`，未识别字段写入注释 |

### 4.2 Python 改动

| 文件 | 性质 | 说明 |
|---|---|---|
| `python/packages/corlinman-providers/src/corlinman_providers/specs.py` | 改 | `SUB2API = "sub2api"` → `NEWAPI = "newapi"`；注释更新 |
| `python/packages/corlinman-providers/src/corlinman_providers/registry.py` | 改 | dispatch 表 `SUB2API` → `NEWAPI`；继续走 shared `OpenAICompatibleProvider` |
| `python/packages/corlinman-providers/tests/` | 改 | mock 用例改用 `kind = "newapi"`；新增 audio TTS 路由 mock 测试 |

### 4.3 UI 改动（Next.js）

| 文件 | 性质 | 说明 |
|---|---|---|
| `ui/app/onboard/page.tsx` | 改 | 单页 3 字段 → 4 步向导（账户 → newapi 连接 → 选默认模型 → 确认）。用 `useState` 跨步保存草稿，错误内联展示 |
| `ui/app/onboard/page.test.tsx` | 改 | 4 步流测试 + 失败态 |
| `ui/app/(admin)/newapi/page.tsx` | **新增** | 三块：连接信息编辑卡（PATCH `/admin/newapi`） + 频道健康表（GET `/admin/newapi/channels`） + 测试按钮（POST `/admin/newapi/test`） |
| `ui/app/(admin)/newapi/page.test.tsx` | **新增** | 渲染、加载态、错误态、测试动作 |
| `ui/app/(admin)/embedding/page.tsx` | 改 | 在 provider 下拉旁加 "从 newapi 拉清单" 按钮，点击后展开 newapi 频道列表勾选填入 |
| `ui/app/(admin)/models/page.tsx` | 改 | 同上 |
| `ui/lib/api.ts` | 改 | 新增 `fetchNewapi`、`fetchNewapiChannels`、`probeNewapi`、`testNewapi` |
| `ui/components/layout/nav.tsx` 和/或 `sidebar.tsx` | 改 | 在 admin 导航加 `/newapi` 入口（按当前 nav schema） |

### 4.4 文档

| 文件 | 性质 | 说明 |
|---|---|---|
| `docs/design/sub2api-integration.md` | **删除** | 用 `docs/design/newapi-integration.md` 替代（rename + 重写） |
| `docs/design/newapi-integration.md` | **新增** | 本 spec 的精简发布版（操作手册） |
| `docs/providers.md` | 改 | 表格里 `sub2api` 行替换为 `newapi`；MIT 许可注明；移除 LGPL 警告 |
| `docs/migration/sub2api-to-newapi.md` | **新增** | 旧 sub2api 用户的 5 步迁移指引：拉新版 → 跑 `migrate-sub2api --dry-run` → 检查 → `--apply` → 重启 |
| `CREDITS.md` | 改 | 删除 `Wei-Shaw/sub2api` 段；新增 `QuantumNous/new-api`（MIT，无许可注意事项） |
| `CHANGELOG.md` | 改 | `## [Unreleased] — BREAKING` 段添加：`Removed ProviderKind::Sub2api. Use ProviderKind::Newapi with QuantumNous/new-api. Migration: corlinman config migrate-sub2api --apply.` |

### 4.5 i18n

所有新增 UI 字符串走命名空间 `t("onboard.newapi.*")` 与 `t("admin.newapi.*")`，
中英两版同提交。错误码（如 `newapi_admin_unauthorized`、`newapi_channel_list_empty`）
在 `ui/lib/i18n/locales/{zh-CN,en}/errors.json` 都添加翻译条目。

## 5. 数据流详图

### 5.1 Onboard 向导（4 步）

```
Step 1 — Account
  UI POST /admin/onboard/account { username, password, password_confirm }
  Gateway: 写 [admin] 段，issue ephemeral onboard_session_id (cookie)
  Response: { onboard_session_id, next: "newapi" }

Step 2 — newapi connect
  UI POST /admin/onboard/newapi { base_url, token, admin_token? }
  Gateway: corlinman-newapi-client probe(base_url, admin_token or token)
           - GET /api/user/self → 验证 token 真实存在
           - GET /api/status → 验证 base_url 是 newapi 而不是任意 HTTP 服务
  200 ⇒ session 暂存 { base_url, token, admin_token }
        Response: { next: "models", channels_available: <count> }
  4xx ⇒ 精确错误码：newapi_unreachable / newapi_token_invalid /
                    newapi_admin_required / newapi_version_too_old

Step 3 — pick defaults
  UI GET /admin/onboard/newapi/channels?type=llm
  UI GET /admin/onboard/newapi/channels?type=embedding
  UI GET /admin/onboard/newapi/channels?type=tts
  Gateway: 用 session 里的 admin_token 调 newapi GET /api/channel?type=1|2|8
           过滤 status=enabled & test_time>0（健康频道）
           返回 [{id, name, type, models[], group, priority}]
  
  UI POST /admin/onboard/newapi/select {
    llm:  { channel_id, model_name },     // 默认: 第一个 status=enabled 的 chat 频道里的第一个 model
    embed:{ channel_id, model_name },     // 类似；自动 probe dimension
    tts:  { channel_id, model_name, voice }  // 类似；voice 候选 alloy/echo/onyx 等
  }
  Gateway: 暂存到 session；不写盘
  Response: { next: "confirm", preview: <generated_config_diff> }

Step 4 — confirm
  UI POST /admin/onboard/finalize {}
  Gateway: 一次原子写 config.toml（走现有 admin-write mutex 99f8390）：
    [providers.newapi]
    kind = "newapi"
    base_url = "<from session>"
    api_key = { value = "<from session>" }       # 或写 env 提示
    enabled = true
    [providers.newapi.params]
    newapi_admin_url = "<base_url>/api"
    newapi_admin_key = { value = "<from session>" }
    
    [models]
    default = "<picked llm model>"
    [models.aliases.<picked llm model>]
    model = "<actual model id from newapi>"
    provider = "newapi"
    
    [embedding]
    enabled = true
    provider = "newapi"
    model = "<picked embed model>"
    dimension = <probed>
    
    [voice]
    enabled = true
    provider_alias = "newapi"
    tts_model = "<picked tts model>"
    tts_voice = "<picked voice>"
    sample_rate_hz_out = 24000
  
  200 ⇒ 销毁 session, redirect /login
```

### 5.2 Runtime — 与今天一致

LLM/Embed/TTS 调用全部走 `OpenAICompatibleProvider` → `<base_url>/v1/<endpoint>`。
newapi 自身做 channel failover、限速、计费。corlinman 看不见这些。

### 5.3 Admin `/admin/newapi` 后期管理

```
GET /admin/newapi
  → { connection: { base_url, token_masked, admin_key_present, last_probe_at }, status: ok|degraded }

GET /admin/newapi/channels
  → newapi GET /api/channel → enriched view:
    [{ id, name, type, models, status, used_quota, remain_quota,
       last_test_time, test_latency_ms, oauth_token_ttl_seconds? }]

POST /admin/newapi/test
  → 1-token chat completion → { latency_ms, status, model_used }

PATCH /admin/newapi { base_url?, token?, admin_token? }
  → re-probe + atomic config.toml write
```

## 6. 错误处理

| 场景 | 行为 |
|---|---|
| newapi 不可达 | onboard step2 内联红字 `i18n: error.newapi_unreachable`，可重试，账户步骤已写入不丢 |
| token 无效（newapi 返回 401） | `error.newapi_token_invalid`，引导用户去 newapi `/console/token` 重新拷 |
| admin token 缺失或权限不够 | `error.newapi_admin_required`，指引「在 newapi 后台 → 设置 → 系统访问令牌 → 创建」 |
| `/api/channel` 返回空 | step3 黄条 `error.newapi_channel_list_empty` + 链接到「newapi 渠道」管理页 + 重新加载按钮 |
| newapi 版本过老（缺 `/api/status`） | `error.newapi_version_too_old`，告知最低支持版本号 |
| step3 → step4 之间 newapi 频道下线 | finalize 时再 probe 一次，若不可用则回滚到 step3 并提示 |
| finalize 写盘失败 | 503 + 详细 reason；session 不销毁，用户可重试 |
| migrate-sub2api 遇到无法识别字段 | dry-run 列出，`--apply` 时把字段保留为注释 `# legacy sub2api field: review` |

## 7. 测试策略

| 层 | 文件 | 用例 |
|---|---|---|
| Rust unit | `rust/crates/corlinman-newapi-client/tests/client.rs` | wiremock 模拟 newapi：成功、401、404、超时、JSON 异常、`/api/status` 不存在 |
| Rust unit | `rust/crates/corlinman-core/src/config.rs` | `ProviderKind::Newapi` 全部 round-trip 测试（替换 Sub2api 测试） |
| Rust route | `rust/crates/corlinman-gateway/tests/admin_newapi.rs` | 全部 5 个 `/admin/newapi/*` 路由 happy + sad |
| Rust route | `rust/crates/corlinman-gateway/tests/admin_onboard.rs` | 4 步完整流 + 中途中断恢复 + finalize 失败 |
| Rust CLI | `rust/crates/corlinman-cli/tests/migrate.rs` | 单 entry / 多 entry / 含未识别字段 / 已迁移幂等 |
| Python | `python/packages/corlinman-providers/tests/test_newapi.py` | dispatch + audio TTS 路由 mock |
| UI | `ui/app/onboard/page.test.tsx` | 4 步流（含返回上一步）+ 错误态 |
| UI | `ui/app/(admin)/newapi/page.test.tsx` | 加载 / 错误 / 健康度展示 / 测试按钮 |
| E2E | `scripts/e2e/newapi-flow.sh` | docker-compose 启 newapi + corlinman → onboard → 跑 chat / embed / tts 各一发 → assert 200 |
| 进化系统回归 | C 子项目 spec 单独处理 | 不在本 spec |

## 8. 迁移指引（docs/migration/sub2api-to-newapi.md 大纲）

```
1. 升级到本版本前：先备份 config.toml + identity store。
2. `corlinman config migrate-sub2api --dry-run` 查看待改动 diff。
3. 部署新版二进制 / 镜像。
4. 在另一台主机或同主机上跑 QuantumNous/new-api：
   docker run -d -p 3000:3000 -v ./newapi-data:/data \
       calciumion/new-api:latest
5. 在 newapi 后台导入原 sub2api 的 OAuth 账户（如果有）/ 渠道 / 令牌。
6. `corlinman config migrate-sub2api --apply --newapi-base-url=http://... --newapi-token=sk-...`
7. `corlinman gateway restart`，进入 /admin/newapi 检查频道清单。
```

`migrate-sub2api` CLI 不能自动把 sub2api 的内部状态（Account/Channel）搬到 newapi —— 不同的 schema、不同的 OAuth 实现，corlinman 不持有这些数据。CLI 只改写 corlinman 自己的 config.toml。

## 9. 工作量估计

| 切片 | 投入 |
|---|---|
| `corlinman-newapi-client` crate + tests | 1 天 |
| `ProviderKind::Sub2api` → `Newapi` 重命名 + 测试迁移 | 0.5 天 |
| `/admin/newapi` 路由（5 个）+ tests | 1 天 |
| Onboard 后端拆 4 步 + tests | 1 天 |
| Onboard UI 4 步向导 + tests + i18n | 1.5 天 |
| `/admin/newapi` UI 页 + tests + i18n | 1 天 |
| Embedding/Models 页面"从 newapi 拉清单"按钮 | 0.5 天 |
| `corlinman config migrate-sub2api` CLI + tests | 0.5 天 |
| 文档：design / providers / migration / CREDITS / CHANGELOG | 0.5 天 |
| E2E 脚本 + docker-compose 示例 | 0.5 天 |
| **合计** | **8 天**（单人，含测试与翻译） |

## 10. 风险与开放问题

| # | 风险 | 缓解 |
|---|---|---|
| R1 | newapi `/api/channel` admin API 未在公开 README 文档化，我们在「逆向」 | crate 加 feature flag；pin newapi 镜像 tag；为每个调用加单测用 wiremock 锁请求形状 |
| R2 | newapi 的 Audio TTS 实际转发能力依赖底层渠道是否支持，运行时可能失败 | onboard step3 拉 channel list 时按 `type=8`（audio）过滤；admin 页显示「该频道未启用 TTS」灰态 |
| R3 | newapi 用户 token 与 admin system token 是两套，混用导致 401 | onboard probe 同时要 user token + admin token，缺一不可；admin token 在 `params.newapi_admin_key` 单独存 |
| R4 | 用户跳过 onboard 步骤直接编辑 config.toml | 仍允许（保留 free-form provider 哲学）；admin 页若发现 `[providers.newapi].params.newapi_admin_url` 缺失则降级展示「无健康度数据，请补全连接信息」 |
| R5 | 多 newapi 实例 | 当前 spec 不显式支持，但 `[providers.newapi-a] [providers.newapi-b]` 多个同 kind 条目天然可用；admin `/newapi` 页 v1 只显示 default newapi（首个 enabled 的 newapi entry） |
| R6 | 在线升级时 newapi 重启 → 短暂 503 | corlinman 既有 retry 不变；admin 页 status 字段 5xx 时显示 degraded 而不是 dead |
| R7 | newapi 服务端版本 GA 漂移 | install 文档 pin minor 版本范围；CI 跑 wiremock 不动 |

## 11. 范围外（再次明确）

- B（openclaw / vcp-tools 调研改进 backlog）—— 已有独立草稿 `docs/design/research/2026-05-13-openclaw-vcp-improvement-backlog.md`（来自 Explore agent）
- C（功能 / 工具调用 / 进化系统回归测试）—— A 完成后另起 spec
- D（Rust 预编译 + GitHub Release + Docker install 一键脚本）—— 已沟通方向，另起 spec
- E（README + CHANGELOG + GitHub push 上线）—— A/D 完成后另起
- F（服务器上传预编译产物 + 重启服务）—— E 之后另起

## 12. References

- QuantumNous/new-api: https://github.com/QuantumNous/new-api（MIT）
- 上一轮 sub2api 设计：`docs/design/sub2api-integration.md`（本 spec 落地后即删除）
- ProviderKind 枚举：`rust/crates/corlinman-core/src/config.rs:441-493`
- admin providers 路由：`rust/crates/corlinman-gateway/src/routes/admin/providers.rs`
- admin-write 串行化锁：commit 99f8390
- 现有 onboard：`rust/crates/corlinman-gateway/src/routes/onboard.rs`（路由表里）, `ui/app/onboard/page.tsx`
