/**
 * EPA 3-axis scatter — Tidepool (Phase 5e) retoken.
 *
 * Renders one circle per chunk on a 2D projection of the first two EPA axes.
 * The third axis is colour-encoded (via `logic_depth`) and size is
 * proportional to the chunk's dominant energy. Hovering a circle updates the
 * shared `hoveredId` so linked panels react too.
 *
 * Palette: amber (bright/shallow) → ember (deep) gradient. Hover lifts the
 * dot to the ember stop plus a warm ring glow. Axes + legend hairlines use
 * `--tp-ink-4` / `--tp-glass-edge` so they recede into the glass panel.
 */
"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import { scaleLinear } from "@visx/scale";
import { ParentSize } from "@visx/responsive";
import { AxisBottom, AxisLeft } from "@visx/axis";
import {
  useTooltip,
  useTooltipInPortal,
  defaultStyles,
} from "@visx/tooltip";

import { cn } from "@/lib/utils";
import type { TagMemoChunk } from "@/lib/mocks/tagmemo";
import { useHoveredId } from "./use-hovered-id";

interface EpaScatterProps {
  chunks: TagMemoChunk[];
  className?: string;
}

interface InnerProps extends EpaScatterProps {
  width: number;
  height: number;
}

const MARGIN = { top: 14, right: 18, bottom: 36, left: 42 };
const MIN_R = 3;
const MAX_R = 10;

export function EpaScatter({ chunks, className }: EpaScatterProps) {
  return (
    <div className={cn("relative h-[320px] w-full", className)}>
      <ParentSize>
        {({ width, height }) =>
          width > 0 && height > 0 ? (
            <ScatterInner
              chunks={chunks}
              width={width}
              height={height}
            />
          ) : null
        }
      </ParentSize>
    </div>
  );
}

function ScatterInner({ chunks, width, height }: InnerProps) {
  const { hoveredId, setHoveredId } = useHoveredId();
  const reduced = useReducedMotion();

  const xs = chunks.map((c) => c.projections[0] ?? 0);
  const ys = chunks.map((c) => c.projections[1] ?? 0);
  const ds = chunks.map((c) => c.logic_depth);

  const xDomain = domainOf(xs);
  const yDomain = domainOf(ys);
  const dDomain = domainOf(ds, 0, 1);

  const innerW = Math.max(0, width - MARGIN.left - MARGIN.right);
  const innerH = Math.max(0, height - MARGIN.top - MARGIN.bottom);

  const xScale = React.useMemo(
    () => scaleLinear({ domain: xDomain, range: [0, innerW], nice: true }),
    [xDomain, innerW],
  );
  const yScale = React.useMemo(
    () => scaleLinear({ domain: yDomain, range: [innerH, 0], nice: true }),
    [yDomain, innerH],
  );
  const colorScale = React.useMemo(
    () => scaleLinear({ domain: dDomain, range: [0, 1] }),
    [dDomain],
  );

  const {
    tooltipOpen,
    tooltipData,
    tooltipLeft,
    tooltipTop,
    showTooltip,
    hideTooltip,
  } = useTooltip<TagMemoChunk>();
  const { containerRef, TooltipInPortal } = useTooltipInPortal({
    detectBounds: true,
    scroll: true,
  });

  const axisLabelX = chunks[0]?.dominant_axes[0]?.label ?? "axis_0";
  const axisLabelY = chunks[0]?.dominant_axes[1]?.label ?? "axis_1";
  const axisLabelD = chunks[0]?.dominant_axes[2]?.label ?? "axis_2";

  // Unique ids for gradient defs (one page may host multiple scatters).
  const gradId = React.useId();

  // Axis + tick styling — quiet hairlines on ink-4 / glass-edge so the dots
  // carry the visual weight.
  const axisStroke = "var(--tp-glass-edge)";
  const tickLabelFill = "var(--tp-ink-3)";
  const axisLabelFill = "var(--tp-ink-2)";

  return (
    <div ref={containerRef} className="relative h-full w-full">
      <svg
        width={width}
        height={height}
        role="img"
        aria-label="EPA 3-axis scatter"
      >
        <defs>
          {/* Pre-baked depth ramp used for the legend gradient only; the
              individual dots interpolate per-point via `colourForDepth` so
              each circle gets its own solid fill (easier to hover-swap). */}
          <linearGradient id={`${gradId}-legend`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="var(--tp-amber)" stopOpacity={0.9} />
            <stop offset="100%" stopColor="var(--tp-ember)" stopOpacity={0.95} />
          </linearGradient>
        </defs>
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          <AxisBottom
            top={innerH}
            scale={xScale}
            numTicks={5}
            stroke={axisStroke}
            tickStroke={axisStroke}
            tickLabelProps={{
              fill: tickLabelFill,
              fontSize: 10,
              textAnchor: "middle",
            }}
            label={axisLabelX}
            labelProps={{
              fill: axisLabelFill,
              fontSize: 10,
              textAnchor: "middle",
            }}
          />
          <AxisLeft
            scale={yScale}
            numTicks={5}
            stroke={axisStroke}
            tickStroke={axisStroke}
            tickLabelProps={{
              fill: tickLabelFill,
              fontSize: 10,
              textAnchor: "end",
              dx: -4,
              dy: 3,
            }}
            label={axisLabelY}
            labelProps={{
              fill: axisLabelFill,
              fontSize: 10,
              textAnchor: "middle",
            }}
          />

          {chunks.map((c) => {
            const cx = xScale(c.projections[0] ?? 0);
            const cy = yScale(c.projections[1] ?? 0);
            const energy = c.dominant_axes[0]?.energy ?? 0.25;
            const baseR = MIN_R + energy * (MAX_R - MIN_R);
            const highlighted = hoveredId === c.chunk_id;
            const dim =
              hoveredId !== null && hoveredId !== c.chunk_id
                ? 0.25
                : 1;
            const t = colorScale(c.logic_depth) ?? 0;
            const fill = highlighted
              ? "var(--tp-ember)"
              : colourForDepth(t);
            return (
              <motion.circle
                key={c.chunk_id}
                layout={!reduced}
                cx={cx}
                cy={cy}
                r={highlighted ? baseR * 1.6 : baseR}
                fill={fill}
                stroke={
                  highlighted
                    ? "var(--tp-amber)"
                    : "color-mix(in oklch, var(--tp-amber) 18%, transparent)"
                }
                strokeWidth={highlighted ? 1.4 : 0.5}
                opacity={dim}
                style={{
                  cursor: "pointer",
                  filter: highlighted
                    ? "drop-shadow(0 0 6px var(--tp-amber-glow))"
                    : undefined,
                }}
                data-testid={`scatter-dot-${c.chunk_id}`}
                onMouseOver={(ev) => {
                  setHoveredId(c.chunk_id);
                  showTooltip({
                    tooltipData: c,
                    tooltipLeft:
                      (ev.nativeEvent as MouseEvent).offsetX ?? cx,
                    tooltipTop:
                      (ev.nativeEvent as MouseEvent).offsetY ?? cy,
                  });
                }}
                onMouseOut={() => {
                  setHoveredId(null);
                  hideTooltip();
                }}
                onFocus={() => setHoveredId(c.chunk_id)}
                onBlur={() => setHoveredId(null)}
              />
            );
          })}
        </g>
        {/* Legend — amber → ember ramp keyed to logic_depth. */}
        <g transform={`translate(${Math.max(MARGIN.left, width - 150)},12)`}>
          <text fontSize={10} fill="var(--tp-ink-3)">
            {axisLabelD} (depth)
          </text>
          <rect
            x={0}
            y={8}
            width={100}
            height={6}
            rx={1.5}
            fill={`url(#${gradId}-legend)`}
          />
        </g>
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
              chunk #{tooltipData.chunk_id}
            </div>
            <div className="text-tp-ink-2">
              x: {tooltipData.projections[0]?.toFixed(2)} · y:{" "}
              {tooltipData.projections[1]?.toFixed(2)}
            </div>
            <div className="text-tp-ink-2">
              entropy: {tooltipData.entropy.toFixed(3)}
            </div>
            <div className="text-tp-ink-2">
              logic_depth: {tooltipData.logic_depth.toFixed(3)}
            </div>
          </div>
        </TooltipInPortal>
      ) : null}
    </div>
  );
}

// ------------- helpers -------------

function domainOf(
  vs: number[],
  fallbackLo = 0,
  fallbackHi = 1,
): [number, number] {
  if (vs.length === 0) return [fallbackLo, fallbackHi];
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of vs) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
    return [fallbackLo, fallbackHi];
  }
  if (lo === hi) return [lo - 1, hi + 1];
  return [lo, hi];
}

/**
 * Two-stop gradient from amber (shallow) to ember (deep) in OKLCH. Returns a
 * `color-mix(...)` expression so the stops track theme-driven token values
 * automatically — one gradient, two themes.
 *
 * Colour-blind safety: dot size encodes energy so depth isn't purely hue.
 */
function colourForDepth(t: number): string {
  const clamped = Math.max(0, Math.min(1, t));
  // Per spec: bright amber oklch(0.80 0.17 58 / 0.8) → deep ember
  // oklch(0.60 0.18 32 / 0.4). Expressed via color-mix so each token stop
  // still flips in light-mode (tp-amber, tp-ember shift L/C).
  const amberPct = (1 - clamped) * 100;
  const alpha = 0.4 + (1 - clamped) * 0.4; // 0.8 shallow → 0.4 deep
  const mix = `color-mix(in oklch, var(--tp-amber) ${amberPct.toFixed(
    1,
  )}%, var(--tp-ember))`;
  return `color-mix(in oklch, ${mix} ${(alpha * 100).toFixed(
    1,
  )}%, transparent)`;
}
