import { TodoCard } from "@/components/todo-card";

export default function RagPage() {
  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">RAG 调参</h1>
        <p className="text-sm text-muted-foreground">
          `rag_params.json` 可视化表单 + usearch 索引状态 + 重建按钮。
          算法实现见 corlinman-vector（TagMemo / ResidualPyramid，plan §2/§14）。
        </p>
      </header>
      <TodoCard
        title="向量库状态"
        milestone="M4"
        description="展示每个 Knowledge Base 的 .usearch 大小 / 条目 / 最近重建。"
        todos={[
          "GET /admin/rag/indexes → 每库状态",
          "GET /admin/rag/params → rag_params.json (zod schema 校验)",
          "PUT /admin/rag/params → Arc<ArcSwap<RagParams>> 原子切换",
          "POST /admin/rag/indexes/:id/rebuild → 进度 SSE",
          "TODO: TagMemo wave 波形图（M4 后接入）",
        ]}
      />
    </>
  );
}
