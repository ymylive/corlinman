"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { RefreshCcw } from "lucide-react";

import { GlassPanel } from "@/components/ui/glass-panel";
import { cn } from "@/lib/utils";
import { BudgetGauge } from "./BudgetGauge";

/**
 * Evolution page hero — Tidepool's quiet-prose pattern, retuned with a
 * serif display title (Hermès' hand-set catalogue feel) and a single
 * StreamPill on the right that doubles as the budget readout.
 *
 * Two states:
 *   - Pending rows → "{{n}} proposals await your reading. Oldest waited
 *     {{s}}s." — amber inline metric, restrained.
 *   - Empty       → "Nothing in the queue. The agent is still listening…"
 *
 * The refresh CTA is a flat ghost button; the visual weight stays on the
 * title itself so the page reads like a printed catalogue.
 */
export interface EvolutionPageHeaderProps {
  pendingCount: number;
  oldestHeldMs: number | null;
  /** Total weekly budget, e.g. 12. */
  budgetTotal: number;
  /** How many proposals have been auto-approved against budget this week. */
  budgetUsed: number;
  /** Whether the upstream feed is live. */
  watching: boolean;
  onRefresh?: () => void;
  refreshing?: boolean;
}

export function EvolutionPageHeader({
  pendingCount,
  oldestHeldMs,
  budgetTotal,
  budgetUsed,
  watching,
  onRefresh,
  refreshing = false,
}: EvolutionPageHeaderProps) {
  const { t } = useTranslation();
  const hasPending = pendingCount > 0;

  return (
    <GlassPanel
      as="header"
      variant="strong"
      className="flex flex-col gap-5 p-5 md:flex-row md:items-end md:justify-between md:gap-8 md:p-7"
    >
      <div className="flex max-w-[58ch] flex-col gap-3">
        <div className="flex items-center gap-2.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-tp-ink-4">
          <span
            aria-hidden
            className="h-1.5 w-1.5 rounded-full bg-tp-amber tp-breathe"
          />
          corlinman · evolution
        </div>
        <h1
          className={cn(
            "font-serif text-[40px] font-normal leading-[1.04] tracking-[-0.02em] text-tp-ink",
            "sm:text-[46px]",
          )}
        >
          {t("evolution.tp.title")}
        </h1>
        <p className="text-[13.5px] leading-[1.65] text-tp-ink-2">
          {t("evolution.tp.subtitle")}
        </p>
        <p className="mt-1 text-[13px] leading-[1.6] text-tp-ink-3">
          {hasPending ? (
            <>
              <InlineMetric tone="warn">
                {t("evolution.tp.heroLead", { n: pendingCount })}
              </InlineMetric>
              {oldestHeldMs !== null ? (
                <span className="ml-1.5">
                  {t("evolution.tp.heroLeadOldest", {
                    s: Math.max(1, Math.floor(oldestHeldMs / 1000)),
                  })}
                </span>
              ) : null}
            </>
          ) : (
            <>
              <span className="text-tp-ink">{t("evolution.tp.heroQuiet")}</span>
              <span className="ml-1.5">{t("evolution.tp.heroQuietSub")}</span>
            </>
          )}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2.5">
        <BudgetPill used={budgetUsed} total={budgetTotal} />
        <WatchPill watching={watching} />
        {onRefresh ? (
          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            aria-label={t("evolution.tp.refresh")}
            className={cn(
              "inline-flex h-8 items-center gap-1.5 rounded-full border px-3.5 text-[12px] font-medium",
              "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
              "transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
              "disabled:pointer-events-none disabled:opacity-60",
            )}
          >
            <RefreshCcw
              aria-hidden
              className={cn("h-3.5 w-3.5", refreshing && "animate-spin")}
            />
            {t("evolution.tp.refresh")}
          </button>
        ) : null}
      </div>
    </GlassPanel>
  );
}

function InlineMetric({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "neutral" | "warn";
}) {
  return (
    <span
      className={cn(
        "whitespace-nowrap rounded-md border px-1.5 py-px font-mono text-[12px] font-medium tabular-nums",
        tone === "warn"
          ? "border-tp-warn/30 bg-tp-warn-soft text-tp-warn"
          : "border-tp-glass-edge bg-tp-glass-inner-strong text-tp-ink",
      )}
    >
      {children}
    </span>
  );
}

function BudgetPill({ used, total }: { used: number; total: number }) {
  const { t } = useTranslation();
  const pct = total === 0 ? 0 : Math.round((used / total) * 100);
  return (
    <span
      className={cn(
        "inline-flex h-8 items-center gap-2 rounded-full border px-3 font-mono text-[11px]",
        "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
      )}
    >
      <BudgetGauge
        used={used}
        total={total}
        label={t("evolution.tp.budgetGaugeLabel")}
      />
      <span className="text-tp-ink-3">
        {t("evolution.tp.statBudget").toLowerCase()} ·
      </span>
      <span className="tabular-nums text-tp-ink">
        {used}/{total}
      </span>
      <span className="text-tp-ink-4">({pct}%)</span>
    </span>
  );
}

function WatchPill({ watching }: { watching: boolean }) {
  const { t } = useTranslation();
  return (
    <span
      role="status"
      aria-live="polite"
      className={cn(
        "inline-flex h-8 items-center gap-2 rounded-full border px-3 font-mono text-[11px]",
        watching
          ? "border-tp-ok/25 bg-tp-ok-soft text-tp-ok"
          : "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-3",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-[7px] w-[7px] rounded-full",
          watching ? "bg-tp-ok tp-breathe" : "bg-tp-ink-4",
        )}
      />
      {watching
        ? t("evolution.tp.streamWatching")
        : t("evolution.tp.streamPaused")}
    </span>
  );
}
