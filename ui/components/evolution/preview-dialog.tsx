"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ArrowRight } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { CuratorReport, CuratorTransition } from "@/lib/api";

/**
 * Dry-run preview dialog. Receives a :class:`CuratorReport` and renders
 * it as a diff-style list of transitions. The "Apply now" CTA fires the
 * real run (the parent page handles the actual mutation + cache
 * invalidation); the dialog only flips closed when the parent passes a
 * fresh `report=null` value via `open` toggling.
 */
export interface PreviewDialogProps {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  report: CuratorReport | null;
  loading?: boolean;
  /** Called when the operator confirms "Apply now". Optional so the
   * dialog can also be opened in read-only inspection mode (e.g. from
   * the run-history surface in a future wave). */
  onApply?: () => void;
  applyDisabled?: boolean;
}

export function PreviewDialog({
  open,
  onOpenChange,
  report,
  loading = false,
  onApply,
  applyDisabled = false,
}: PreviewDialogProps) {
  const { t } = useTranslation();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("evolution.preview.title")}</DialogTitle>
          {report && !loading ? (
            <DialogDescription>
              {t("evolution.preview.summary", {
                checked: report.checked,
                stale: report.marked_stale,
                archived: report.archived,
                reactivated: report.reactivated,
              })}
            </DialogDescription>
          ) : null}
        </DialogHeader>

        <div
          data-testid="preview-body"
          className="max-h-[400px] overflow-y-auto"
        >
          {loading ? (
            <div className="py-6 text-center text-sm text-tp-ink-3">
              {t("evolution.preview.loading")}
            </div>
          ) : !report || report.transitions.length === 0 ? (
            <div className="py-6 text-center text-sm text-tp-ink-3">
              {t("evolution.preview.empty")}
            </div>
          ) : (
            <ul className="flex flex-col gap-2">
              {report.transitions.map((tr) => (
                <TransitionRow
                  key={`${tr.skill_name}:${tr.from_state}:${tr.to_state}`}
                  transition={tr}
                />
              ))}
            </ul>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
          >
            {t("evolution.preview.cancel")}
          </Button>
          {onApply ? (
            <Button
              type="button"
              onClick={onApply}
              disabled={
                applyDisabled || !report || report.transitions.length === 0
              }
            >
              {t("evolution.preview.applyNow")}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TransitionRow({ transition }: { transition: CuratorTransition }) {
  const { t } = useTranslation();
  const days = Math.round(transition.days_idle * 10) / 10;
  const reasonLabel = reasonFor(transition.reason, days, t);
  return (
    <li
      data-testid={`transition-${transition.skill_name}`}
      className={cn(
        "flex items-center justify-between gap-3 rounded-lg border border-tp-glass-edge",
        "bg-tp-glass-inner/50 px-3 py-2 text-[13px]",
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="font-mono font-semibold text-tp-ink-1 truncate">
          {transition.skill_name}
        </span>
        <span className="font-mono text-tp-ink-3">
          {transition.from_state}
        </span>
        <ArrowRight aria-hidden className="h-3.5 w-3.5 text-tp-ink-3" />
        <span className="font-mono font-semibold text-tp-ink-1">
          {transition.to_state}
        </span>
      </div>
      <span className="shrink-0 text-[11px] text-tp-ink-3">{reasonLabel}</span>
    </li>
  );
}

function reasonFor(
  reason: string,
  days: number,
  t: (k: string, opts?: Record<string, unknown>) => string,
): string {
  switch (reason) {
    case "stale_threshold":
      return t("evolution.transitions.staleThreshold", { days });
    case "archive_threshold":
      return t("evolution.transitions.archiveThreshold", { days });
    case "reactivated":
      return t("evolution.transitions.reactivated");
    default:
      return t("evolution.transitions.daysIdle", { days });
  }
}
