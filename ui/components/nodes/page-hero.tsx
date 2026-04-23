"use client";

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

/**
 * Nodes page header — quiet prose pattern (big title + one-sentence summary,
 * no glass panel container). Mirrors the Approvals/Dashboard heroes so the
 * admin surface reads as one voice.
 *
 * Copy variants:
 *   - All online: `"N nodes online across M capabilities."`
 *   - Degraded present: adds `" One node-b degraded 2m ago — file-io capability
 *     dropped."` in warn tone.
 *   - Offline present: adds offline count suffix in muted tone.
 *   - Empty roster: `"No runners registered. Start a WebSocket tool runner —
 *     it will appear here automatically."`
 */

export interface PageHeroProps {
  onlineCount: number;
  capabilityCount: number;
  degradedCount: number;
  offlineCount: number;
  total: number;
  /** Name of the most recently degraded runner, if any — surfaces in prose. */
  recentDegradedHost: string | null;
  recentDegradedAgoSec: number | null;
  recentDegradedCapability: string | null;
}

export function PageHero({
  onlineCount,
  capabilityCount,
  degradedCount,
  offlineCount,
  total,
  recentDegradedHost,
  recentDegradedAgoSec,
  recentDegradedCapability,
}: PageHeroProps) {
  const { t } = useTranslation();
  const empty = total === 0;
  const hasDegraded = degradedCount > 0;
  const hasOffline = offlineCount > 0;

  return (
    <header className="flex flex-col gap-3">
      <h1
        className={cn(
          "font-sans text-[30px] font-semibold leading-[1.12] tracking-[-0.025em] text-tp-ink",
          "sm:text-[34px]",
        )}
      >
        {t("nodes.tp.heroTitle")}
      </h1>
      <p className="max-w-[64ch] text-[14px] leading-[1.6] text-tp-ink-2">
        {empty ? (
          <>
            <span className="text-tp-ink">{t("nodes.tp.heroEmpty")}</span>
            <span className="ml-1 text-tp-ink-3">{t("nodes.tp.heroEmptyHint")}</span>
          </>
        ) : (
          <>
            <InlineMetric tone="neutral">
              {t("nodes.tp.heroLead", {
                n: onlineCount,
                caps: capabilityCount,
              })}
            </InlineMetric>
            {hasDegraded && recentDegradedHost !== null ? (
              <span className="ml-1 text-tp-ink-2">
                <InlineMetric tone="warn">
                  {t("nodes.tp.heroDegraded", {
                    host: recentDegradedHost,
                    s: recentDegradedAgoSec ?? 0,
                  })}
                </InlineMetric>
                {recentDegradedCapability ? (
                  <span className="ml-1 text-tp-ink-3">
                    {t("nodes.tp.heroDegradedCap", {
                      cap: recentDegradedCapability,
                    })}
                  </span>
                ) : null}
              </span>
            ) : null}
            {hasOffline && !hasDegraded ? (
              <span className="ml-1 text-tp-ink-3">
                {t("nodes.tp.heroOffline", { n: offlineCount })}
              </span>
            ) : null}
          </>
        )}
      </p>
    </header>
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
        "whitespace-nowrap rounded-md border px-1.5 py-px font-mono text-[12.5px] font-medium tabular-nums",
        tone === "warn"
          ? "border-tp-warn/30 bg-tp-warn-soft text-tp-warn"
          : "border-tp-glass-edge bg-tp-glass-inner-strong text-tp-ink",
      )}
    >
      {children}
    </span>
  );
}

export default PageHero;
