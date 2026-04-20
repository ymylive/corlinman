import { Inbox, History } from "lucide-react";
import type { Tab } from "./types";

/** Friendly empty state for the approvals table.
 *
 * Uses a lucide icon (already in deps) instead of an inline SVG — keeps the
 * component tiny and matches the rest of the admin UI's visual language.
 */
export function ApprovalsEmptyState({ tab }: { tab: Tab }) {
  if (tab === "pending") {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-10">
        <Inbox className="h-8 w-8 text-muted-foreground" aria-hidden />
        <p className="text-sm font-medium">当前无待审批工具调用</p>
        <p className="text-xs text-muted-foreground">
          新请求会通过 SSE 自动推送到此列表。
        </p>
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10">
      <History className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium">历史记录为空</p>
      <p className="text-xs text-muted-foreground">
        审批完成后将出现在此。默认仅展示最近 200 条。
      </p>
    </div>
  );
}
