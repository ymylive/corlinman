"use client";

import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";

/** Batch-action toolbar that lights up when the operator has selection. */
export interface BatchToolbarProps {
  selectedCount: number;
  onApproveAll: () => void;
  onDenyAll: () => void;
  onClear: () => void;
  disabled?: boolean;
}

export function BatchToolbar({
  selectedCount,
  onApproveAll,
  onDenyAll,
  onClear,
  disabled = false,
}: BatchToolbarProps) {
  const { t } = useTranslation();
  if (selectedCount === 0) return null;
  return (
    <div
      role="region"
      aria-label={t("approvals.batchActionsAria")}
      className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm"
    >
      <span className="font-medium">
        {t("approvals.selectedCount", { n: selectedCount })}
      </span>
      <div className="flex-1" />
      <Button size="sm" onClick={onApproveAll} disabled={disabled}>
        {t("approvals.batchApprove")}
      </Button>
      <Button
        size="sm"
        variant="destructive"
        onClick={onDenyAll}
        disabled={disabled}
      >
        {t("approvals.batchDeny")}
      </Button>
      <Button size="sm" variant="ghost" onClick={onClear} disabled={disabled}>
        {t("approvals.clear")}
      </Button>
    </div>
  );
}
