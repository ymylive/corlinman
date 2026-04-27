"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/**
 * Visualizes baseline-vs-current event_kind counts as a stack of small
 * horizontal bars. One row per event_kind in the union of both maps.
 *
 * The neutral bar (baseline) sits behind the warn/ok bar (current); the
 * comparison bar is colored:
 *   - warn tone when current > baseline (regression)
 *   - ok   tone when current ≤ baseline (improvement / flat)
 *
 * Compact variant: only renders the top-3 rows by absolute delta — used
 * inline on Approved / History cards. Full variant renders all rows.
 *
 * No charting libs by design — pure flex + Tailwind width via inline
 * style. Keeps the tree-shaken bundle small and matches the rest of the
 * admin shell's "no D3" aesthetic.
 */
export interface MetricsDeltaProps {
  baseline: Record<string, number>;
  current: Record<string, number>;
  /** Optional label rendered above the rows (e.g. "Shadow vs baseline"). */
  label?: string;
  variant?: "compact" | "full";
}

interface Row {
  key: string;
  baseline: number;
  current: number;
  /** current - baseline */
  abs: number;
  /** percentage delta vs baseline (denominator floored at 1). */
  pct: number;
}

function computeRows(
  baseline: Record<string, number>,
  current: Record<string, number>,
): Row[] {
  const keys = new Set<string>([
    ...Object.keys(baseline),
    ...Object.keys(current),
  ]);
  const rows: Row[] = [];
  for (const key of keys) {
    const b = baseline[key] ?? 0;
    const c = current[key] ?? 0;
    const denom = Math.max(b, 1);
    rows.push({
      key,
      baseline: b,
      current: c,
      abs: c - b,
      pct: ((c - b) / denom) * 100,
    });
  }
  // Sort by absolute delta (descending) so the biggest movers surface
  // first — same intent the operator has when scanning the card.
  rows.sort((a, b) => Math.abs(b.abs) - Math.abs(a.abs));
  return rows;
}

export function MetricsDelta({
  baseline,
  current,
  label,
  variant = "full",
}: MetricsDeltaProps) {
  const { t } = useTranslation();
  const allRows = React.useMemo(
    () => computeRows(baseline, current),
    [baseline, current],
  );
  const rows = variant === "compact" ? allRows.slice(0, 3) : allRows;

  if (rows.length === 0) {
    return null;
  }

  // Width of each bar is normalized against the largest count in the set
  // so two charts with mismatched scales still read at a glance.
  const max = rows.reduce(
    (acc, r) => Math.max(acc, r.baseline, r.current),
    1,
  );

  const heading =
    label ?? (t("evolution.tp.metricsDeltaHeader") as string);

  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 rounded-xl border border-tp-glass-edge",
        "bg-tp-glass-inner/40 p-2.5",
      )}
      role="group"
      aria-label={heading}
    >
      <div className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
        {heading}
      </div>
      <ul className="flex flex-col gap-1.5">
        {rows.map((row) => (
          <DeltaRow key={row.key} row={row} max={max} />
        ))}
      </ul>
    </div>
  );
}

function DeltaRow({ row, max }: { row: Row; max: number }) {
  const baselinePct = (row.baseline / max) * 100;
  const currentPct = (row.current / max) * 100;
  const regressed = row.current > row.baseline;
  // No change → render as neutral so the eye doesn't read "regression"
  // when nothing actually moved.
  const sameOrBetter = row.current <= row.baseline;

  const sign = row.abs > 0 ? "+" : row.abs < 0 ? "" : "±";
  const annotation = `${sign}${row.abs} (${sign}${row.pct.toFixed(0)}%)`;

  return (
    <li className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2 text-[11px] text-tp-ink-2">
        <span className="truncate font-mono text-tp-ink-3">{row.key}</span>
        <span
          className={cn(
            "font-mono tabular-nums",
            row.abs === 0
              ? "text-tp-ink-4"
              : regressed
                ? "text-tp-warn"
                : "text-tp-ok",
          )}
          aria-label={`baseline ${row.baseline}, current ${row.current}, delta ${row.abs}`}
        >
          {row.baseline} → {row.current} · {annotation}
        </span>
      </div>
      <div className="relative h-2 w-full overflow-hidden rounded-full bg-tp-glass-inner">
        {/* Baseline (neutral, behind). */}
        <span
          aria-hidden
          className="absolute inset-y-0 left-0 rounded-full bg-tp-ink-3/30"
          style={{ width: `${baselinePct}%` }}
        />
        {/* Current — drawn over baseline. Warn tone if regressed, ok if not. */}
        <span
          aria-hidden
          className={cn(
            "absolute inset-y-0 left-0 rounded-full",
            regressed
              ? "bg-tp-warn/70"
              : sameOrBetter
                ? "bg-tp-ok/70"
                : "bg-tp-ink-3/40",
          )}
          style={{ width: `${currentPct}%` }}
        />
      </div>
    </li>
  );
}

export default MetricsDelta;
