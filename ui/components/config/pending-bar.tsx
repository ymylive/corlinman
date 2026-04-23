"use client";

import { useTranslation } from "react-i18next";
import { RotateCcw } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";

/**
 * Sticky bottom action-bar — rendered only when there are unsaved edits.
 * Mirrors GitHub / Linear "you have unsaved changes" affordance.
 *
 * Floats at `bottom-4 z-30` with `pointer-events-none` on the wrapper so it
 * doesn't swallow clicks to page elements underneath; the actual panel
 * re-enables pointer events. Max-width matches the drawer column so the
 * bar lines up with the editor.
 */
export function PendingBar({
  pendingCount,
  visible,
  saving,
  onSave,
  onDiscard,
}: {
  pendingCount: number;
  visible: boolean;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
}) {
  const { t } = useTranslation();
  if (!visible) return null;
  const lead =
    pendingCount === 1
      ? t("config.tp.pendingBarLeadSingular")
      : t("config.tp.pendingBarLead", { n: pendingCount });
  const saveLabel = saving
    ? t("config.saving")
    : pendingCount === 1
      ? t("config.save")
      : `${t("config.save")} · ${pendingCount}`;

  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-4 z-30 flex justify-center px-4"
      role="region"
      aria-label={lead}
    >
      <GlassPanel
        variant="strong"
        className="pointer-events-auto flex w-full max-w-[720px] items-center gap-3 px-4 py-3"
      >
        <span
          className="inline-flex h-2 w-2 rounded-full bg-tp-amber tp-breathe-amber"
          aria-hidden
        />
        <span className="flex-1 text-[13px] text-tp-ink">{lead}</span>
        <button
          type="button"
          onClick={onDiscard}
          disabled={saving}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-1.5 text-[12px] font-medium text-tp-ink-2",
            "transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
            "disabled:cursor-not-allowed disabled:opacity-60",
          )}
        >
          <RotateCcw className="h-3.5 w-3.5" aria-hidden />
          {t("config.tp.discardChanges")}
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg border border-tp-amber/40 bg-tp-amber px-3.5 py-1.5 text-[12px] font-medium text-tp-glass-hl",
            "transition-all hover:brightness-[1.04]",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/60",
            "disabled:cursor-not-allowed disabled:opacity-70",
          )}
        >
          {saveLabel}
        </button>
      </GlassPanel>
    </div>
  );
}
