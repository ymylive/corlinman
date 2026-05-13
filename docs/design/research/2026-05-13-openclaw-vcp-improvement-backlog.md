# openclaw + vcp-tools 改进点 backlog（草稿，待核验）

Status: **URL verified 2026-05-13** — repo handles confirmed via `gh repo view`. Feature claims (multi-agent routing, geodesic reranking, etc.) remain second-hand from Explore agent's web search; concrete improvement adoption still requires source-level read of each candidate.
Date: 2026-05-13.
Owner: B 子项目。
Related: `CREDITS.md`（两个上游仓库 URL 当前为占位 placeholder）。

---

## ⚠️ Verification 状态

| 主张 | 状态 |
|---|---|
| openclaw 官方仓库 = `https://github.com/openclaw/openclaw` | ✅ 已核验（gh repo view, 2026-05-13）"personal AI assistant" |
| VCPToolBox 官方仓库 = `https://github.com/lioensky/VCPToolBox` | ✅ 已核验（gh repo view, 2026-05-13）VCP 协议 + 分布式插件引擎 |
| Verdent Guides 对比文章 `https://www.verdent.ai/guides/claw-code-claude-code-vs-openclaw` | **未核验** |
| Feature 主张（multi-agent / voice / Live Canvas 等）| **二手描述**。Agent 转述了搜索结果，未读源码本身 |

**采纳前必须做**：人工跑 `gh repo view openclaw/openclaw` 和 `gh repo view lioensky/VCPToolBox`，确认仓库存在且 star/活跃度匹配预期，再把 URL 落到 CREDITS.md。如果其中一个 URL 不对，应替换为正确 URL 后再继续。

---

## TL;DR

Corlinman 已经从 openclaw（hook 事件总线、skills manifest、channel 抽象）和 VCPToolBox（TOOL_REQUEST 协议、TVStxt 变量级联、字符卡、TagMemo EPA 数学）借鉴了 7 个核心机制。但两个上游仍在演化，**待核验**的潜在改进点共 12 条：openclaw 那边主要是多代理路由、语音原语、Live Canvas A2UI、沙箱执行；VCPToolBox 那边主要是分布式插件引擎、TagMemo V8 几何重排序、元思考、外部变量加载。

**最高优先级（如果核验属实）**：
1. TagMemo V8 几何重排序（vcp）—— corlinman 目前用 PCA + Gram-Schmidt，几何重排序在长上下文召回上有明显提升空间
2. 语音/唤醒词（openclaw）—— corlinman 已有 TTS 但没有 inbound voice transport 与 wake word
3. 沙箱执行（openclaw）—— 多租户与不信任 agent 场景需要

---

## openclaw 改进机会（5 条）

> **核验前提**：以下都假定 openclaw 仓库存在且如 agent 所述。如果上游仓库不是这个项目，本节作废。

### 1. 多代理路由与会话隔离（HIGH）
- **上游**：单一频道流路由到多个独立 workspace 的 agent，每个 agent 自己的 session
- **corlinman 缺**：gateway 路由是单 agent / session 模型；HookEvent 假设单 agent 上下文
- **收益**：团队协作、角色化 agent pool、不重配频道就能分流
- **工作量**：中（gateway 路由层加 session→agent map）

### 2. 语音 & 唤醒词原语（HIGH）
- **上游**：macOS/iOS wake word + Android 持续 voice，voice 是原生 transport
- **corlinman 缺**：`MessageTranscribed` event 有但无 inbound voice channel adapter 或 wake-word detector
- **收益**：免提交互、语音优先 UX、IoT/车载
- **工作量**：大（新增 `VoiceChannel` trait、Porcupine/Whisper 集成、音频缓冲、DTX）

### 3. Live Canvas & A2UI（MEDIUM-HIGH）
- **上游**：agent 驱动的可视工作区，可在前端 push 交互组件（按钮、表单、滑块、媒体画廊）
- **corlinman 缺**：admin UI 是静态的，agent 不能向用户推送交互元素
- **收益**：agent 主动 UX、零轮询
- **工作量**：大（双向消息 schema、前端 React 组件注册表、远控状态、Canvas→agent event 路由）

### 4. Cron / 计划任务工具（MEDIUM）
- **上游**：把 cron 列为 first-class tool，agent 可安排周期任务
- **corlinman 缺**：tool plugins 无 cron 抽象；周期任务靠 prompt 自管
- **收益**：减少周期任务 prompt 模板、统一 schedule
- **工作量**：小到中（接 `tokio-cron` 或 APScheduler）

### 5. 沙箱执行 & 运行时选择（MEDIUM）
- **上游**：非主 session 默认 Docker 沙箱，可选 SSH/OpenShell
- **corlinman 缺**：无内建沙箱抽象，代码执行假设 in-process
- **收益**：安全边界、不信任 agent / 用户的多租户隔离
- **工作量**：大（Executor trait 三套实现 + 沙箱生命周期 + 资源限额）

---

## VCPToolBox 改进机会（7 条）

> **核验前提**：同上。

### 1. 分布式插件引擎（HIGH）
- **上游**：插件运行时 87 个插件目录可分区跨机编排，元级任务均衡
- **corlinman 缺**：skills 单 gateway 进程注册，无跨机插件发现 / 远程执行 / 分布式任务队列
- **收益**：水平扩展、区域部署、跨团队复用、故障隔离
- **工作量**：很大（分布式 registry、RPC bridge、heartbeat、状态同步）

### 2. TagMemo V8 几何重排序（HIGH）
- **上游**：拓扑流形上的 geodesic reranking，配 logic depth + resonance 评分，O(1) 推断复杂度
- **corlinman 缺**：EPA 投影用基线 Gram-Schmidt + PCA 权重；检索仍是向量距离
- **收益**：长上下文场景语义召回更准、噪音更少
- **工作量**：中（流形学习后端 + geodesic 距离 + resonance 校准）

### 3. 元思考系统（MEDIUM）
- **上游**：agent 在提交动作前自反思的层
- **corlinman 缺**：无显式元思考 / 反思层
- **收益**：错误恢复、推理透明、可审计
- **工作量**：中（prompt 结构 + Claude extended thinking fallback + 反思事件路由）

### 4. 外部变量加载 & 递归解析（MEDIUM）
- **上游**：变量可从外部文件加载（`{{Agent:file.txt}}`），递归解析；模板跨 Tar/Var/Sar 组合不重复
- **corlinman 缺**：TVStxt 有级联但无文件加载语法，大模板库容易重复
- **收益**：persona DRY、配置继承、多租户模板共享
- **工作量**：小到中（扩展 TVStxt 解析器 + 文件 IO + ACL + 模板缓存）

### 5. 键控字符卡子集（MEDIUM）
- **上游**：字符卡可按 role 关键字过滤（`{{Agent:role=developer}}`），session 中动态切角色
- **corlinman 缺**：字符卡加载是 per-session 静态
- **收益**：role-specific dispatch 节省 prompt token
- **工作量**：小（加 role 过滤 + active role 入 session state）

### 6. TOOL_REQUEST 信封 schema 版本号（LOW-MEDIUM）
- **上游**：信封头含隐式 schema 版本号，宽容容错降级
- **corlinman 缺**：`protocol/block.rs` 宽容但没编码 schema 版本
- **收益**：前向兼容、工具升级更易
- **工作量**：小（信封加 `version: <int>` 字段）

### 7. 语义 tag 序列分析（MEDIUM）
- **上游**：TagMemo V8 通过 tag 序列分析捕获用户认知模式（区别于内容检索：track 用户怎么*想*）
- **corlinman 缺**：session 进化观察器跟踪 tool calls 和 message events 但不合成用户意图模式 / 学习曲线
- **收益**：个性化 prompt 适配、多 session ranking 改进、用户偏好学习
- **工作量**：中到大（HMM / attention 序列模型 + tag 抽取启发式 + user model 持久化）

---

## 兼容性观察 ("we got this wrong"?)

Agent 报告 **未发现根本性偏离**，但记录两个微差异（需核验）：

1. **Hook 事件命名**：corlinman `MessageTranscribed` / `MessagePreprocessed` vs openclaw 文档里据称 `message.transcribed` / `message.preprocessed` 点记号。若计划做跨厂商集成，JSON 序列化要确认 `tag = "kind"` 一致
2. **变量级联术语**：corlinman 用 Tar/Var/Sar 命名；VCPToolBox 据说用 `{{Date}}` 等 system var 名。功能等价，但兼容文档应有 mapping table

---

## 推荐落地优先级（待核验后）

1. **立刻可做**：核验仓库 URL，把 `CREDITS.md` 占位 URL 替换为真实地址
2. **近期高收益**：TagMemo V8 几何重排序、语音/wake word、分布式插件发现
3. **中期**：Live Canvas + A2UI、元思考
4. **未来研究**：沙箱执行策略、语义 tag 序列学习

---

## 下一步

1. 跑 `gh repo view openclaw/openclaw` 与 `gh repo view lioensky/VCPToolBox`，确认仓库存在 + 活跃度
2. 如果两个 URL 都正确，把 CREDITS.md 的两处「Repository: `https://github.com/` — specific URL to be added」替换
3. 上面 12 条改进点逐条对照源码核验（每条 0.5–2 小时）
4. 把核验通过的高优先项写入 issue / 后续 spec backlog

本文档落地后即从 「未核验草稿」 转为 「已核验 backlog」，可作为后续若干 PR 的输入。
