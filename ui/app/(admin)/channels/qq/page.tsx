import { TodoCard } from "@/components/todo-card";

export default function QqChannelPage() {
  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">QQ 通道</h1>
        <p className="text-sm text-muted-foreground">
          gocq / OneBot v11 心跳、QQ_GROUP_KEYWORDS 编辑、最近消息。
          对应 corlinman-channels::qq（plan §5.3）。
        </p>
      </header>
      <TodoCard
        title="连接状态"
        milestone="M5"
        description="反向 WS 状态 + QQ_GROUP_KEYWORDS / QQ_GROUP_AGENTS 编辑。"
        todos={[
          "GET /admin/channels/qq/status → ws / heartbeat / bot_uin",
          "GET /admin/channels/qq/keywords → 按群 keywords 配置",
          "PUT /admin/channels/qq/keywords → 热更新（不断线）",
          "SSE /admin/channels/qq/messages → 最近 50 条 preview",
          "TODO: CQ code preview + ChannelBinding → session_key 调试",
        ]}
      />
    </>
  );
}
