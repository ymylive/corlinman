# PLAN — Easy Setup + Hermes Self-Evolution Port

**Status:** 草案 v1.0 · 2026-05-17 · 多 agent 并行执行

**Scope:** 把 corlinman 从"能用但要懂"提升到"开箱即用"，并把 hermes-agent 的自我进化机制完整移植进 `gateway/evolution/` + `(admin)/evolution/`。

**Hard requirements**（来自用户）：
1. 登录首页初始账号密码 = `admin` / `root`
2. 用户可在 `账户/安全` (Account / Security) 修改用户名 + 密码
3. 设计快速初始化流程，让普通用户也能快速 setup
4. **明确借鉴 hermes-agent 的自我进化模块**

---

## 0. 三方对比矩阵（State of Play）

| 维度 | corlinman 现状 | hermes-agent | openclaw | 差距来源 |
|------|---------------|--------------|----------|---------|
| **首启动凭证** | 503 强制走 /onboard | 无登录（单用户本地） | CLI wizard `openclaw onboard` | corlinman 应自动播种 `admin/root` |
| **Auth 模式** | argon2id + session cookie | 无（信任本地） | `none` / `token` / `password` / `trusted-proxy` | corlinman 可加 trusted-proxy 备选 |
| **多语言** | i18next, 2 种 (zh-CN, en) | i18next, 16 种 | 无 | corlinman 可逐步扩 |
| **Setup 模式** | Web 向导（4 步） | CLI `/setup` slash | CLI 交互式 | corlinman 选了正路，需优化 |
| **Agent 定义** | `agents/*.yaml`（4 个） | 隐式（profile 目录） | `Agent/*.txt` markdown 分层人设 | corlinman YAML 强类型，但缺 UI 创建 |
| **创建新 agent UI** | 无 | `ProfilesPage` 模态框（slug 正则 + clone_from_default） | AdminPanel 表单 | corlinman 需新建 /(admin)/profiles |
| **Skill 模型** | `skills/*.md` 静态 | `SKILL.md` + 子目录（references/templates/scripts/assets）+ 运行时可创建 | `Plugin/*` 带 manifest.json | corlinman 缺运行时可变性 |
| **插件发现** | 手工注册 | `plugin.yaml` + `__init__.py` 自注册，bundled vs user 双层 | manifest.json + stdio 协议 | corlinman 可引入自注册 |
| **Provider 配置** | TOML `[providers.*]` | `plugins/model-providers/<name>/` (27 个) + provider profile dataclass | gateway config | corlinman 缺 UI provider 分组管理 |
| **API key 输入** | TOML 手填 | EnvPage 按 provider 分组 + 眼睛图标 + paste-only + 掩码 | AdminPanel form | corlinman 需要 EnvPage |
| **模型选择 UI** | 双 select 联动 | `ModelPickerDialog` 两段式（provider→model）+ 搜索 + 持久化勾选 | 表单 | corlinman 可升级 |
| **State 持久化** | SQLite `kb.sqlite` + `evolution.sqlite` | SQLite `state.db` (WAL + FTS5 + NFS fallback) | SQLite | corlinman 已有，缺 FTS5 |
| **记忆** | KB + evolution signals | MEMORY.md (2200 字符) + USER.md (1375 字符) + 外部 Honcho/mem0/supermemory | TagMemo + EPA + Residual Pyramid + USearch | corlinman 缺人类可读 markdown 记忆面 |
| **Cron/Routine** | scheduled subprocess (daily 03:00 UTC) | `cron/jobs.json` + croniter，每 60 秒 tick，输出归档 `cron/output/{id}/{ts}.md` | 无 | corlinman 已有，可引入 hermes 的 webhook routine |
| **自我进化** | `EvolutionObserver` + `EvolutionApplier` + signals 表 | **3 子系统**：curator loop（idle 7d 触发）+ background fork（隔离 mem+skill tools）+ cron scheduler；用户纠正写入 skill body | persona 层 + TagMemo 记忆评估 | corlinman 骨架在，缺 hermes 的语义层（详见 §4） |
| **Dry-run** | 无 | `hermes curator run --dry-run` 输出预览 YAML | 无 | corlinman 应加 |
| **测试** | 仅中间件单测，无 E2E | 有 trajectory replay + skill curator dry-run | 测试薄 | corlinman 需补 onboard→login→admin Playwright E2E |

---

## 1. Target Architecture（目标态）

### 1.1 首启动流程（关键改变）

```
冷启动 → gateway 检测 config.toml 无 [admin] 块
   ↓
admin_seed.ensure_admin_credentials() 写入 username="admin", password_hash=hash("root"),
   must_change_password=true
   ↓
浏览器访问 /login → 输入 admin/root → /admin/login 返回 cookie + me.must_change_password=true
   ↓
前端强制重定向 → /account/security
   ↓
用户改用户名 + 密码 → POST /admin/username + /admin/password → must_change_password 翻 false
   ↓
正常进入 /admin（dashboard）
```

> **现有 `/onboard` 多步向导保留**，但变成"可选附加配置"入口（连 newapi、选默认模型、配 LLM provider），不再是强制路径。

### 1.2 自我进化目标态（hermes 模式移植）

```
事件流（已有）：HookBus → EvolutionObserver → evolution_signals.sqlite
                                                  ↓
新增触发器：IdleReflectionTrigger（每 7 天 / 可配） + UserCorrectionDetector + ToolFailureCounter
                                                  ↓
新增执行器：AsyncBackgroundApplier（fork 出受限工具集的 mini-agent）
   - 仅允许 memory_tool + skill_manage 工具
   - 继承父会话的 provider/model/credentials
   - 写入 SKILL.md（用户纠正直接 patch 进 skill body，不止 memory）
                                                  ↓
新增生命周期：SkillState { active, stale (>30d), archived (>90d) } + auto-transition
                                                  ↓
新增 provenance: SkillOrigin { bundled, user-requested, agent-created }
   - 仅 agent-created 受 curator 管理
                                                  ↓
UI 新增 /admin/evolution/curator：dry-run toggle、按 origin 过滤、状态徽章
```

---

## 2. 实施波次（Waves）

每个 task 标注：
- **Subagent**：建议 agent 类型
- **Files**：要触碰的文件
- **Deps**：依赖的其它 task
- **Validation**：完成判定
- **ETA**：粗略估算（人时）

### Wave 1 — Auth 基础（4 个并行 task）

> 这一波让 admin/root 落地 + 强制改密 + 账户安全页生效。是用户硬需求的最短路径。

#### W1.1 后端首启动播种 admin/root + 入口接线
- **Subagent**: Backend Architect（或直接主线）
- **Files**:
  - `python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/admin_seed.py`（已存在，已写完）
  - `python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/entrypoint.py`（接入 `await ensure_admin_credentials()`，在 `_mount_routes` 之前；把结果塞进 `AdminState`）
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/state.py`（已加 `must_change_password` 字段）
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/auth.py`（`/admin/me` response 增加 `must_change_password`；`change_password` 成功后把 state 中的标志翻 false 并 persist）
- **Deps**: 无
- **Validation**:
  1. 删 `config.toml`，启 gateway → 日志出 `admin_seed.default_credentials_installed`
  2. `curl -X POST /admin/login -d '{"username":"admin","password":"root"}'` 返回 200 + Set-Cookie
  3. `curl /admin/me -H "Cookie: corlinman_session=..."` 返回 `{"must_change_password": true}`
  4. `pytest python/packages/corlinman-server/tests/gateway/lifecycle/test_admin_seed.py`
- **ETA**: 3h
- **Note**: 我已经做了 50%（`admin_seed.py` + state field + 部分 entrypoint 编辑）。剩下：调用接线 + /admin/me 字段 + change_password 翻 flag + 单测。

#### W1.2 后端 /admin/username（修改用户名）
- **Subagent**: Backend Architect
- **Files**:
  - `routes_admin_a/auth.py`（新 `@r.post("/admin/username")` handler，要求 session + old_password 验证，校验 username 非空 + 不冲突，重用 `_persist_admin_credentials` 但只换 username）
  - `routes_admin_a/auth.py` 增 `ChangeUsernameRequest` pydantic model
  - `routes_admin_a/auth.py` 抽 `_persist_admin_credentials` 接受 `must_change_password` 参数
  - `python/packages/corlinman-server/tests/gateway/routes_admin_a/test_username.py`（新）
- **Deps**: W1.1（必须先有 admin_seed + state.must_change_password）
- **Validation**: 单测覆盖：正确改名 → me 返回新名；未鉴权 → 401；密码错 → 401；空名 → 422
- **ETA**: 2h

#### W1.3 前端 /account/security 页面
- **Subagent**: Frontend Developer + UI Designer
- **Files**:
  - `ui/app/(admin)/account/security/page.tsx`（新）— 两 form：改用户名、改密码（旧+新+确认+至少 8 位）
  - `ui/lib/api.ts` 新增 `changeUsername`、`changePassword` 函数
  - `ui/lib/locales/{en,zh-CN}.ts` 增 i18n keys：`account.security.*`
  - `ui/components/admin/account-banner.tsx`（新）— 顶部红色横幅，"You're using the default password — change it now"，只在 must_change_password=true 时渲染
  - `ui/components/layout/admin-shell.tsx`（在顶部条加 banner 挂载点）
- **Borrow from hermes**:
  - 眼睛图标显示/隐藏密码（参考 hermes `EnvVarRow`）
  - 错误用 toast，不用红边框（hermes 模式）
  - 破坏性操作 ConfirmDialog（hermes `confirm-dialog.tsx`）
- **Deps**: W1.1 (api.me 需要 must_change_password 字段), W1.2 (改用户名 API)
- **Validation**: Playwright E2E：admin/root 登 → banner 显示 → 改用户名 admin→ops → 改密码 → banner 消失 → 重新登录用 ops/新密码成功
- **ETA**: 5h

#### W1.4 登录后强制跳转 /account/security
- **Subagent**: Frontend Developer
- **Files**:
  - `ui/lib/auth.ts`（login() 返回 me 数据；调用方据此决定跳转）
  - `ui/app/login/page.tsx`（onSubmit 成功后：如果 `me.must_change_password` → router.replace("/account/security") 而不是 redirect 参数）
  - `ui/components/admin/auth-guard.tsx`（admin layout 守卫：若 must_change_password=true 且当前路由 ≠ /account/security，强制跳过去）
- **Deps**: W1.1, W1.3
- **Validation**:
  1. admin/root 登 → URL 自动变 /account/security
  2. 直接访问 /admin/dashboard → 被强制跳 /account/security
  3. 改密后访问 /admin/dashboard → 放行
- **ETA**: 2h

**Wave 1 同步点**：W1.2 依赖 W1.1；W1.3 依赖 W1.1+W1.2；W1.4 依赖 W1.1+W1.3。可并行起 W1.1+W1.2 占 50% 时间，W1.3+W1.4 紧接其后。

---

### Wave 2 — Quick Setup 向导重塑（3 个并行 task）

> 现有 4 步 onboard 仍可用，但加"跳过"路径 + 借鉴 hermes 模型选择器，让新用户最少 30 秒完成 setup。

#### W2.1 重写 /onboard：可跳过模型 + admin 自动确认
- **Subagent**: Frontend Developer
- **Files**:
  - `ui/app/onboard/page.tsx`（改造）：
    - 检测 admin 已被首启动播种 → 默认跳过 Step 1 "account"（仍可点 "Customize" 进入修改）
    - Step 2 改名 "Connect LLM (optional)"，加 "Skip → use mock provider" 按钮
    - Step 3 模型选择器换成 hermes `ModelPickerDialog` 两段式
    - Step 4 增加 "Setup complete — change your password now" CTA 链接到 /account/security
  - `ui/components/onboard/model-picker-dialog.tsx`（新，参考 hermes `web/src/components/ModelPickerDialog.tsx:9-200`）
- **Deps**: W1.1
- **Validation**:
  - 删 config.toml + 跳过 LLM 步骤 → 进入 admin 用 mock provider 也能聊
  - 选 OpenAI provider → 拉模型列表 → 选 GPT-4 → finalize 成功
- **ETA**: 6h

#### W2.2 后端 mock provider + skip 路径
- **Subagent**: Backend Architect
- **Files**:
  - `python/packages/corlinman-providers/src/corlinman_providers/mock.py`（新）— 内置 echo provider，返回 prompt 倒序
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/onboard.py`（增 `/admin/onboard/finalize-skip` 端点，写入 `[providers.mock] enabled = true`）
- **Deps**: 无
- **Validation**: `curl /v1/chat/completions` 用 mock provider 返回结构化响应
- **ETA**: 4h

#### W2.3 EnvPage 风格的 Provider 凭证管理页
- **Subagent**: Frontend Developer + UI Designer
- **Files**:
  - `ui/app/(admin)/credentials/page.tsx`（新）— 按 provider 分组，每行 `<EnvVarRow>` 眼睛图标 + paste-only + 掩码
  - `ui/components/credentials/env-var-row.tsx`（新，参考 hermes `web/src/pages/EnvPage.tsx:99-160`）
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/credentials.py`（新）— GET/POST/DELETE `/admin/credentials/{provider}/{key}`，写回 config TOML 的 `[providers.<name>]` 块
- **Deps**: 无（独立 feature）
- **Validation**: 设 OPENAI_API_KEY → reload page → 显示掩码 → 点眼睛 → 显示原值 → 删除 → TOML 中字段消失
- **ETA**: 5h

**Wave 2 同步点**：W2.1↔W2.3 独立可并行；W2.2 是 W2.1 的可选依赖（mock provider 让 skip 流程可演示）。

---

### Wave 3 — Profiles 模型（4 个并行 task）

> 引入 hermes "Profile" 概念：一个 profile = 一个隔离的 agent 实例（自有 persona、记忆、技能、状态）。

#### W3.1 Profiles 后端 schema + CRUD
- **Subagent**: Backend Architect + Database Optimizer
- **Files**:
  - `python/packages/corlinman-server/src/corlinman_server/profiles/__init__.py`（新模块）
  - `python/packages/corlinman-server/src/corlinman_server/profiles/store.py` — Profile dataclass + SQLite CRUD（slug PK, display_name, persona_md, created_at, parent_profile_id for clone）
  - `python/packages/corlinman-server/src/corlinman_server/profiles/paths.py` — `<data_dir>/profiles/<slug>/{SOUL.md, MEMORY.md, USER.md, state.db, skills/}`（hermes 目录结构）
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/profiles.py` — 4 端点：GET /admin/profiles, POST /admin/profiles, PATCH /admin/profiles/{slug}, DELETE /admin/profiles/{slug}
  - 测试：`tests/profiles/test_store.py` + `tests/gateway/routes_admin_a/test_profiles.py`
- **Deps**: 无
- **Validation**: 创建 profile "research-bot" + clone_from="default" → 目录 + 文件齐全；DELETE 删干净
- **ETA**: 8h

#### W3.2 /(admin)/profiles UI
- **Subagent**: Frontend Developer + UI Designer
- **Files**:
  - `ui/app/(admin)/profiles/page.tsx`（新）— 参考 hermes `ProfilesPage.tsx:1-272`
  - `ui/components/profiles/create-profile-modal.tsx`（新）— 2 字段：slug（正则 `/^[a-z0-9][a-z0-9_-]{0,63}$/`）+ clone_from 下拉
  - `ui/components/profiles/profile-row.tsx`（新）— inline rename（铅笔图标）、SOUL.md 折叠编辑器、删除确认
  - `ui/lib/api.ts` 增 `listProfiles / createProfile / renameProfile / deleteProfile / getSoul / saveSoul`
- **Borrow from hermes**: slug 正则即时校验 + toast 反馈（`ProfilesPage.tsx:66-88`）
- **Deps**: W3.1
- **Validation**: Playwright：创建 "test1" → rename 失败（已存在）→ rename "test2" → 编辑 SOUL → 删除 → 列表干净
- **ETA**: 6h

#### W3.3 把现有 agents/*.yaml 迁移成 profile
- **Subagent**: Backend Architect + Minimal Change Engineer
- **Files**:
  - `python/packages/corlinman-server/src/corlinman_server/profiles/migrate.py`（新）— 启动期一次性把 `agents/orchestrator.yaml` 等转成 `profiles/orchestrator/SOUL.md`（保留 yaml 兼容路径）
  - `gateway/lifecycle/entrypoint.py` 加迁移调用（gated by `profiles.migrate_from_agents=true`）
- **Deps**: W3.1
- **Validation**: 首启动后 `<data_dir>/profiles/orchestrator/SOUL.md` 存在且内容等价
- **ETA**: 3h

#### W3.4 Profile 切换器（顶部条）
- **Subagent**: Frontend Developer
- **Files**:
  - `ui/components/layout/profile-switcher.tsx`（新）— 顶部条下拉，"Active: orchestrator ▾"，列表 + "Create new profile" 入口
  - `ui/lib/context/active-profile.tsx` — Context provider + localStorage 持久 `corlinman_active_profile`
- **Deps**: W3.1, W3.2
- **Validation**: 切换后页面右上角显示 active；reload 后保持
- **ETA**: 3h

**Wave 3 同步点**：W3.1 是最大瓶颈；W3.2↔W3.3↔W3.4 并行。

---

### Wave 4 — 自我进化模块（核心，6 个并行 task）

> **明确借鉴 hermes** 的三大子系统：curator loop / background fork / 信号驱动 skill mutation。
> Corlinman 已有 `gateway/evolution/` 骨架（observer + applier + signals 表 + scheduled engine），这一波在它上面叠 hermes 语义层。

#### W4.1 KB schema 扩展（provenance + 版本 + 生命周期）
- **Subagent**: Database Optimizer
- **Files**:
  - `python/packages/corlinman-kb/src/corlinman_kb/migrations/008_skill_lifecycle.sql`（新）
  - Schema 变更：
    ```sql
    ALTER TABLE skills ADD COLUMN version TEXT DEFAULT '1.0.0';
    ALTER TABLE skills ADD COLUMN state TEXT DEFAULT 'active'
      CHECK(state IN ('active','stale','archived'));
    ALTER TABLE skills ADD COLUMN origin TEXT DEFAULT 'user-requested'
      CHECK(origin IN ('bundled','user-requested','agent-created'));
    ALTER TABLE skills ADD COLUMN last_used_at TIMESTAMP;
    ALTER TABLE skills ADD COLUMN last_patched_at TIMESTAMP;
    ALTER TABLE skills ADD COLUMN use_count INTEGER DEFAULT 0;
    ALTER TABLE skills ADD COLUMN patch_count INTEGER DEFAULT 0;
    ALTER TABLE skills ADD COLUMN pinned BOOLEAN DEFAULT 0;
    CREATE INDEX idx_skills_lifecycle ON skills(state, last_used_at);
    ```
  - `python/packages/corlinman-kb/src/corlinman_kb/models.py` — `Skill` dataclass 增字段
  - `tests/kb/test_migration_008.py`
- **Hermes 参考**: `tools/skill_usage.py:119-136`（usage record schema）
- **Deps**: 无
- **Validation**: 迁移幂等；旧数据 origin 全部回填 "user-requested"
- **ETA**: 4h

#### W4.2 evolution_signals + curator_state 表
- **Subagent**: Database Optimizer
- **Files**:
  - `python/packages/corlinman-server/src/corlinman_server/gateway/evolution/migrations/003_curator.sql`（新）
  - Schema:
    ```sql
    -- 已有 evolution_signals 表，增 signal_type 取值
    -- 新增 curator_state 表
    CREATE TABLE curator_state (
      profile_slug TEXT PRIMARY KEY,
      last_review_at TIMESTAMP,
      last_review_duration_ms INTEGER,
      last_review_summary TEXT,
      run_count INTEGER DEFAULT 0,
      paused BOOLEAN DEFAULT 0,
      interval_hours INTEGER DEFAULT 168,  -- 7 days
      stale_after_days INTEGER DEFAULT 30,
      archive_after_days INTEGER DEFAULT 90
    );
    ```
- **Hermes 参考**: `agent/curator.py:66`（curator_state JSON）+ §SQL 示例
- **Deps**: 无
- **Validation**: 表创建 + 单测插入/查询
- **ETA**: 2h

#### W4.3 IdleReflectionTrigger（curator loop）
- **Subagent**: AI Engineer + Backend Architect
- **Files**:
  - `python/packages/corlinman-server/src/corlinman_server/gateway/evolution/curator.py`（新）— 仿 hermes `agent/curator.py:198-296`：
    - `maybe_run_curator(profile_slug)`：检查 curator_state，若 `now - last_review_at > interval_hours` 则触发
    - `apply_lifecycle_transitions(profile_slug)`：纯逻辑（不调 LLM），active→stale (>30d 未用)，stale→archived (>90d 未用)，stale→active (一旦再次使用)
    - 返回 `CuratorReport(marked_stale, archived, reactivated)`
  - `gateway/evolution/__init__.py` 注册 curator 子系统
  - `tests/evolution/test_curator.py` — 用时间穿越测三态转换
- **Hermes 参考**: `agent/curator.py:198-296`
- **Deps**: W4.1, W4.2
- **Validation**: 30 天前的 skill → 自动标 stale；再用一次 → 回 active
- **ETA**: 5h

#### W4.4 AsyncBackgroundApplier（background review fork）
- **Subagent**: AI Engineer
- **Files**:
  - `python/packages/corlinman-server/src/corlinman_server/gateway/evolution/background_review.py`（新）— 仿 hermes `agent/background_review.py:1-300`：
    - `spawn_background_review(parent_session_id, kind: "memory" | "skill" | "combined" | "curator")` 
    - 受限工具集白名单：`memory_tool`, `skill_manage`（NO terminal, NO web, NO file_write outside profile dir）
    - 继承 parent 的 provider/model 配置，但 messages 仅传 review prompt + 最近 N 轮对话
    - 写入 `_memory_write_origin="background_review"` 标记
  - 提示词模板：`evolution/prompts/{memory_review,skill_review,combined_review,curator_review}.md`
- **Hermes 参考**: 
  - `run_agent.py:1103-1125`（spawn）
  - `agent/background_review.py:33-214`（prompts）
- **Deps**: W4.1, W4.2, W3.1（需要 profile）
- **Validation**: 触发 review → 生成 KB 写入 → 标记 origin="agent-created"；超时/失败 → fallback 不污染 KB
- **ETA**: 8h

#### W4.5 User-Correction Detector + Signal Routing
- **Subagent**: AI Engineer
- **Files**:
  - `python/packages/corlinman-server/src/corlinman_server/gateway/evolution/signals/user_correction.py`（新）—
    - HookBus 订阅 `chat.message.user`
    - 用启发式（不调 LLM）+ regex 库识别纠正型语句："stop", "don't", "I said", "actually", "no", "you always X" 等
    - 写 `evolution_signals(signal_type='user_correction', skill_id=None, metadata={'text': ..., 'session_id': ...})`
  - `gateway/evolution/applier.py` 增 `apply_user_correction(signal)`：路由到 `spawn_background_review(kind='skill')` 用 user_preference_patch 提示词
  - `evolution/prompts/user_preference_patch.md`（新）— 让 review agent 把纠正写进相关 SKILL.md，而不只是 MEMORY.md
- **Hermes 参考**: `agent/background_review.py:54-111`（用户纠正→ skill body）
- **Deps**: W4.4
- **Validation**: 模拟会话："stop using bullet points" → 触发 review → 相关 skill 的 SKILL.md 出现新约束
- **ETA**: 6h

#### W4.6 Dry-run + UI 改造
- **Subagent**: Frontend Developer + Backend Architect
- **Files**:
  - 后端：`gateway/evolution/curator.py` 增 `dry_run=True` 参数 — 收集本应做的变更但不写
  - 后端：`/admin/evolution/curator/preview` 端点返回 dry-run YAML
  - 后端：`/admin/evolution/curator/run` 端点真实执行
  - 前端：`ui/app/(admin)/evolution/page.tsx` 改造：
    - 加 "Dry run preview" 按钮 + YAML diff 视图
    - skill 列表加状态徽章 (active/stale/archived) 和 origin 徽章 (bundled/user/agent)
    - origin 过滤器（侧栏）
  - 前端：`ui/components/evolution/curator-preview-dialog.tsx`（新）
- **Hermes 参考**: `agent/curator.py:303-327`（dry-run summary 输出格式）
- **Deps**: W4.3, W4.4
- **Validation**: dry-run 不改 KB；点 "Apply" 后 KB 同步变更；UI 实时刷新
- **ETA**: 5h

**Wave 4 同步点**：W4.1+W4.2 并行（独立 schema）→ W4.3+W4.4 并行（依赖 schema）→ W4.5+W4.6 并行（依赖 W4.4）。
**Wave 4 总 ETA**: 30h（如 3 agent 并行约 12h）

---

### Wave 5 — 抛光与 E2E（3 个并行 task）

#### W5.1 Playwright E2E 全链路测试
- **Subagent**: API Tester + Frontend Developer
- **Files**:
  - `ui/tests/e2e/onboard-to-admin.spec.ts`（新）— 冷启动 → admin/root 登录 → must_change_password 强制 → 改密 → 进 dashboard
  - `ui/tests/e2e/profile-lifecycle.spec.ts`（新）— 创建 profile → 切换 → SOUL 编辑 → 删除
  - `ui/tests/e2e/curator-dry-run.spec.ts`（新）— 触发 curator → 看 preview → 应用 → 看 signal 出现
- **Deps**: 所有前面波
- **Validation**: CI 绿
- **ETA**: 6h

#### W5.2 文档与首次体验
- **Subagent**: Technical Writer
- **Files**:
  - `README.md` 加 "60-second quickstart"：`docker compose up → 访问 :6005 → admin/root → 改密 → 完成`
  - `docs/quickstart.md`（新）— 截图+步骤
  - `docs/profiles.md`（新）— Profile 概念说明
  - `docs/evolution-curator.md`（新）— curator loop + dry-run 用法
- **Deps**: W5.1（截图）
- **ETA**: 4h

#### W5.3 i18n 扩充
- **Subagent**: Frontend Developer
- **Files**:
  - `ui/lib/locales/en.ts` 增所有新 key
  - `ui/lib/locales/zh-CN.ts` 增所有新 key（已有翻译规范）
  - （可选）从 hermes `web/src/i18n/` 复制 ja/ko/fr 3 种语言种子
- **Deps**: W1-W4 全部
- **ETA**: 4h

---

## 3. 并行编排建议

### 推荐 agent 分工（3-5 个 background agent 同时跑）

| Agent 角色 | 并发 task | 串行 task |
|-----------|----------|-----------|
| **Backend-A** | W1.1, W1.2, W2.2, W4.1, W4.2 | W3.1 → W3.3 → W4.3 → W4.4 → W4.5 |
| **Backend-B** | W2.3 后端, W4.6 后端 | — |
| **Frontend-A** | W1.3, W1.4, W2.1, W2.3 前端 | W3.2 → W3.4 |
| **Frontend-B** | W4.6 前端 | — |
| **QA** | W5.1, W5.2 | （等所有前置完成） |

### 关键同步点（hard barrier）
1. **End of W1**：admin/root + must_change_password 工作链路打通后，才能让 Wave 2/3 的前端假设新的 me API
2. **End of W3.1**：profile schema 定稿后，W3.2/3.3/3.4 + W4.4 才能正确写入 profile-scoped 目录
3. **End of W4.1+W4.2**：schema migration 上去后，W4.3/4.4 才能 import 新字段

### 软同步（建议而非强制）
- W2.3（credentials UI）可以单独立项与 onboard 解耦
- W3.4（profile switcher）可在 W3.2 完成后单独迭代

---

## 4. 风险 / 决策点

| 风险 | 缓解 |
|------|------|
| `admin/root` 在生产被误用 | 启动日志大写警告 + must_change_password 强制 + 文档明确写"仅本地 dev" |
| 旧用户配置文件已有 [admin] 块 → 不应覆盖 | `admin_seed.ensure_admin_credentials` 已实现 "if exists, leave alone"（见 `admin_seed.py:159`）|
| Background fork 失控写坏 KB | 工具白名单 + 文件路径白名单（只能写 profile 目录）+ 写入前 schema 校验 + dry-run 默认 |
| YAML→Profile 迁移破坏现有 4 个 agent | gated by `profiles.migrate_from_agents=true`，默认 false；保留 `agents/*.yaml` 作为 read-only 备份 |
| Curator 把用户手写 skill 误删 | provenance 过滤：仅 `origin='agent-created'` 进入 curator 范围；`pinned=true` 永久保护 |
| User-correction detector 误判正常对话为纠正 | 启发式 + 阈值 + 单元测试 corpus；后续可上小模型做意图分类（W4.5 之后的 phase 2）|

---

## 5. 落地优先级（如果时间有限）

**Must（用户硬要求）**:
- W1.1, W1.2, W1.3, W1.4 — admin/root + 账户安全

**Should**（提升新用户体验）:
- W2.1 + W2.2 — skip 路径
- W3.1 + W3.2 — Profile 基础

**Could**（hermes 借鉴亮点）:
- W4.1, W4.2, W4.3, W4.6 — 自我进化 dry-run 闭环
- W4.4, W4.5 — 自我进化全自治（最有价值但最复杂）

**Won't this round**:
- W3.3 yaml 迁移、W3.4 切换器、W5 全部 — 留下一轮

---

## 6. 决策待用户确认

- [ ] Wave 1-2 是否本轮硬交付？（保持目标：admin/root + 账户安全 + skip 向导）
- [ ] Profile 是否本轮引入？或留到下一轮？
- [ ] 自我进化 W4 选 a) dry-run + 生命周期（W4.1-3, 4.6）b) 全套（含 background fork + user-correction）
- [ ] 是否允许多 background agent 并行写代码？（建议是 — 否则 ETA 翻倍）

---

## 附录 A：hermes 自我进化映射表（一页可看）

| hermes 概念 | hermes 文件 | corlinman 落点 | 新增/复用 |
|------------|-----------|--------------|----------|
| Curator loop（idle 触发） | `agent/curator.py:198-248` | `gateway/evolution/curator.py` | 新增 |
| Background review fork | `run_agent.py:1103-1125` + `agent/background_review.py` | `gateway/evolution/background_review.py` | 新增 |
| Skill provenance | `tools/skill_usage.py:154-200` | `kb.skills.origin` 字段 | schema 扩展 |
| Skill 生命周期 (active/stale/archived) | `agent/curator.py:256-296` | `kb.skills.state` + curator 转换 | schema + 逻辑 |
| Skill 版本（SemVer） | `skills/yuanbao/SKILL.md:1-10` YAML frontmatter | `kb.skills.version` 字段 | schema |
| SKILL.md 子目录（references/templates/scripts/assets） | hermes skill 目录约定 | `<data_dir>/profiles/<slug>/skills/<name>/` 同构 | 路径约定 |
| User-correction → skill body patch | `agent/background_review.py:54-111` | `evolution/signals/user_correction.py` + `prompts/user_preference_patch.md` | 新增 |
| Dry-run | `agent/curator.py:303-327` | `/admin/evolution/curator/preview` 端点 + UI | 新增 |
| Memory MEMORY.md + USER.md | `agent/memory_manager.py` | `<data_dir>/profiles/<slug>/MEMORY.md` + `USER.md` | 文件约定 |
| FTS5 跨会话搜索 | `hermes_state.py:11` | 已有 SQLite，加 FTS5 虚表 | schema 扩展 |
| Cron jobs.json | `cron/jobs.py:65-130` | 已有 scheduled subprocess + signals | 复用 |
| Plugin 自注册 (plugin.yaml + __init__.py) | `providers/__init__.py:102-138` | `corlinman_providers/registry.py` 增 entry-point 扫描 | 新增 |

---

## 附录 B：可借鉴的 UX 模式速查

来自 hermes 前端的 8 个模式（按 ROI 排序）：

1. **slug 正则即时校验 + toast**（`ProfilesPage.tsx:66-88`）→ W3.2
2. **眼睛图标 paste-only 密钥录入**（`EnvPage.tsx:99-160`）→ W1.3, W2.3
3. **两段式 ModelPickerDialog (provider→model)**（`ModelPickerDialog.tsx:65-195`）→ W2.1
4. **provider 分组 + 隐藏未设 row**（`EnvPage.tsx:45-79`）→ W2.3
5. **破坏性操作 ConfirmDialog + AlertTriangle icon**（`confirm-dialog.tsx`）→ W1.3, W3.2
6. **Phase-state 机（OAuthLoginModal 7 阶段）**（`OAuthLoginModal.tsx:18-140`）→ 未来 OAuth provider 接入
7. **分类边栏 + 搜索（Skills/Config 页）**（`SkillsPage.tsx:100-187`）→ W3, W4.6
8. **Toast-driven 即时反馈**（`ProfilesPage.tsx:79-82`）→ 所有 mutation

来自 openclaw：
9. **Plugin SDK definePluginEntry({id, kind, configSchema, register})** → 长期插件扩展点
10. **Persona 多层 markdown（biography / core / speech_style / forbidden）**（VCPToolBox `Agent/Aemeath.txt`）→ Profile SOUL.md 结构参考
11. **Tool manifest JSON + invocationCommands schema**（`Plugin/DailyNote/plugin-manifest.json`）→ 未来 tool 注册形式化

---

## 附录 C：现有已部分完成的工作

我（主线 agent）已经做了：
- `gateway/lifecycle/admin_seed.py` ✅ 完成（含 `ensure_admin_credentials` + `resolve_admin_config_path` + 单元接口）
- `routes_admin_a/state.py` ✅ 加了 `must_change_password: bool = False` 字段
- `gateway/lifecycle/entrypoint.py` 部分：✅ 增 `from admin_seed import ...`；⚠️ `_mount_routes` 签名扩展但**未调用**

**W1.1 真正需要做的**：
1. 在 `build_app` 内 `_load_config` 之后调用 `ensure_admin_credentials`（async，可放在 lifespan startup 而非顶层）
2. 把返回的 SeededAdmin 字段塞进 `AdminState` 构造
3. `routes_admin_a/auth.py` 的 `/admin/me` response model 加 `must_change_password`
4. `change_password` 成功后 `state.must_change_password = False` 并 persist 到 TOML
5. 单测：`tests/gateway/lifecycle/test_admin_seed.py`

---

**End of Plan v1.0** · 下一步：用户确认 Must/Should/Could 范围 → 我开始派 background agent 并行执行 Wave 1。
