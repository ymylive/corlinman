/**
 * Residual pyramid — Tidepool (Phase 5e) retoken.
 *
 * Stacked horizontal bars, one row per chunk. Each row has
 * `pyramid_levels.length` segments; widths are proportional to
 * `explained_energy`, colours interpolate across a 3-stop warm ramp
 * (amber → ember → peach) keyed to the pyramid level index. Rows reveal
 * top-down in an 80ms stagger — disabled under `prefers-reduced-motion`
 * because stagger of 500 rows is a non-trivial amount of animation.
 *
 * Hover: the hovered chunk keeps full opacity; siblings dim to 0.3 (this
 * ratio is asserted by the page test — do not change without updating the
 * test).
 */
"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import {
  useTooltip,
  useTooltipInPortal,
  defaultStyles,
} from "@visx/tooltip";

import { cn } from "@/lib/utils";
import type {
  PyramidLevel,
  TagMemoChunk,
} from "@/lib/mocks/tagmemo";
import { useHoveredId } from "./use-hovered-id";

interface ResidualPyramidProps {
  chunks: TagMemoChunk[];
  /** Parent width — we cap at 900 regardless. */
  parentWidth: number;
  className?: string;
}

const ROW_HEIGHT = 12;
const ROW_GAP = 1;
const LABEL_COL = 52; // space for the chunk id on the left
const MAX_WIDTH = 900;

interface TipData {
  chunk: TagMemoChunk;
}

export function ResidualPyramid({
  chunks,
  parentWidth,
  className,
}: ResidualPyramidProps) {
  const reduced = useReducedMotion();
  const { hoveredId, setHoveredId } = useHoveredId();

  const width = Math.min(parentWidth, MAX_WIDTH);
  const rowWidth = Math.max(200, width - LABEL_COL - 12);
  const height = chunks.length * (ROW_HEIGHT + ROW_GAP);

  const {
    tooltipOpen,
    tooltipData,
    tooltipLeft,
    tooltipTop,
    showTooltip,
    hideTooltip,
  } = useTooltip<TipData>();
  const { containerRef, TooltipInPortal } = useTooltipInPortal({
    detectBounds: true,
    scroll: true,
  });

  // Under reduced motion we skip the per-row stagger entirely. Animating
  // opacity+x on 500 rows is cheap, but 500 delayed tweens queued up to
  // 40s of animation is a lot to ask of AT users.
  const staggerMs = reduced ? 0 : 80;

  // Deepest pyramid across all chunks — defines the gradient t range so the
  // warm ramp spans the same band regardless of per-chunk depth variance.
  const maxDepth = React.useMemo(() => {
    let m = 0;
    for (const c of chunks) {
      if (c.pyramid_levels.length > m) m = c.pyramid_levels.length;
    }
    return Math.max(1, m);
  }, [chunks]);

  return (
    <div
      ref={containerRef}
      className={cn(
        // subtle: no backdrop-filter; we're inside a `strong` panel already
        // and stacking blurs kills scroll perf on 500-row lists.
        "relative max-h-[420px] w-full overflow-auto rounded-xl border border-tp-glass-edge bg-tp-glass-inner",
        className,
      )}
      data-testid="residual-pyramid-root"
    >
      <svg
        width={width}
        height={height}
        role="img"
        aria-label="Residual pyramid — per-chunk axis decomposition"
      >
        {chunks.map((c, rowIdx) => {
          const y = rowIdx * (ROW_HEIGHT + ROW_GAP);
          const highlighted = hoveredId === c.chunk_id;
          const dim =
            hoveredId !== null && hoveredId !== c.chunk_id ? 0.3 : 1;

          // Compute segment widths from explained_energy.
          const total = c.pyramid_levels.reduce(
            (acc, l) => acc + l.explained_energy,
            0,
          );
          let xCursor = 0;

          const row = (
            <g
              key={c.chunk_id}
              transform={`translate(0, ${y})`}
              opacity={dim}
              onMouseOver={(ev) => {
                setHoveredId(c.chunk_id);
                const rect = (
                  ev.currentTarget.ownerSVGElement as SVGSVGElement
                ).getBoundingClientRect();
                showTooltip({
                  tooltipData: { chunk: c },
                  tooltipLeft: ev.clientX - rect.left,
                  tooltipTop: ev.clientY - rect.top,
                });
              }}
              onMouseOut={() => {
                setHoveredId(null);
                hideTooltip();
              }}
              data-testid={`pyramid-row-${c.chunk_id}`}
              style={{ cursor: "pointer" }}
            >
              <text
                x={LABEL_COL - 6}
                y={ROW_HEIGHT - 2}
                textAnchor="end"
                fontSize={9}
                fill="var(--tp-ink-3)"
                className="font-mono"
              >
                #{c.chunk_id}
              </text>
              {highlighted ? (
                <rect
                  x={LABEL_COL - 2}
                  y={-1}
                  width={rowWidth + 4}
                  height={ROW_HEIGHT + 2}
                  fill="none"
                  stroke="var(--tp-amber)"
                  strokeWidth={1}
                  rx={2}
                  style={{
                    filter:
                      "drop-shadow(0 0 4px var(--tp-amber-glow))",
                  }}
                />
              ) : null}
              {c.pyramid_levels.map((lvl, segIdx) => {
                const frac =
                  total > 0 ? lvl.explained_energy / total : 0;
                const w = rowWidth * frac;
                const x = LABEL_COL + xCursor;
                xCursor += w;
                // Level position → warm ramp stop. t=0 = bright amber,
                // t=0.5 = ember, t=1 = peach.
                const t =
                  maxDepth <= 1 ? 0 : segIdx / (maxDepth - 1);
                const fill = warmRamp(t);
                const showLabel = w > 48;
                return (
                  <g key={segIdx}>
                    <rect
                      x={x}
                      y={0}
                      width={Math.max(0, w - 0.5)}
                      height={ROW_HEIGHT}
                      fill={fill}
                      rx={1.5}
                    />
                    {showLabel ? (
                      <text
                        x={x + 4}
                        y={ROW_HEIGHT - 3}
                        fontSize={8}
                        fill="var(--tp-ink)"
                        fillOpacity={0.78}
                        className="font-mono"
                      >
                        {lvl.axis_label}
                      </text>
                    ) : null}
                  </g>
                );
              })}
            </g>
          );

          if (reduced) return row;
          return (
            <motion.g
              key={c.chunk_id}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{
                duration: 0.2,
                delay: (rowIdx * staggerMs) / 1000,
                ease: "easeOut",
              }}
            >
              {row}
            </motion.g>
          );
        })}
      </svg>

      {tooltipOpen && tooltipData ? (
        <TooltipInPortal
          top={tooltipTop}
          left={tooltipLeft}
          style={{
            ...defaultStyles,
            background: "var(--tp-glass-2)",
            color: "var(--tp-ink)",
            border: "1px solid var(--tp-glass-edge)",
            borderRadius: 8,
            backdropFilter: "blur(12px) saturate(1.5)",
            WebkitBackdropFilter: "blur(12px) saturate(1.5)",
            boxShadow: "var(--tp-shadow-panel)",
            fontSize: 11,
            padding: "6px 8px",
          }}
        >
          <div className="font-mono text-[11px] leading-4">
            <div className="font-semibold text-tp-ink">
              chunk #{tooltipData.chunk.chunk_id}
            </div>
            <ul className="mt-1 space-y-0.5">
              {tooltipData.chunk.pyramid_levels.map((l, i) => {
                const t =
                  maxDepth <= 1 ? 0 : i / (maxDepth - 1);
                return (
                  <li
                    key={i}
                    className="flex items-center gap-1 text-tp-ink-2"
                  >
                    <span
                      className="inline-block h-2 w-2 rounded-sm"
                      style={{ backgroundColor: warmRamp(t) }}
                    />
                    <span>{l.axis_label}</span>
                    <span className="ml-auto tabular-nums">
                      {(l.explained_energy * 100).toFixed(1)}%
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        </TooltipInPortal>
      ) : null}
    </div>
  );
}

// -------- helpers --------

/**
 * Three-stop warm ramp: amber → ember → peach. `t` is clamped to [0, 1].
 * Expressed with `color-mix` so the ramp tracks token shifts across themes
 * without a separate light/dark code path.
 *
 * The row outline on hover is what carries the a11y highlight signal, so
 * colour alone isn't load-bearing for distinguishing pyramid levels.
 */
function warmRamp(t: number): string {
  const c = Math.max(0, Math.min(1, t));
  if (c <= 0.5) {
    // amber → ember in the lower half
    const pct = (c / 0.5) * 100;
    return `color-mix(in oklch, var(--tp-amber) ${(100 - pct).toFixed(
      1,
    )}%, var(--tp-ember))`;
  }
  // ember → peach in the upper half
  const pct = ((c - 0.5) / 0.5) * 100;
  return `color-mix(in oklch, var(--tp-ember) ${(100 - pct).toFixed(
    1,
  )}%, var(--tp-peach))`;
}

/**
 * Helper exported for tests / rows consumed outside SVG.
 */
export function pyramidRowSummary(levels: PyramidLevel[]): string {
  return levels
    .map((l) => `${l.axis_label}:${(l.explained_energy * 100).toFixed(0)}%`)
    .join(" · ");
}
