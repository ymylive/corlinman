import { TodoCard } from "@/components/todo-card";

export default function SchedulerPage() {
  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">定时任务</h1>
        <p className="text-sm text-muted-foreground">
          cron 列表、下次触发时间、手动触发。对应 corlinman-scheduler（plan §5.4）。
        </p>
      </header>
      <TodoCard
        title="Job 列表"
        milestone="M7"
        description="tokio-cron-scheduler 注册的任务 + 历史执行记录。"
        todos={[
          "GET /admin/scheduler/jobs → cron / next_fire / last_status",
          "POST /admin/scheduler/jobs/:id/trigger → 手动触发",
          "SSE /admin/scheduler/events → 触发/完成事件流",
          "TODO: 支持新增/编辑 cron（M7 JobContext 暴露后）",
        ]}
      />
    </>
  );
}
