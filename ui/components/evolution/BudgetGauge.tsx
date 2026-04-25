"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Tiny circular gauge — used in the hero pill row to show how much of the
 * weekly evolution budget the operator has already spent.
 *
 * Tone ramps with remaining capacity:
 *   - >= 75% remaining → tp-ok (green)
 *   - 25–74% remaining → tp-warn (amber)
 *   -  < 25% remaining → tp-err (red)
 *
 * Mathematically the ring fills *clockwise* with `used`, so the visible arc
 * grows as the budget is consumed. Colour is keyed off `remaining` so the
 * ring eases from green → amber → red as the budget is approached.
 */
export interface BudgetGaugeProps extends React.HTMLAttributes<HTMLDivElement> {
  used: number;
  total: number;
  size?: number;
  strokeWidth?: number;
  /** aria-label for the SVG (read by screen readers as a meter). */
  label?: string;
}

export const BudgetGauge = React.forwardRef<HTMLDivElement, BudgetGaugeProps>(
  function BudgetGauge(
    { used, total, size = 22, strokeWidth = 2.5, label, className, ...rest },
    ref,
  ) {
    const safeTotal = Math.max(1, total);
    const usedPct = Math.max(0, Math.min(1, used / safeTotal));
    const remainingPct = 1 - usedPct;

    const radius = (size - strokeWidth) / 2;
    const circumference = 2 * Math.PI * radius;
    const dashOffset = circumference * (1 - usedPct);

    const tone =
      remainingPct >= 0.75
        ? "tp-ok"
        : remainingPct >= 0.25
          ? "tp-warn"
          : "tp-err";

    return (
      <div
        ref={ref}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={Math.min(used, total)}
        aria-label={label}
        className={cn("inline-flex shrink-0 items-center", className)}
        {...rest}
      >
        <svg
          width={size}
          height={size}
          viewBox={`0 0 ${size} ${size}`}
          aria-hidden="true"
        >
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="var(--tp-glass-edge)"
            strokeWidth={strokeWidth}
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={`var(--${tone})`}
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
            style={{ transition: "stroke-dashoffset 300ms ease, stroke 300ms ease" }}
          />
        </svg>
      </div>
    );
  },
);

export default BudgetGauge;
