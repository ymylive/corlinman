# Contributing to corlinman

感谢你考虑为 corlinman 贡献代码。本文说清楚：怎么搭开发环境、提交 PR 的规则、代码风格要求。

**前置**：先读 [docs/README.md](docs/README.md) 和 [docs/architecture.md](docs/architecture.md)
了解系统全貌。计划文件单一事实来源在
`/Users/cornna/.claude/plans/openclaw-rust-python-corlinma-graceful-meerkat.md`，所有架构
决策以那里为准。

## 1. 开发环境搭建

只需一次：

```bash
git clone https://github.com/<org>/corlinman.git
cd corlinman
./scripts/dev-setup.sh
```

`dev-setup.sh` 做了这些事：

1. 检查 toolchain：Rust 1.95（由 `rust-toolchain.toml` 锁定）、Python 3.12、Node 20、pnpm 9、`uv` 和 `cargo-nextest`。缺哪个给装哪个的 hint。
2. `cargo fetch` + `uv sync` + `pnpm install`，把三个包管理器的依赖拉齐。
3. 跑 `scripts/gen-proto.sh` 一次，生成 Rust tonic 和 Python grpcio stubs。
4. 把 `core.hooksPath` 指到 `.git-hooks/`，安装 pre-commit 钩子。

之后常用命令：

```bash
cargo build                          # 编译所有 Rust crate
cargo nextest run                    # 跑 Rust 测试
uv run pytest -m "not live"          # 跑 Python 测试（非 live lane）
pnpm --filter ui test                # 跑前端测试
corlinman dev                        # 本地起 gateway + Python agent，支持热重载
```

## 2. 开发工作流

### 2.1 起新分支

```bash
git checkout main
git pull
git checkout -b feat/plugin-manifest-watcher
```

分支命名：`<type>/<short-slug>`，`type` 和下面 commit 规范里的相同。

### 2.2 写代码

参考 [docs/architecture.md](docs/architecture.md) 找你要改的 crate / package。每个 crate
会在自己目录下放一份简短 README（M1 起）说内部模块划分。改之前先看一眼。

**小 PR 优先**：一个 PR 一个关注点。大 refactor 拆成多个 PR（先结构调整，再行为变更）。

**改 proto 要谨慎**：`proto/corlinman/v1/*.proto` 的字段编号和语义不能向后不兼容。加字段
OK；改字段类型 / 删字段 / 重排编号要先开 issue 讨论。

### 2.3 提交前

pre-commit 会自动跑，但最好手动先跑一遍：

```bash
cargo fmt
cargo clippy --all-targets -- -D warnings
uv run ruff check python/
uv run ruff format python/
uv run mypy python/
pnpm --filter ui typecheck
pnpm --filter ui lint
```

有紧急情况（生产 hotfix，CI 挂但你确定你改的部分没问题）可以用逃生舱：
```bash
FAST_COMMIT=1 git commit ...
```
但 CI 上不会有 `FAST_COMMIT`，PR 仍会被 lint 拦住。

### 2.4 提 PR

```bash
git push -u origin feat/plugin-manifest-watcher
# 然后 GH 页面 open PR，或 gh cli
gh pr create
```

PR 模板会要求你填：**改动摘要**、**动机**、**测试计划**、**Changelog 条目**（按
[keep a changelog](https://keepachangelog.com/) 格式）。

## 3. 代码风格

### 3.1 Rust

- `rustfmt` 默认配置（仓库不改 `rustfmt.toml`）。
- `clippy` 以 `-D warnings` 运行；确实要 `allow` 就在 attribute 上注释理由。
- 错误类型统一用 `corlinman-core::error::CorlinmanError`，不自己定义 crate 级 Error。
- 日志一律 `tracing::` 宏，带上 `subsystem=...` 字段。不用 `println!`。
- 公开函数必须有 rustdoc。模块级文档写"这个模块管什么"一句话。
- async 里不要 `unwrap()`，也不要 `.await` 同步锁。需要同步锁就用 `parking_lot` 且锁范围最小。

### 3.2 Python

- `ruff` 配置在 `pyproject.toml`，line-length 100，启用 `E, F, I, N, UP, B, SIM, RUF`。
- 类型注解**全覆盖**，`mypy --strict` 通过（配置在 `pyproject.toml`）。
- `pydantic v2` strict 模式做所有配置和 IPC 载荷校验。
- 日志用 `structlog.get_logger(__name__).bind(subsystem=...)`；不用 `print` 或 stdlib logging。
- 自定义异常继承 `corlinman_agent.errors.CorlinmanError`。
- async 函数里的 `try/except` 必须单独处理 `asyncio.CancelledError` 并 re-raise。

### 3.3 TypeScript / UI

- `ui/` 用 Next.js 默认的 TS 和 ESLint 配置（App Router 15 模板）。
- 组件文件 PascalCase；工具模块小写 kebab。
- 不要 `any`；真需要用 `unknown` + narrow。
- shadcn/ui 组件从 `@/components/ui/*` import；不要直接改生成出来的基础组件，写 wrapper。

## 4. 测试要求

- **新代码必须带测试**。改 bug 必须先写一个能复现的失败测试，再修。
- **Rust**：单测写在模块同文件 `#[cfg(test)] mod tests`；集成测试在 `tests/`；快照用
  `insta`；属性测试用 `proptest`。
- **Python**：`tests/` 下 `pytest`；live lane（真打 provider API）标 `@pytest.mark.live`，
  默认 `-m "not live"` 跳过。
- **跨进程**：参考 `qa/scenarios/*.yaml`，新契约加一个 scenario。
- **性能敏感的改动**：跑 `corlinman qa bench` 对比 main 的 histogram，PR 描述里贴数字。

## 5. 提交信息约定（Conventional Commits）

格式：`<type>(<scope>): <subject>`，正文和 footer 可选。

```
feat(plugins): support async plugin callback via /plugin-callback

Adds oneshot::Sender wakeup keyed by taskId. Gateway middleware
parses taskId from query, matches pending task, sends payload.

Closes #42
```

允许的 `type`：

| type | 含义 |
| --- | --- |
| `feat` | 新功能（对用户可见） |
| `fix` | bug 修复 |
| `docs` | 仅文档 |
| `refactor` | 不改行为的结构调整 |
| `test` | 加测试或测试基础设施 |
| `chore` | 构建、依赖、CI、脚本 |
| `perf` | 性能优化（带 bench 数字） |

`scope` 是受影响的 crate / package / 组件，如 `plugins` / `gateway` / `agent` / `ui` / `proto` /
`docs`。多个 scope 用 `/` 分隔或省略。

**subject 用现在时祈使句**：`add X`、`fix Y`，不是 `added` / `adds`。

**不合格的 commit** 会被 `commitlint`（pre-commit 钩子里）拒。

## 6. PR 要求

合入 `main` 前所有 PR 必须：

- [ ] 所有 CI 检查绿（fmt、clippy、ruff、mypy、typecheck、nextest、pytest non-live、UI test、
  QA scenarios 1-5）
- [ ] 至少一个 reviewer approve
- [ ] Conventional Commits 标题
- [ ] 带测试（新 feature 或 bug fix）
- [ ] 文档同步更新：
  - 改 proto → 更新 [docs/architecture.md](docs/architecture.md) §5 "proto 服务速览"
  - 改 config schema → 更新 [docs/architecture.md](docs/architecture.md) §7 数据与配置组织
  - 改插件 runtime 行为 → 更新 [docs/plugin-authoring.md](docs/plugin-authoring.md)
  - 新增 metric / 可运维项 → 更新 [docs/runbook.md](docs/runbook.md)
- [ ] `CHANGELOG.md` 条目（M8 之后强制）

**禁止**：
- `--no-verify` 绕过 hooks（CI 会重跑拦住，浪费时间）
- 一个 PR 同时改 3+ 不相关的 feature
- 大量 drive-by formatting（写你改的函数就好）

## 7. 分支策略

- `main` —— 受保护。只能通过 PR 合入。线性历史（squash merge 或 rebase merge）。
- `feature/*` / `feat/*` / `fix/*` —— 短期分支，从 `main` 拉，合回 `main`。
- `release/1.x` —— 发布分支（M8 之后引入）。只接受 cherry-pick 的 fix。
- 不鼓励长期 topic branch；拆小 PR 勤合 main。

## 8. 行为准则

工作语言中英皆可（docs 里为了中文用户以中文为主，技术术语保留英文；代码 comment 英文）。
尊重他人、就事论事、不人身攻击。

安全漏洞请**不要**公开开 issue。发邮件到 `TODO: security contact email` 报告（维护者补）。

## 9. 还没决定的东西（M0 待定）

以下在 M0 过程中会定稿，当前都是占位：

- 具体 GitHub organization 名（影响镜像 namespace、crate publish target）
- LICENSE 的署名（现在是 `MIT` 但 copyright holder 还空）
- security contact 邮箱
- Code of Conduct 文件（大概率用 Contributor Covenant 2.1）

如果你的 PR 触到这些，PR 描述里 ping 维护者讨论。

## 延伸阅读

- 架构: [docs/architecture.md](docs/architecture.md)
- 插件作者: [docs/plugin-authoring.md](docs/plugin-authoring.md)
- 运维手册: [docs/runbook.md](docs/runbook.md)
- 里程碑: [docs/milestones.md](docs/milestones.md)
- 完整计划: `/Users/cornna/.claude/plans/openclaw-rust-python-corlinma-graceful-meerkat.md`
