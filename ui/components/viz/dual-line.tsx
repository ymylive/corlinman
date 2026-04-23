/**
 * Entropy / logic_depth dual-line chart — Tidepool (Phase 5e) retoken.
 *
 * Two lines drawn with `@visx/shape LinePath`: entropy (amber) and
 * logic_depth (ember, dashed). Both share a 0..1 y-axis. On mount each path
 * animates its `pathLength` from 0 → 1 over 1200 ms; `prefers-reduced-motion`
 * snaps to 1 instantly. Hover draws a vertical guideline and shows a warm
 * glass tooltip with both values.
 */
"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import { scaleLinear } from "@visx/scale";
import { LinePath } from "@visx/shape";
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

interface DualLineProps {
  chunks: TagMemoChunk[];
  className?: string;
}

interface InnerProps extends DualLineProps {
  width: number;
  height: number;
}

interface TipData {
  chunk: TagMemoChunk;
}

const MARGIN = { top: 14, right: 18, bottom: 32, left: 40 };

export function DualLine({ chunks, className }: DualLineProps) {
  return (
    <div className={cn("relative h-[320px] w-full", className)}>
      <ParentSize>
        {({ width, height }) =>
          width > 0 && height > 0 ? (
            <DualLineInner
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

function DualLineInner({ chunks, width, height }: InnerProps) {
  const reduced = useReducedMotion();
  const { hoveredId, setHoveredId } = useHoveredId();

  const innerW = Math.max(0, width - MARGIN.left - MARGIN.right);
  const innerH = Math.max(0, height - MARGIN.top - MARGIN.bottom);

  const xScale = React.useMemo(
    () =>
      scaleLinear({
        domain: [0, Math.max(1, chunks.length - 1)],
        range: [0, innerW],
      }),
    [chunks.length, innerW],
  );
  const yScale = React.useMemo(
    () => scaleLinear({ domain: [0, 1], range: [innerH, 0] }),
    [innerH],
  );

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

  // pathLength animation — the existing test gates this on `reduced` so the
  // path's `d` attr is rendered immediately rather than drawn in.
  const lineAnim = reduced
    ? { initial: { pathLength: 1 }, animate: { pathLength: 1 } }
    : {
        initial: { pathLength: 0 },
        animate: { pathLength: 1 },
        transition: { duration: 1.2, ease: "easeOut" as const },
      };

  const handleMouseMove = (ev: React.MouseEvent<SVGRectElement>) => {
    if (chunks.length === 0) return;
    const rect = (ev.currentTarget as SVGRectElement).getBoundingClientRect();
    const mouseX = ev.clientX - rect.left;
    const raw = xScale.invert(mouseX);
    const idx = Math.max(0, Math.min(chunks.length - 1, Math.round(raw)));
    const chunk = chunks[idx];
    if (!chunk) return;
    setHoveredId(chunk.chunk_id);
    showTooltip({
      tooltipData: { chunk },
      tooltipLeft: MARGIN.left + xScale(idx),
      tooltipTop: MARGIN.top + yScale(chunk.entropy),
    });
  };

  const handleMouseLeave = () => {
    setHoveredId(null);
    hideTooltip();
  };

  const hoveredIdx = React.useMemo(() => {
    if (hoveredId === null) return null;
    return chunks.findIndex((c) => c.chunk_id === hoveredId);
  }, [hoveredId, chunks]);

  // Quiet hairlines: the panel already reads as the figure frame; the axes
  // fade into ink-4 so the warm strokes dominate the composition.
  const axisStroke = "var(--tp-glass-edge)";
  const tickLabelFill = "var(--tp-ink-3)";

  return (
    <div ref={containerRef} className="relative h-full w-full">
      <svg
        width={width}
        height={height}
        role="img"
        aria-label="Entropy and logic depth by chunk"
      >
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          <AxisBottom
            top={innerH}
            scale={xScale}
            numTicks={6}
            stroke={axisStroke}
            tickStroke={axisStroke}
            tickLabelProps={{
              fill: tickLabelFill,
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
          />

          <LinePath
            data={chunks}
            x={(_, i) => xScale(i)}
            y={(d) => yScale(d.entropy)}
          >
            {({ path }) => {
              const d = path(chunks) ?? "";
              return (
                <motion.path
                  d={d}
                  fill="none"
                  stroke="var(--tp-amber)"
                  strokeWidth={1.75}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  style={{
                    filter:
                      "drop-shadow(0 0 4px color-mix(in oklch, var(--tp-amber) 30%, transparent))",
                  }}
                  {...lineAnim}
                  data-testid="line-entropy"
                />
              );
            }}
          </LinePath>
          <LinePath
            data={chunks}
            x={(_, i) => xScale(i)}
            y={(d) => yScale(d.logic_depth)}
          >
            {({ path }) => {
              const d = path(chunks) ?? "";
              return (
                <motion.path
                  d={d}
                  fill="none"
                  stroke="var(--tp-ember)"
                  strokeWidth={1.75}
                  strokeDasharray="4 3"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  {...lineAnim}
                  data-testid="line-logic-depth"
                />
              );
            }}
          </LinePath>

          {hoveredIdx !== null && hoveredIdx >= 0 ? (
            <line
              x1={xScale(hoveredIdx)}
              x2={xScale(hoveredIdx)}
              y1={0}
              y2={innerH}
              stroke="var(--tp-amber)"
              strokeOpacity={0.45}
              strokeDasharray="3 3"
            />
          ) : null}

          {/* Transparent hit-layer for mouse tracking. */}
          <rect
            x={0}
            y={0}
            width={innerW}
            height={innerH}
            fill="transparent"
            onMouseMove={handleMouseMove}
            onMouseLeave={handleMouseLeave}
          />
        </g>

        {/* Legend — amber solid = entropy; ember dashed = logic_depth. The
            dash pattern keeps the two lines distinguishable under monochrome
            or deuteranopic viewing. */}
        <g transform={`translate(${MARGIN.left + 4}, ${MARGIN.top + 4})`}>
          <line
            x1={0}
            x2={18}
            y1={4}
            y2={4}
            stroke="var(--tp-amber)"
            strokeWidth={1.75}
            strokeLinecap="round"
          />
          <text x={22} y={7} fontSize={10} fill="var(--tp-ink-3)">
            entropy
          </text>
          <line
            x1={80}
            x2={98}
            y1={4}
            y2={4}
            stroke="var(--tp-ember)"
            strokeWidth={1.75}
            strokeDasharray="4 3"
            strokeLinecap="round"
          />
          <text x={102} y={7} fontSize={10} fill="var(--tp-ink-3)">
            logic_depth
          </text>
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
              chunk #{tooltipData.chunk.chunk_id}
            </div>
            <div className="flex items-center gap-1.5 text-tp-ink-2">
              <span
                aria-hidden
                className="inline-block h-2 w-2 rounded-full"
                style={{ background: "var(--tp-amber)" }}
              />
              entropy: {tooltipData.chunk.entropy.toFixed(3)}
            </div>
            <div className="flex items-center gap-1.5 text-tp-ink-2">
              <span
                aria-hidden
                className="inline-block h-2 w-2 rounded-full"
                style={{ background: "var(--tp-ember)" }}
              />
              logic_depth: {tooltipData.chunk.logic_depth.toFixed(3)}
            </div>
          </div>
        </TooltipInPortal>
      ) : null}
      {/* Screen-reader fallback. The SVG above is aria-labelled but a table
          gives AT users the raw series. */}
      <details className="sr-only">
        <summary>Entropy / logic_depth table (accessibility fallback)</summary>
        <table>
          <thead>
            <tr>
              <th>chunk_id</th>
              <th>entropy</th>
              <th>logic_depth</th>
            </tr>
          </thead>
          <tbody>
            {chunks.map((c) => (
              <tr key={c.chunk_id}>
                <td>{c.chunk_id}</td>
                <td>{c.entropy.toFixed(3)}</td>
                <td>{c.logic_depth.toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
