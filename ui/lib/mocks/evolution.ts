/**
 * Mock proposals for /evolution while the gateway endpoints (Wave 1-C) are
 * landing in parallel. Activated by `NEXT_PUBLIC_MOCK_MODE=1` via the
 * `mock:` escape hatch in `lib/api.ts`. Once the real `/admin/evolution/*`
 * routes ship, this file can be deleted — none of the production code
 * paths reference it directly.
 */

import type { EvolutionProposal } from "@/lib/api";

const NOW = Date.UTC(2026, 3, 25, 14, 0, 0); // matches currentDate

export const MOCK_EVOLUTION_PENDING: EvolutionProposal[] = [
  {
    id: "evo_01HZAB",
    kind: "memory_op",
    target: "memory/sessions/qq:group:881133/snippets",
    diff: [
      "--- a/memory/sessions/qq:group:881133/snippets.md",
      "+++ b/memory/sessions/qq:group:881133/snippets.md",
      "@@ -12,3 +12,5 @@",
      " - 周三例会顺延到周四 09:30",
      " - 索菲亚不喝豆奶，备注里加一行",
      "+- 三月底前完成蜡封样本归档",
      "+- 蜂蜡熔点 62°C，与 D2 批次记录一致",
    ].join("\n"),
    reasoning:
      "三周内出现 4 次此 session 用户复述同一备注，置信度 0.86。新增条目可减少回溯检索 ~3 次/周。",
    risk: "low",
    status: "pending",
    signal_ids: [4112, 4133, 4170, 4201],
    trace_ids: ["t-2a91c3", "t-2a91d0"],
    created_at: NOW - 1000 * 60 * 4,
  },
  {
    id: "evo_01HZAC",
    kind: "tag_rebalance",
    target: "tagmemo/axes/calendar",
    diff: [
      "--- a/tagmemo/axes/calendar.toml",
      "+++ b/tagmemo/axes/calendar.toml",
      "@@ -3,7 +3,7 @@",
      ' weight = 0.62',
      "-fanout = 12",
      "+fanout = 8",
      "@@ -15,2 +15,3 @@",
      " synonyms = [\"日程\", \"安排\"]",
      "+synonyms = [\"日程\", \"安排\", \"约定\"]",
    ].join("\n"),
    reasoning:
      "calendar 轴近 7 天激活率 21%（同侪平均 38%），fanout 调低后近义检索误杀下降 11%。",
    risk: "medium",
    status: "pending",
    signal_ids: [4084, 4129],
    trace_ids: ["t-29ee10"],
    created_at: NOW - 1000 * 60 * 33,
  },
  {
    id: "evo_01HZAD",
    kind: "skill_prompt",
    target: "skills/diary_writer/prompt.md",
    diff: [
      "--- a/skills/diary_writer/prompt.md",
      "+++ b/skills/diary_writer/prompt.md",
      "@@ -8,4 +8,5 @@",
      " - 用第一人称",
      "-- 不要列出当天所有事件",
      "+- 优先记述对你触动最深的一件事，其余略写",
      "+- 若当天无对话，则记你对环境的观察（光线、声音、气味）",
    ].join("\n"),
    reasoning:
      "12 条用户反馈中 9 条提到 '日记太流水账'。新提示在影子评估集上情感密度 +0.18，长度 -22%。",
    risk: "high",
    status: "pending",
    signal_ids: [3990, 4001, 4055, 4112, 4188],
    trace_ids: ["t-2872fa", "t-2872fb", "t-2891aa"],
    created_at: NOW - 1000 * 60 * 60 * 2 - 1000 * 17,
  },
];
