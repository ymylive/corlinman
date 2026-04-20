import { TodoCard } from "@/components/todo-card";

export default function ConfigPage() {
  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">配置</h1>
        <p className="text-sm text-muted-foreground">
          `config.toml` 分组编辑（[admin] / [rag] / [providers.*]，见 plan §17）。
          保存后通过 ArcSwap 原子热更新。
        </p>
      </header>
      <TodoCard
        title="config.toml"
        milestone="M1"
        description="读取 config.toml；分组 ([admin] / [rag] / [providers.*] / [channels] / [plugins])。"
        todos={[
          "GET /admin/config → ValidationReport + grouped keys",
          "PUT /admin/config → zod 客户端预校验 + 服务端 schemars 终裁",
          "GET /admin/config/schema → 渲染描述 + default",
          "UI：[admin] / [rag] / [providers.*] / [channels] / [plugins] 分组",
          "TODO: diff 视图（改前 vs 改后），便于回滚判断",
        ]}
      />
    </>
  );
}
