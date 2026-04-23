"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * 90-day availability card. Big serif number, italic subtext, and a 30-bar
 * histogram of day-level availability.
 *
 * Designed for the Dashboard's System-Health pane (right column) and may
 * reappear on Settings / Observability pages.
 *
 * Render without a wrapper; caller supplies the surrounding `<GlassPanel>`.
 *
 * Accessibility: the histogram is `aria-hidden`; the canonical data is the
 * big number + italic summary which read fine as plain prose.
 */

export type DayBar = {
  /** Percent availability (0–100). Rendered as bar height. */
  height: number;
  /** `warn` / `err` bars are coloured to signal degraded days. */
  tone?: "ok" | "warn" | "err";
};

export interface UptimeStreakProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Formatted availability (e.g. "99.94"). No `%` — we render the unit. */
  pct: string;
  /** Bars, typically 30. Most recent last. */
  bars: DayBar[];
  /** Incidents summary ("3 incidents"), plain text. */
  incidentsText?: string;
  /** Override label at the top. Default "90-day availability". */
  label?: string;
}

const toneBg: Record<NonNullable<DayBar["tone"]>, string> = {
  ok: "bg-[color-mix(in_oklch,var(--tp-ok)_60%,transparent)]",
  warn: "bg-[color-mix(in_oklch,var(--tp-warn)_60%,transparent)]",
  err: "bg-[color-mix(in_oklch,var(--tp-err)_60%,transparent)]",
};

export const UptimeStreak = React.forwardRef<HTMLDivElement, UptimeStreakProps>(
  function UptimeStreak(
    { pct, bars, incidentsText, label = "90-day availability", className, ...rest },
    ref,
  ) {
    return (
      <div
        ref={ref}
        className={cn(
          "relative flex flex-col gap-2.5 overflow-hidden rounded-xl border p-4",
          "bg-tp-glass-inner border-tp-glass-edge",
          className,
        )}
        {...rest}
      >
        {/* soft amber glow behind the big number — opacity 0.6 */}
        <div
          aria-hidden
          className="pointer-events-none absolute -bottom-10 -right-8 h-32 w-44 rounded-full opacity-60 blur-3xl"
          style={{
            background:
              "radial-gradient(closest-side, var(--tp-amber-glow), transparent 70%)",
          }}
        />

        <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-tp-ink-4">
          {label}
        </div>

        <div className="relative flex items-baseline gap-2">
          <span className="font-sans text-[30px] font-medium leading-none tracking-[-0.03em] tabular-nums text-tp-ink">
            {pct}
            <span className="ml-0.5 text-[15px] font-normal text-tp-ink-3">%</span>
          </span>
          {incidentsText ? (
            <span className="ml-auto font-serif italic text-[14px] text-tp-ink-2">
              <span className="not-italic font-medium text-tp-amber">{incidentsText}</span>
            </span>
          ) : null}
        </div>

        <div
          aria-hidden
          className="relative flex h-[22px] items-end gap-[2px]"
        >
          {bars.map((b, i) => (
            <span
              key={i}
              className={cn(
                "min-w-[4px] flex-1 rounded-[1px]",
                toneBg[b.tone ?? "ok"],
              )}
              style={{ height: `${Math.max(0, Math.min(100, b.height))}%` }}
            />
          ))}
        </div>
      </div>
    );
  },
);

export default UptimeStreak;
