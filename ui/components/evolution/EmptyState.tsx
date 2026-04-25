"use client";

import { Sprout, Hourglass } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { Tab } from "./types";

/**
 * Empty state for the /evolution proposal queue.
 *
 *  - `pending`  — the agent is observing; nothing has crossed the threshold.
 *  - `approved` / `history` — Phase-3 placeholder copy. The tabs stay
 *    visible structurally, but nothing renders here until Wave 1-E.
 */
export function EvolutionEmptyState({ tab }: { tab: Tab }) {
  const { t } = useTranslation();

  if (tab === "pending") {
    return (
      <div
        className={cn(
          "flex flex-col items-center justify-center gap-3 py-14 text-center",
          "rounded-2xl border border-dashed border-tp-glass-edge bg-tp-glass-inner/40",
        )}
      >
        <Sprout className="h-9 w-9 text-tp-amber/80" aria-hidden />
        <p className="font-serif text-[20px] font-normal leading-tight tracking-[-0.01em] text-tp-ink">
          {t("evolution.tp.emptyPending")}
        </p>
        <p className="max-w-[42ch] text-[12.5px] leading-[1.6] text-tp-ink-3">
          {t("evolution.tp.emptyPendingHint")}
        </p>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 py-14 text-center",
        "rounded-2xl border border-dashed border-tp-glass-edge bg-tp-glass-inner/40",
      )}
    >
      <Hourglass className="h-8 w-8 text-tp-ink-4" aria-hidden />
      <p className="font-serif text-[18px] font-normal leading-tight tracking-[-0.01em] text-tp-ink">
        {t("evolution.tp.tabPlaceholderTitle")}
      </p>
      <p className="max-w-[42ch] text-[12.5px] leading-[1.6] text-tp-ink-3">
        {t("evolution.tp.tabPlaceholderHint")}
      </p>
    </div>
  );
}

export default EvolutionEmptyState;
