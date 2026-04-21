"use client";

import { Inbox, History } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { Tab } from "./types";

/** Friendly empty state for the approvals table. */
export function ApprovalsEmptyState({ tab }: { tab: Tab }) {
  const { t } = useTranslation();
  if (tab === "pending") {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-10">
        <Inbox className="h-8 w-8 text-muted-foreground" aria-hidden />
        <p className="text-sm font-medium">
          {t("approvals.emptyPendingTitle")}
        </p>
        <p className="text-xs text-muted-foreground">
          {t("approvals.emptyPendingHint")}
        </p>
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10">
      <History className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium">{t("approvals.emptyHistoryTitle")}</p>
      <p className="text-xs text-muted-foreground">
        {t("approvals.emptyHistoryHint")}
      </p>
    </div>
  );
}
