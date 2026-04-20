import { TodoCard } from "@/components/todo-card";

export default function ApprovalsPage() {
  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">工具审批</h1>
        <p className="text-sm text-muted-foreground">
          `toolApprovalConfig.json` 匹配到 prompt 模式的待审批工具调用队列。
          对应 corlinman-gateway::middleware::approval（plan §7 #6）。
        </p>
      </header>
      <TodoCard
        title="待审批队列"
        milestone="M3"
        description="SQLite pending_approvals 表实时流，通过/拒绝后回传 ApprovalDecision 帧。"
        todos={[
          "SSE /admin/approvals → 新待审批",
          "POST /admin/approvals/:id/approve | /deny",
          "GET /admin/approvals/config → 当前规则",
          "PUT /admin/approvals/config → 修改规则（热更新）",
          "TODO: 内嵌 TraceIdBadge，点击跳 /logs?trace=…",
        ]}
      />
    </>
  );
}
