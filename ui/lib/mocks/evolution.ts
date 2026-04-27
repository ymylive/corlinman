/**
 * Mock proposals for /evolution while the gateway endpoints (Wave 1-C) are
 * landing in parallel. Activated by `NEXT_PUBLIC_MOCK_MODE=1` via the
 * `mock:` escape hatch in `lib/api.ts`. Once the real `/admin/evolution/*`
 * routes ship, this file can be deleted — none of the production code
 * paths reference it directly.
 */

import type {
  BudgetSnapshot,
  EvolutionProposal,
  HistoryEntry,
  MetricSnapshot,
} from "@/lib/api";

const NOW = Date.UTC(2026, 3, 25, 14, 0, 0); // matches currentDate
const ONE_WEEK_MS = 1000 * 60 * 60 * 24 * 7;

export const MOCK_EVOLUTION_BUDGET: BudgetSnapshot = {
  enabled: true,
  window_start_ms: NOW - ONE_WEEK_MS,
  window_end_ms: NOW,
  weekly_total: { limit: 15, used: 4, remaining: 11 },
  per_kind: [
    { kind: "memory_op", limit: 5, used: 2, remaining: 3 },
    { kind: "skill_update", limit: 3, used: 1, remaining: 2 },
    { kind: "tag_rebalance", limit: 4, used: 1, remaining: 3 },
  ],
};

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

// ─── Phase 3 W2 (3-2D): Approved + History fixtures ────────────────────────

/** Helper: a `MetricSnapshot` JSON with hand-tuned counts. Keeps the
 * mocks readable rather than scattering literal objects through the file. */
function snapshot(target: string, counts: Record<string, number>): MetricSnapshot {
  return {
    target,
    captured_at_ms: NOW - 1000 * 60 * 30,
    window_secs: 1800,
    counts,
  };
}

/**
 * Approved proposals: a memory_op with shadow improvements (counts down)
 * and a skill_update where shadow held flat. Both populate
 * `baseline_metrics_json` + `shadow_metrics` so the `MetricsDelta`
 * compact bar chart has data to render.
 */
export const MOCK_EVOLUTION_APPROVED: EvolutionProposal[] = [
  {
    id: "evo_01HZBA",
    kind: "memory_op",
    target: "merge_chunks:8821,8822",
    diff: "",
    reasoning:
      "两条 chunk 内容 0.94 余弦相似，合并后 search.recall.dropped 影子下降 33%。",
    risk: "low",
    status: "approved",
    signal_ids: [4205, 4211],
    trace_ids: ["t-2b03aa"],
    created_at: NOW - 1000 * 60 * 60 * 6,
    decided_at: NOW - 1000 * 60 * 25,
    decided_by: "operator",
    eval_run_id: "eval-2026-04-25-001",
    baseline_metrics_json: snapshot("merge_chunks:8821,8822", {
      "tool.call.failed": 4,
      "search.recall.dropped": 6,
    }),
    shadow_metrics: {
      "tool.call.failed": 3,
      "search.recall.dropped": 4,
      success_rate: 0.93,
    },
  },
  {
    id: "evo_01HZBB",
    kind: "skill_update",
    target: "skills/diary_writer/prompt.md",
    diff: [
      "--- a/skills/diary_writer/prompt.md",
      "+++ b/skills/diary_writer/prompt.md",
      "@@ -8,4 +8,5 @@",
      " - 用第一人称",
      "-- 不要列出当天所有事件",
      "+- 优先记述对你触动最深的一件事，其余略写",
    ].join("\n"),
    reasoning:
      "情感密度提升 +0.18, 长度 -22%; 影子评估在 prompt.eval.failed 维持持平。",
    risk: "medium",
    status: "approved",
    signal_ids: [4188, 4201, 4233],
    trace_ids: ["t-2b04ee"],
    created_at: NOW - 1000 * 60 * 60 * 4,
    decided_at: NOW - 1000 * 60 * 12,
    decided_by: "operator",
    eval_run_id: "eval-2026-04-25-002",
    baseline_metrics_json: snapshot("skills/diary_writer/prompt.md", {
      "prompt.eval.failed": 5,
      "tool.call.failed": 2,
    }),
    shadow_metrics: {
      "prompt.eval.failed": 5,
      "tool.call.failed": 2,
      success_rate: 0.88,
    },
  },
];

/**
 * History entries: one applied (clean), one auto-rolled-back (counts
 * regressed past threshold), one manually rolled back (operator notes).
 * Mirrors what `GET /admin/evolution/history` will serve once the
 * gateway endpoint lands.
 */
export const MOCK_EVOLUTION_HISTORY: HistoryEntry[] = [
  {
    proposal_id: "evo_01HZAA",
    kind: "memory_op",
    target: "merge_chunks:7710,7711",
    risk: "low",
    status: "applied",
    applied_at: NOW - 1000 * 60 * 60 * 2,
    rolled_back_at: null,
    rollback_reason: null,
    auto_rollback_reason: null,
    metrics_baseline: snapshot("merge_chunks:7710,7711", {
      "tool.call.failed": 2,
      "search.recall.dropped": 3,
    }),
    shadow_metrics: {
      "tool.call.failed": 2,
      "search.recall.dropped": 2,
    },
    baseline_metrics_json: snapshot("merge_chunks:7710,7711", {
      "tool.call.failed": 2,
      "search.recall.dropped": 3,
    }),
    before_sha: "a1b2c3d4e5f6",
    after_sha: "f6e5d4c3b2a1",
    eval_run_id: "eval-2026-04-25-101",
    reasoning: "两条 chunk 接近重复，合并后召回更稳。",
  },
  {
    proposal_id: "evo_01HZ99",
    kind: "memory_op",
    target: "delete_chunk:5530",
    risk: "medium",
    status: "rolled_back",
    applied_at: NOW - 1000 * 60 * 60 * 9,
    rolled_back_at: NOW - 1000 * 60 * 60 * 7,
    rollback_reason: null,
    auto_rollback_reason:
      "err_signal_count: 4 -> 14 (+250%) breaches threshold +50%",
    metrics_baseline: snapshot("delete_chunk:5530", {
      "tool.call.failed": 4,
      "search.recall.dropped": 0,
    }),
    shadow_metrics: {
      "tool.call.failed": 14,
      "search.recall.dropped": 2,
    },
    baseline_metrics_json: snapshot("delete_chunk:5530", {
      "tool.call.failed": 4,
      "search.recall.dropped": 0,
    }),
    before_sha: "01a1b1c1d1e1",
    after_sha: "f1e1d1c1b1a1",
    eval_run_id: "eval-2026-04-25-099",
    reasoning:
      "删除候选 chunk; 灰度期内监测到 tool.call.failed 显著上升, 自动回滚。",
  },
  {
    proposal_id: "evo_01HZ88",
    kind: "skill_update",
    target: "skills/topic_tagger/rules.toml",
    risk: "high",
    status: "rolled_back",
    applied_at: NOW - 1000 * 60 * 60 * 24,
    rolled_back_at: NOW - 1000 * 60 * 60 * 20,
    rollback_reason: "operator: tagging hallucinated novel categories",
    auto_rollback_reason: null,
    metrics_baseline: snapshot("skills/topic_tagger/rules.toml", {
      "prompt.eval.failed": 3,
    }),
    shadow_metrics: null,
    baseline_metrics_json: null,
    before_sha: "9988aa776655",
    after_sha: "5566778899aa",
    eval_run_id: null,
    reasoning:
      "更激进的标签合并规则; 上线后用户反馈出现凭空合成的类别，运维手动回滚。",
  },
];
