# Agent Brain Memory Curator 任务拆解草案

状态：草案  
分支：`feat/agent-brain-memory-curator`  
目标仓库：`ymylive/corlinman`  
日期：2026-05-10

## 1. 总目标

为 Corlinman 增加一套“会话后知识整理系统”，让每个子 agent 可以拥有类似 Obsidian 的长期知识网络。

系统不应把完整聊天记录直接塞进长期记忆，而应在每次会话结束后完成以下动作：

1. 读取本次会话内容。
2. 按主题拆分出多个可长期保存的知识候选。
3. 过滤临时闲聊、低价值内容和敏感内容。
4. 将稳定事实、用户偏好、项目背景、任务决策、agent 人设信息整理成 Markdown 节点。
5. 检索已有知识节点，判断是更新、链接、合并还是新建。
6. 使用 `[[双链]]`、标签、frontmatter 元数据形成知识网络。
7. 将通过审核或低风险自动确认的节点写入 agent brain vault。
8. 同步写入现有 MemoryHost / vector / RAG 检索系统，供后续 agent 调用。

## 2. 非目标

第一版不做以下内容：

1. 不做完整 Obsidian UI。
2. 不做复杂图谱可视化。
3. 不直接替换现有 `sessions`、`episodes`、`persona`、`user-model`、`goals`。
4. 不把所有聊天内容默认写入长期记忆。
5. 不在 agent 实时回复链路里阻塞执行记忆整理。
6. 不一开始支持所有外部知识库同步，例如 Notion、飞书、企业 Wiki。

## 3. 现有基础

可以复用的现有模块：

1. `sessions.sqlite`
   - 保存原始会话消息。
   - 适合作为会话后整理的输入源。

2. `corlinman-episodes`
   - 已经提供 episodic memory 的摘要、切片、重要性判断和嵌入流程。
   - 适合作为 Memory Curator 的上游输入。

3. `corlinman-user-model`
   - 已经能从会话中提取用户兴趣、语气偏好、长期偏好。
   - 可作为用户偏好类知识节点的来源之一。

4. `corlinman-persona`
   - 已经支持 per-agent runtime state。
   - 可扩展为子 agent 脑库中的人设状态来源。

5. `corlinman-goals`
   - 已经支持目标、证据、反思、演进信号。
   - 可用于任务目标、长期计划、决策记录类节点。

6. `corlinman-memory-host`
   - 已经定义统一 MemoryHost 接口。
   - 新知识节点最终应通过该接口进入检索层。

7. `corlinman-vector`
   - 已经支持 SQLite、FTS、向量检索、标签和 namespace。
   - 可用于相似节点检索、链接候选召回和 RAG 上下文组装。

## 4. 推荐架构

新增一个逻辑层：`AgentBrainMemoryCurator`。

职责：

1. 从 session / episode 读取会话材料。
2. 调用 distiller 抽取主题、事实、偏好、决策、任务、人物、项目上下文。
3. 调用 classifier 判断每条候选的记忆类型、风险等级和保留价值。
4. 调用 linker 检索已有知识节点，生成链接、合并建议或新建建议。
5. 调用 writer 生成 Markdown 文件或待审核草稿。
6. 调用 indexer 将最终确认的节点写入 MemoryHost / vector。
7. 记录审计日志，支持查看、回滚、删除和重建索引。

推荐链路：

```text
sessions.sqlite / episodes.sqlite
        |
        v
SessionBundle
        |
        v
Topic Splitter
        |
        v
Memory Candidate Extractor
        |
        v
Risk + Importance Classifier
        |
        v
Existing Node Retriever
        |
        v
Link / Merge / Create Planner
        |
        v
Draft Review Queue
        |
        v
Markdown Vault Writer
        |
        v
MemoryHost + Vector Upsert
```

## 5. 第一版策略

默认策略建议为“半自动写入”：

1. 低风险稳定内容自动写入。
   - 项目背景
   - 已确认偏好
   - 明确任务状态
   - 明确技术决策
   - 子 agent 的长期职责设定

2. 高风险或不确定内容进入待审核草稿。
   - 隐私信息
   - 账号、密钥、身份信息
   - 财务、医疗、法律相关内容
   - 模型推断出来但用户没有明确确认的人设
   - 与已有记忆冲突的内容

3. 配置层保留三种模式：

```toml
[agent_brain_memory]
write_policy = "semi_auto" # draft_first | semi_auto | auto
```

第一版默认使用 `semi_auto`，但实现时要让 `draft_first` 和 `auto` 可以扩展。

## 6. 数据目录规划

推荐 vault 目录：

```text
knowledge/
  agent-brain/
    global/
      projects/
      people/
      preferences/
      decisions/
      tasks/
      inbox/
    agents/
      <agent_id>/
        persona/
        projects/
        skills/
        decisions/
        tasks/
        inbox/
```

说明：

1. `global` 保存跨 agent 的用户和项目知识。
2. `agents/<agent_id>` 保存某个子 agent 自己的脑库。
3. `inbox` 保存未审核草稿或无法归类的候选记忆。
4. 后续可以增加 `archive`、`conflicts`、`deleted` 或 `history`。

## 7. Markdown 节点格式

每个知识节点建议包含 YAML frontmatter：

```markdown
---
id: mem_20260510_project_corlinman_memory
tenant_id: default
agent_id: planner-agent
scope: agent
kind: project_context
status: active
confidence: 0.86
risk: low
source:
  session_id: sess_xxx
  episode_id: ep_xxx
  created_from: session_curator
created_at: 2026-05-10T00:00:00Z
updated_at: 2026-05-10T00:00:00Z
links:
  - "[[Corlinman Agent 架构]]"
  - "[[长期记忆系统]]"
tags:
  - corlinman
  - memory
  - agent-brain
---

# Corlinman 的小 agent 大脑记忆系统

## 摘要

用户希望每个子 agent 拥有类似 Obsidian 的知识网络，用于保存长期项目背景、偏好、任务决策和人设信息。

## 关键事实

- 该记忆系统不应直接保存完整聊天记录。
- 会话结束后应按主题拆分为多个长期知识节点。

## 相关节点

- [[Corlinman Agent 架构]]
- [[长期记忆系统]]
```

## 8. 记忆类型

第一版建议支持以下 `kind`：

1. `project_context`
   - 项目背景、仓库结构、长期方向。

2. `user_preference`
   - 用户偏好、工作方式、表达偏好。

3. `agent_persona`
   - 子 agent 的职责、人设、工作风格、边界。

4. `decision`
   - 已确认技术决策、产品决策、架构决策。

5. `task_state`
   - 任务进度、下一步、阻塞点。

6. `concept`
   - 可复用概念，例如“Obsidian 式知识网络”。

7. `relationship`
   - 人、项目、agent、工具之间的关系。

8. `conflict`
   - 与已有记忆冲突、需要人工确认的内容。

## 9. 任务拆解

### 阶段 0：确认产品边界

目标：确定第一版到底做多大，避免一开始变成完整知识管理平台。

任务：

1. 确认默认写入策略。
   - 推荐：`semi_auto`。
   - 备选：`draft_first`。

2. 确认第一版是否只做后端和 CLI。
   - 推荐：先做后端 + CLI。
   - Admin UI 放到第二版。

3. 确认第一版支持的记忆范围。
   - 推荐：global brain + per-agent brain。

4. 确认 Markdown vault 是否作为主存储。
   - 推荐：Markdown 作为人可读主文档，SQLite/vector 作为索引。

验收点：

1. 有明确 scope。
2. 有明确非目标。
3. 有默认策略。
4. 后续开发不会因为边界不清反复返工。

### 阶段 1：设计正式 spec

目标：将本草案升级成正式设计文档。

任务：

1. 写 `docs/superpowers/specs/2026-05-10-agent-brain-memory-curator-design.md`。
2. 明确模块边界。
3. 明确配置项。
4. 明确目录结构。
5. 明确 Markdown schema。
6. 明确风险分类和写入策略。
7. 明确与 `episodes`、`MemoryHost`、`vector` 的关系。
8. 明确测试范围。
9. 自检文档是否存在占位符、矛盾和模糊表述。

验收点：

1. spec 可以直接转成 implementation plan。
2. 用户确认设计方向。
3. 不需要在实现阶段重新讨论核心架构。

### 阶段 2：建立数据模型

目标：定义 Memory Curator 的内部数据结构。

任务：

1. 定义 `SessionBundle`。
   - session_id
   - tenant_id
   - user_id
   - agent_id
   - messages
   - tool events
   - timestamps

2. 定义 `MemoryCandidate`。
   - candidate_id
   - topic
   - kind
   - summary
   - evidence
   - confidence
   - risk
   - source refs

3. 定义 `KnowledgeNode`。
   - node_id
   - title
   - path
   - kind
   - content
   - frontmatter
   - links
   - tags

4. 定义 `LinkPlan`。
   - update existing
   - merge into existing
   - create new
   - create new and link
   - send to review

5. 定义 `CuratorRun`。
   - run_id
   - input refs
   - outputs
   - decision log
   - errors
   - rollback refs

验收点：

1. 每个数据结构有清晰字段。
2. 字段能序列化。
3. 能被单元测试覆盖。
4. 能和 Markdown frontmatter 对应。

### 阶段 3：读取会话输入

目标：让 curator 能稳定读取会话材料。

任务：

1. 支持从 `sessions.sqlite` 读取指定 session。
2. 支持从 `episodes.sqlite` 读取已生成 episode。
3. 支持按 tenant / agent / time range 拉取候选会话。
4. 处理消息顺序、工具调用、空内容和异常中断会话。
5. 对输入做基础脱敏。
6. 生成统一 `SessionBundle`。

验收点：

1. 可以通过 session_id 构造完整输入。
2. 不依赖 UI。
3. 能处理空会话和半截会话。
4. 不把明显敏感信息原样送入后续 LLM。

### 阶段 4：主题拆分和候选记忆抽取

目标：从会话里提取值得长期保存的主题。

任务：

1. 设计主题拆分 prompt 或规则。
2. 区分短期上下文和长期知识。
3. 抽取以下候选：
   - 用户偏好
   - 项目背景
   - 技术决策
   - 任务状态
   - 子 agent 人设
   - 概念定义
   - 关系信息
4. 为每个候选保存 evidence。
5. 计算初始 confidence。
6. 标记不应保存的候选。

验收点：

1. 一次复杂会话可以拆成多个主题候选。
2. 候选不是原始聊天复制粘贴。
3. 每个候选都能追溯到原始 session。
4. 候选数量可控，避免碎片爆炸。

### 阶段 5：风险分类和写入策略

目标：决定哪些记忆可以自动写入，哪些必须审核。

任务：

1. 定义 risk：
   - low
   - medium
   - high
   - blocked

2. 定义 status：
   - draft
   - approved
   - active
   - rejected
   - archived
   - conflict

3. 实现敏感内容检测。
   - 密钥
   - 邮箱
   - 手机号
   - 身份信息
   - URL/token
   - 私人财务/医疗/法律内容

4. 实现策略判断。
   - `draft_first`：全部进草稿。
   - `semi_auto`：低风险自动写，高风险进草稿。
   - `auto`：直接写，但保留审计和回滚。

验收点：

1. 高风险内容不会默认进入 active memory。
2. 每条自动写入的记忆都有 reason。
3. 可以通过配置切换策略。
4. 测试覆盖常见敏感内容。

### 阶段 6：已有节点检索和链接计划

目标：像 Obsidian 一样把新知识接入已有网络。

任务：

1. 从 vault 和 vector 读取已有节点。
2. 对每个 MemoryCandidate 做相似检索。
3. 根据相似度、标签、kind、时间和 confidence 生成 LinkPlan。
4. 决定：
   - 更新已有节点
   - 合并到已有节点
   - 新建并链接已有节点
   - 新建独立节点
   - 标记冲突
5. 生成 `[[双链]]`。
6. 生成 tags。
7. 生成反向链接需要的 metadata。

验收点：

1. 相似主题不会大量重复建节点。
2. 新主题可以独立成节点。
3. 相关主题会产生链接。
4. 冲突内容不会直接覆盖旧记忆。

### 阶段 7：Markdown writer

目标：将知识节点写入可读、可追踪、可编辑的 Markdown vault。

任务：

1. 生成安全文件名。
2. 写入 YAML frontmatter。
3. 写入正文结构。
4. 支持创建新节点。
5. 支持追加更新已有节点。
6. 支持写入 inbox 草稿。
7. 支持冲突节点单独保存。
8. 支持 dry-run。

推荐正文结构：

```markdown
# 标题

## 摘要

## 关键事实

## 决策 / 偏好 / 状态

## 证据来源

## 相关节点
```

验收点：

1. Markdown 可被 Obsidian 打开。
2. frontmatter 可被程序解析。
3. 文件路径稳定。
4. 重复运行不会无限重复追加。

### 阶段 8：索引同步

目标：让写入的 Markdown 知识能被 agent 检索使用。

任务：

1. 将 active 节点 upsert 到 MemoryHost。
2. 将 Markdown 内容切 chunk。
3. 写入 vector / FTS。
4. 保存 namespace。
5. 保存 agent_id / tenant_id / kind / tags。
6. 支持单节点重建索引。
7. 支持整个 vault 重建索引。

验收点：

1. 新写入节点能被 RAG 检索到。
2. per-agent brain 不污染其他 agent。
3. global brain 能被多个 agent 使用。
4. 删除或归档节点后索引能同步更新。

### 阶段 9：CLI 和任务入口

目标：先提供可操作入口，方便验证后端链路。

任务：

1. 增加 CLI 命令：

```text
corlinman-agent-brain curate-session --session-id <id>
corlinman-agent-brain curate-latest --agent-id <id>
corlinman-agent-brain review --run-id <id>
corlinman-agent-brain approve --draft-id <id>
corlinman-agent-brain reject --draft-id <id>
corlinman-agent-brain rebuild-index --agent-id <id>
```

2. 支持 dry-run。
3. 输出本次生成了哪些节点。
4. 输出哪些自动写入、哪些进入草稿、哪些发生冲突。
5. 支持 JSON 输出，方便后续 Admin UI 使用。

验收点：

1. 不依赖前端即可完整跑通链路。
2. 用户能看到每次 curator 做了什么。
3. 可以手动审核草稿。
4. 可以重建索引。

### 阶段 10：调度和会话后触发

目标：让系统能在会话结束后自动整理。

任务：

1. 定义会话结束信号。
2. 支持定时任务扫描未整理 session。
3. 避免重复处理同一个 session。
4. 避免在聊天实时链路中阻塞。
5. 支持失败重试。
6. 支持 curator run 状态表。

验收点：

1. 会话结束后可以自动触发整理。
2. 同一 session 不会被重复写入。
3. 失败可以追踪和重试。
4. 不影响 chat latency。

### 阶段 11：审核、回滚和审计

目标：长期记忆必须可控、可解释、可撤销。

任务：

1. 记录每次 curator run。
2. 记录每个候选为什么自动写入或进入草稿。
3. 记录每个 Markdown 文件的创建、修改和来源。
4. 支持回滚某次 run 的写入。
5. 支持删除节点并同步删除索引。
6. 支持人工将 draft 变为 active。
7. 支持人工合并节点。

验收点：

1. 能解释每条记忆从哪里来。
2. 能撤销一次错误写入。
3. 审核记录不会丢。
4. 不会出现 Markdown 删除但 vector 里仍能搜到的情况。

### 阶段 12：后续 Admin UI

目标：在核心后端稳定后，提供图形化管理界面。

任务：

1. 展示 curator runs。
2. 展示待审核 drafts。
3. 展示每个 agent 的 brain vault。
4. 支持 approve / reject / edit。
5. 支持查看相关节点。
6. 支持按 agent、kind、tag、risk 过滤。
7. 后续再考虑图谱视图。

验收点：

1. 非命令行用户能管理长期记忆。
2. 能安全审核高风险草稿。
3. 能看见 agent brain 的结构。

## 10. 测试计划

### 单元测试

覆盖：

1. Markdown frontmatter 生成。
2. 文件名生成。
3. risk 分类。
4. write policy 判断。
5. LinkPlan 规则。
6. 敏感信息检测。
7. 重复运行幂等性。

### 集成测试

覆盖：

1. 从 session 读取到生成 candidate。
2. 从 candidate 到 Markdown draft。
3. 从 approved node 到 vector upsert。
4. 相似节点更新而不是重复创建。
5. per-agent namespace 隔离。
6. 回滚后 Markdown 和索引一致。

### 端到端测试

覆盖：

1. 构造一段包含多个主题的会话。
2. curator 拆分为多个知识节点。
3. 低风险节点自动 active。
4. 高风险节点进入 inbox。
5. 后续 query 能检索到 active 节点。

## 11. 实现难点

### 11.1 长期记忆和临时聊天的边界

难点：

用户会话里有大量短期上下文。如果直接保存，会污染长期记忆。

应对：

1. 只保存稳定事实、明确偏好、任务状态、决策和项目背景。
2. 为每个候选加 confidence。
3. 不确定内容默认 draft。

### 11.2 主题拆分粒度

难点：

拆太细会产生大量碎片，拆太粗会失去知识网络价值。

应对：

1. 每个候选必须能独立命名。
2. 每个候选必须有明确 kind。
3. 每次会话默认限制候选数量。
4. 对低价值候选直接丢弃。

### 11.3 合并和链接判断

难点：

相似内容可能应该更新旧节点，也可能只是相关但不应合并。

应对：

1. 同 kind + 高相似度才允许 merge。
2. 不同 kind 默认 link，不 merge。
3. 冲突内容进入 conflict。
4. 所有 merge 都保留来源证据。

### 11.4 隐私和敏感信息

难点：

长期记忆比短期上下文风险更高，一旦写入会反复影响未来 agent。

应对：

1. 写入前做脱敏。
2. 高风险内容默认 draft 或 blocked。
3. frontmatter 记录 risk。
4. 提供删除和回滚。

### 11.5 per-agent brain 和 global brain 的边界

难点：

某些记忆属于全局用户，某些只属于一个子 agent。边界不清会导致上下文污染。

应对：

1. 所有节点必须有 scope。
2. scope 取值建议为 `global`、`agent`、`project`。
3. 检索时默认按 agent_id + global 两层取上下文。
4. 跨 agent 共享必须显式标记。

### 11.6 Markdown 和索引一致性

难点：

Markdown 是人可读源，vector 是机器检索索引，两者可能不一致。

应对：

1. 每次写入生成 run log。
2. 每个 indexed chunk 保存 source file path 和 node_id。
3. 支持 rebuild-index。
4. 删除和归档必须同步索引。

### 11.7 幂等性

难点：

同一会话被重复处理时，不能重复生成一堆节点。

应对：

1. curator run 表记录 session_id。
2. candidate 生成稳定 hash。
3. Markdown 节点使用稳定 id。
4. writer 支持 update 而不是重复 append。

### 11.8 模型幻觉和错误记忆

难点：

LLM 可能从会话里过度推断用户偏好或项目事实。

应对：

1. 每条记忆必须有 evidence。
2. 没有 evidence 的候选丢弃。
3. 推断型内容 confidence 降低。
4. 高影响记忆进入 draft。

## 12. 里程碑

### M1：设计确认

产出：

1. 正式设计 spec。
2. Markdown schema。
3. write policy。
4. 模块边界。

完成标准：

用户确认设计可以进入 implementation plan。

### M2：最小可运行链路

产出：

1. 读取 session。
2. 生成 MemoryCandidate。
3. 生成 Markdown draft。
4. dry-run CLI。

完成标准：

可以对指定 session 生成待审核 Markdown 草稿。

### M3：知识网络写入

产出：

1. 相似节点检索。
2. LinkPlan。
3. active Markdown 写入。
4. Obsidian 双链。

完成标准：

新会话能自动生成或链接知识节点。

### M4：检索集成

产出：

1. MemoryHost upsert。
2. vector / FTS 同步。
3. namespace 隔离。

完成标准：

agent 后续能检索到对应脑库知识。

### M5：审核和回滚

产出：

1. draft review。
2. approve / reject。
3. run log。
4. rollback。

完成标准：

错误记忆可以被发现、拒绝或撤销。

### M6：自动触发

产出：

1. 会话后触发。
2. 定时扫描。
3. 重试机制。

完成标准：

系统可以在不阻塞聊天的情况下自动整理记忆。

## 13. 建议第一轮实现范围

第一轮只实现最小闭环：

1. CLI 手动触发。
2. 读取指定 session。
3. 抽取主题候选。
4. 生成 Markdown 草稿。
5. 检索已有节点并生成链接建议。
6. 支持 approve 后写入 active vault。
7. 写入 MemoryHost / vector。
8. 提供 dry-run 和基础测试。

不做：

1. Admin UI。
2. 图谱可视化。
3. 外部知识库同步。
4. 全自动后台调度。
5. 复杂多模型评审。

## 14. 待确认问题

后续进入正式 spec 前，需要确认：

1. 第一版默认写入策略是否采用 `semi_auto`。
2. Markdown vault 是否作为人可读主存储。
3. 第一版是否只做 CLI，不做 Admin UI。
4. 每个子 agent 的 brain 是否默认可以读取 global brain。
5. 高风险内容是否一律进入 draft。
6. 是否需要先支持中文标题和中文标签。

