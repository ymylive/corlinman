"use client";

import * as React from "react";
import { motion } from "framer-motion";

import { cn } from "@/lib/utils";
import type { Runner } from "@/lib/mocks/nodes";

/**
 * Tidepool-retokened satellite node.
 *
 * Palette:
 *   - healthy  → --tp-ok (soft fill at 12%, stroke at 80%)
 *   - degraded → --tp-warn (adds `!` glyph + subtle jitter animation)
 *   - offline  → --tp-ink-4 (desaturated, `∅` glyph, no pulse)
 *
 * Motion:
 *   - healthy gets a pulsing halo (class `.nodes-halo`)
 *   - degraded gets a gentle horizontal shake (class `.nodes-shake`)
 *   - `reduced` collapses both to static
 *
 * Selection adds a 3px stroke + a concentric outer ring.
 */

export interface RunnerNodeProps {
  runner: Runner;
  /** Pre-computed x coordinate in SVG user units. */
  cx: number;
  /** Pre-computed y coordinate in SVG user units. */
  cy: number;
  /** Circle radius (inner ring runners are slightly larger). */
  r: number;
  selected: boolean;
  /** User has enabled `prefers-reduced-motion`. */
  reduced: boolean;
  /** Capability filter excluded this runner — desaturate to 35% opacity. */
  dim?: boolean;
  onSelect: (runner: Runner) => void;
}

const HEALTH_STROKE: Record<Runner["health"], string> = {
  healthy: "var(--tp-ok)",
  degraded: "var(--tp-warn)",
  offline: "var(--tp-ink-4)",
};

const HEALTH_FILL: Record<Runner["health"], string> = {
  healthy: "color-mix(in oklch, var(--tp-ok) 16%, transparent)",
  degraded: "color-mix(in oklch, var(--tp-warn) 16%, transparent)",
  offline: "color-mix(in oklch, var(--tp-ink-4) 18%, transparent)",
};

function truncateId(id: string, max = 10): string {
  if (id.length <= max) return id;
  return `${id.slice(0, max)}…`;
}

export const RunnerNode = React.memo(function RunnerNode({
  runner,
  cx,
  cy,
  r,
  selected,
  reduced,
  dim = false,
  onSelect,
}: RunnerNodeProps) {
  const baseOpacity = runner.health === "offline" ? 0.55 : 1;
  const opacity = dim ? baseOpacity * 0.35 : baseOpacity;
  const showPulse = runner.health === "healthy" && !reduced && !dim;
  const showShake = runner.health === "degraded" && !reduced && !dim;

  const onKeyDown = (e: React.KeyboardEvent<SVGGElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(runner);
    }
  };

  const ariaLabel = [
    `Runner ${runner.hostname}`,
    `health ${runner.health}`,
    runner.health === "offline"
      ? `offline for ${Math.round(runner.lastPingMs / 1000)}s`
      : `latency ${runner.latencyMs}ms`,
    `${runner.toolCount} tools`,
  ].join(", ");

  return (
    <motion.g
      layout
      layoutId={`runner-${runner.id}`}
      transition={
        reduced
          ? { duration: 0 }
          : { type: "spring", stiffness: 320, damping: 28 }
      }
      tabIndex={0}
      role="button"
      aria-label={ariaLabel}
      aria-pressed={selected}
      data-testid={`runner-node-${runner.id}`}
      data-selected={selected ? "true" : "false"}
      data-health={runner.health}
      className={cn(
        "cursor-pointer outline-none focus-visible:[&>circle]:stroke-[3px]",
        showShake && "nodes-shake",
      )}
      style={{ opacity }}
      onClick={(e) => {
        e.stopPropagation();
        onSelect(runner);
      }}
      onKeyDown={onKeyDown}
    >
      {/* Outer halo — pulses on healthy nodes. */}
      {showPulse ? (
        <circle
          aria-hidden="true"
          cx={cx}
          cy={cy}
          r={r + 7}
          fill="none"
          stroke={HEALTH_STROKE[runner.health]}
          strokeOpacity={0.4}
          strokeWidth={1}
          className="nodes-halo"
        />
      ) : null}
      {/* Selection ring — a quiet amber outline when selected. */}
      {selected ? (
        <circle
          aria-hidden="true"
          cx={cx}
          cy={cy}
          r={r + 4}
          fill="none"
          stroke="var(--tp-amber)"
          strokeOpacity={0.9}
          strokeWidth={1.4}
        />
      ) : null}
      {/* Body. */}
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill={HEALTH_FILL[runner.health]}
        stroke={HEALTH_STROKE[runner.health]}
        strokeWidth={selected ? 3 : 1.8}
      />
      {/* Tool-count badge (top-right). */}
      <g aria-hidden="true">
        <circle
          cx={cx + r * 0.72}
          cy={cy - r * 0.72}
          r={8}
          fill="var(--tp-glass-inner-strong)"
          stroke={HEALTH_STROKE[runner.health]}
          strokeOpacity={0.6}
          strokeWidth={1}
        />
        <text
          x={cx + r * 0.72}
          y={cy - r * 0.72}
          fontSize="9"
          fontFamily="var(--font-geist-mono, ui-monospace)"
          textAnchor="middle"
          dominantBaseline="central"
          fill="var(--tp-ink)"
        >
          {runner.toolCount}
        </text>
      </g>
      {/* Hostname label below the node. */}
      <text
        x={cx}
        y={cy + r + 15}
        fontSize="10.5"
        fontFamily="var(--font-geist-mono, ui-monospace)"
        textAnchor="middle"
        fill="var(--tp-ink-2)"
        aria-hidden="true"
      >
        {truncateId(runner.hostname.replace("runner-", ""))}
      </text>
      {/* Colour-blind safety glyph. */}
      {runner.health === "degraded" ? (
        <text
          aria-hidden="true"
          x={cx - r * 0.68}
          y={cy - r * 0.55}
          fontSize={11}
          fontWeight={700}
          textAnchor="middle"
          fill="var(--tp-warn)"
          data-testid={`runner-glyph-${runner.id}`}
        >
          !
        </text>
      ) : null}
      {runner.health === "offline" ? (
        <text
          aria-hidden="true"
          x={cx - r * 0.68}
          y={cy - r * 0.55}
          fontSize={11}
          fontWeight={700}
          textAnchor="middle"
          fill="var(--tp-ink-4)"
          data-testid={`runner-glyph-${runner.id}`}
        >
          ∅
        </text>
      ) : null}
    </motion.g>
  );
});

export default RunnerNode;
