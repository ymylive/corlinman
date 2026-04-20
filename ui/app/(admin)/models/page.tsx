import { TodoCard } from "@/components/todo-card";

export default function ModelsPage() {
  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">模型路由</h1>
        <p className="text-sm text-muted-foreground">
          `ModelRedirect.json` 图形化 —— 客户端请求 model → 供应商映射。
          见 plan §12（M1 验证点：5 redirect case）。
        </p>
      </header>
      <TodoCard
        title="ModelRedirect.json"
        milestone="M1"
        description="列表展示每条 redirect rule + 支持 provider failover 预览。"
        todos={[
          "GET /admin/models/redirects → 当前规则",
          "PUT /admin/models/redirects → 新规则（ArcSwap）",
          "GET /admin/models/providers → 已加载 provider + failover 状态",
          "POST /admin/models/test → dry-run 给定 model / message → 路由结果",
          "TODO: 把 FailoverReason 分类（plan §8 A1）用 Badge 可视化",
        ]}
      />
    </>
  );
}
