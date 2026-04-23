"use client";

import * as React from "react";
import { useMotion } from "@/components/ui/motion-safe";
import { cn } from "@/lib/utils";
import type { Runner } from "@/lib/mocks/nodes";
import { capabilityOf } from "./capabilities";
import { RunnerNode } from "./runner-node";

/**
 * Radial topology SVG — Tidepool retoken (Phase 5d).
 *
 * Visual model:
 *   - Gateway at centre: amber→ember gradient disc with amber-glow outer halo.
 *     Pulses via `tp-breathe-amber` (ring-1 opacity lift).
 *   - Two orbital rings (ellipses) rendered as dashed `var(--tp-ink-4)` guides.
 *   - Satellites render as coloured circles (ok / warn / muted) plus label.
 *   - Edges stroke from centre to each node; dash-offset animates the data
 *     flow hint. A capability filter desaturates non-matching nodes+edges.
 *
 * Geometry (preserved from the pre-retoken component):
 *   Ring 0 (inner)  — 6 slots  · rx/ry = 180/150
 *   Ring 1 (outer)  — 12 slots · rx/ry = 320/280
 *
 *     angle = -π/2 + 2π · slot / N
 *     x     = cx + rx · cos(angle)
 *     y     = cy + ry · sin(angle)
 *
 * Reduced motion:
 *   - Pulse, shake, and dashflow animations all collapse to static state via
 *     the scoped <style> block below.
 */

const VIEWBOX = 800;
const CENTER = VIEWBOX / 2;
const GATEWAY_RADIUS = 54;
const GATEWAY_GLOW_RADIUS = 100;

interface RingSpec {
  slots: number;
  rx: number;
  ry: number;
  nodeRadius: number;
}

const RINGS: Record<0 | 1, RingSpec> = {
  0: { slots: 6, rx: 180, ry: 150, nodeRadius: 22 },
  1: { slots: 12, rx: 320, ry: 280, nodeRadius: 18 },
};

interface PositionedRunner {
  runner: Runner;
  cx: number;
  cy: number;
  r: number;
}

function position(runner: Runner): PositionedRunner {
  const ring = RINGS[runner.ring];
  const angle = -Math.PI / 2 + (2 * Math.PI * runner.slot) / ring.slots;
  const cx = CENTER + ring.rx * Math.cos(angle);
  const cy = CENTER + ring.ry * Math.sin(angle);
  return { runner, cx, cy, r: ring.nodeRadius };
}

function edgeStrokeVar(health: Runner["health"]): string {
  if (health === "healthy") return "var(--tp-amber)";
  if (health === "degraded") return "var(--tp-warn)";
  return "var(--tp-ink-4)";
}

export interface TopologyGraphProps {
  runners: Runner[];
  selectedId: string | null;
  onSelect: (runner: Runner | null) => void;
  /** When set, runners whose tools don't include this capability are desaturated. */
  capabilityFilter?: string | null;
  className?: string;
}

export function TopologyGraph({
  runners,
  selectedId,
  onSelect,
  capabilityFilter = null,
  className,
}: TopologyGraphProps) {
  const { reduced } = useMotion();

  const positioned = React.useMemo(
    () => runners.map(position),
    [runners],
  );

  // Scoped keyframes — pulse glow, edge dashflow, degraded jitter. Reduced
  // motion collapses every animation to the static state.
  const styleBlock = React.useMemo(() => {
    if (reduced) {
      return `
        .nodes-halo { animation: none; }
        .nodes-shake { animation: none; }
        .nodes-dash { animation: none; stroke-dashoffset: 0; }
        .nodes-gate-pulse { animation: none; }
      `;
    }
    return `
      @keyframes nodes-dash-kf {
        from { stroke-dashoffset: 28; }
        to   { stroke-dashoffset: 0; }
      }
      @keyframes nodes-halo-kf {
        0%, 100% { opacity: 0.35; }
        50%      { opacity: 0.7; }
      }
      @keyframes nodes-gate-pulse-kf {
        0%, 100% { opacity: 0.45; transform: scale(1); }
        50%      { opacity: 0.85; transform: scale(1.04); }
      }
      @keyframes nodes-shake-kf {
        0%, 88%, 100% { transform: translateX(0); }
        90%           { transform: translateX(-1.6px); }
        92%           { transform: translateX(1.6px); }
        94%           { transform: translateX(-1.2px); }
        96%           { transform: translateX(1.2px); }
      }
      .nodes-dash { animation: nodes-dash-kf 1.4s linear infinite; }
      .nodes-halo { animation: nodes-halo-kf 2.4s ease-in-out infinite; }
      .nodes-gate-pulse {
        animation: nodes-gate-pulse-kf 3.2s ease-in-out infinite;
        transform-origin: ${CENTER}px ${CENTER}px;
        transform-box: fill-box;
      }
      .nodes-shake { animation: nodes-shake-kf 6s ease-in-out infinite; }
    `;
  }, [reduced]);

  const gradientId = React.useId();
  const glowId = React.useId();

  return (
    <div
      className={cn("relative w-full overflow-hidden", className)}
      data-testid="topology-graph"
    >
      <style>{styleBlock}</style>
      <svg
        viewBox={`0 0 ${VIEWBOX} ${VIEWBOX}`}
        role="img"
        aria-label="Runner topology"
        className="block h-auto w-full"
        onClick={() => onSelect(null)}
      >
        <defs>
          <radialGradient id={gradientId} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--tp-amber)" stopOpacity="1" />
            <stop offset="100%" stopColor="var(--tp-ember)" stopOpacity="0.95" />
          </radialGradient>
          <radialGradient id={glowId} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--tp-amber-glow)" />
            <stop offset="100%" stopColor="var(--tp-amber-glow)" stopOpacity="0" />
          </radialGradient>
        </defs>

        {/* Orbital guides — quiet dashed ellipses. */}
        <ellipse
          cx={CENTER}
          cy={CENTER}
          rx={RINGS[0].rx}
          ry={RINGS[0].ry}
          fill="none"
          stroke="var(--tp-ink-4)"
          strokeOpacity={0.32}
          strokeDasharray="2 8"
          strokeWidth={1}
          aria-hidden="true"
        />
        <ellipse
          cx={CENTER}
          cy={CENTER}
          rx={RINGS[1].rx}
          ry={RINGS[1].ry}
          fill="none"
          stroke="var(--tp-ink-4)"
          strokeOpacity={0.26}
          strokeDasharray="2 8"
          strokeWidth={1}
          aria-hidden="true"
        />

        {/* Edges — first, so satellites paint over them. */}
        {positioned.map(({ runner, cx, cy }) => {
          const stroke = edgeStrokeVar(runner.health);
          const caps = capabilityOf(runner);
          const capMatch =
            capabilityFilter === null || caps.includes(capabilityFilter);
          const baseOpacity = runner.health === "offline" ? 0.2 : 0.45;
          const opacity = capMatch ? baseOpacity : 0.12;
          const active = runner.health !== "offline" && capMatch;
          return (
            <path
              key={`link-${runner.id}`}
              d={`M ${CENTER} ${CENTER} L ${cx} ${cy}`}
              stroke={stroke}
              strokeOpacity={opacity}
              strokeWidth={1.25}
              strokeDasharray="6 6"
              fill="none"
              className={active ? "nodes-dash" : undefined}
              aria-hidden="true"
              data-testid={`link-${runner.id}`}
            />
          );
        })}

        {/* Gateway — outer glow disc, then pulsing halo, then gradient body.
            Motion classes drop when `reduced` so the pulse/halo collapse to
            static state rather than leaking into the reduced-motion DOM. */}
        <g aria-label="Gateway">
          <circle
            cx={CENTER}
            cy={CENTER}
            r={GATEWAY_GLOW_RADIUS}
            fill={`url(#${glowId})`}
            className={reduced ? undefined : "nodes-gate-pulse"}
            aria-hidden="true"
          />
          {reduced ? null : (
            <circle
              cx={CENTER}
              cy={CENTER}
              r={GATEWAY_RADIUS + 10}
              fill="none"
              stroke="var(--tp-amber)"
              strokeOpacity={0.35}
              strokeWidth={1.4}
              className="nodes-halo"
              aria-hidden="true"
            />
          )}
          <circle
            cx={CENTER}
            cy={CENTER}
            r={GATEWAY_RADIUS}
            fill={`url(#${gradientId})`}
            stroke="var(--tp-amber)"
            strokeOpacity={0.55}
            strokeWidth={1}
          />
          {/* Server-stack glyph (pure SVG copy of Lucide `Server`), tinted
              against the ember body. */}
          <g
            aria-hidden="true"
            transform={`translate(${CENTER - 13} ${CENTER - 16})`}
            stroke="var(--tp-glass-hl)"
            strokeOpacity={0.9}
            strokeWidth={1.6}
            strokeLinecap="round"
            strokeLinejoin="round"
            fill="none"
          >
            <rect x={0} y={0} width={26} height={10} rx={2.5} />
            <rect x={0} y={14} width={26} height={10} rx={2.5} />
            <line x1={7} y1={5} x2={7.01} y2={5} />
            <line x1={7} y1={19} x2={7.01} y2={19} />
          </g>
          <text
            x={CENTER}
            y={CENTER + GATEWAY_RADIUS + 22}
            fontSize="13"
            fontWeight={600}
            textAnchor="middle"
            fill="var(--tp-ink)"
            style={{ letterSpacing: "-0.01em" }}
          >
            gateway
          </text>
        </g>

        {/* Satellite runners — drawn last so they z-stack above edges. */}
        {positioned.map(({ runner, cx, cy, r }) => {
          const caps = capabilityOf(runner);
          const dim =
            capabilityFilter !== null && !caps.includes(capabilityFilter);
          return (
            <RunnerNode
              key={runner.id}
              runner={runner}
              cx={cx}
              cy={cy}
              r={r}
              selected={selectedId === runner.id}
              reduced={reduced}
              dim={dim}
              onSelect={(next) => onSelect(next)}
            />
          );
        })}
      </svg>
    </div>
  );
}

export default TopologyGraph;
