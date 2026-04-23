"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import type { Runner } from "@/lib/mocks/nodes";
import { capabilityOf } from "./capabilities";

/**
 * Side rail for the Nodes page: a scrollable list of every satellite runner,
 * grouped by ring. Each entry exposes the runner's status (coloured dot +
 * label), capabilities as tiny chips, and its latency in mono.
 *
 * Clicking an entry toggles the parent's `selectedId`, which drives the
 * topology selection ring and the DetailDrawer. Keyboard: Tab to focus,
 * Enter/Space to toggle.
 */

export interface NodeSideRailProps {
  runners: Runner[];
  selectedId: string | null;
  onSelect: (runner: Runner | null) => void;
  /** If set, entries without this capability are dimmed (not hidden). */
  capabilityFilter?: string | null;
  className?: string;
}

const HEALTH_LABEL_KEY: Record<Runner["health"], string> = {
  healthy: "nodes.tp.healthOnline",
  degraded: "nodes.tp.healthDegraded",
  offline: "nodes.tp.healthOffline",
};

const HEALTH_DOT: Record<Runner["health"], string> = {
  healthy: "bg-tp-ok shadow-[0_0_6px_color-mix(in_oklch,var(--tp-ok)_40%,transparent)]",
  degraded: "bg-tp-warn shadow-[0_0_6px_color-mix(in_oklch,var(--tp-warn)_40%,transparent)]",
  offline: "bg-tp-ink-4",
};

function formatLatency(r: Runner): string {
  if (r.health === "offline") return "—";
  return `${r.latencyMs}ms`;
}

export function NodeSideRail({
  runners,
  selectedId,
  onSelect,
  capabilityFilter = null,
  className,
}: NodeSideRailProps) {
  const { t } = useTranslation();

  // Preserve the original topology order but split by ring for a tidy list.
  const innerRing = React.useMemo(
    () =>
      runners
        .filter((r) => r.ring === 0)
        .sort((a, b) => a.slot - b.slot),
    [runners],
  );
  const outerRing = React.useMemo(
    () =>
      runners
        .filter((r) => r.ring === 1)
        .sort((a, b) => a.slot - b.slot),
    [runners],
  );

  const renderEntry = (r: Runner) => {
    const caps = capabilityOf(r);
    const dim = capabilityFilter !== null && !caps.includes(capabilityFilter);
    const selected = r.id === selectedId;
    return (
      <li key={r.id}>
        <button
          type="button"
          data-testid={`node-rail-entry-${r.id}`}
          data-selected={selected ? "true" : "false"}
          aria-pressed={selected}
          onClick={() => onSelect(selected ? null : r)}
          className={cn(
            "group flex w-full flex-col gap-1 rounded-xl border px-3 py-2.5 text-left",
            "transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
            selected
              ? "border-tp-amber/45 bg-tp-amber-soft"
              : "border-tp-glass-edge bg-tp-glass-inner hover:bg-tp-glass-inner-hover",
            dim && "opacity-55",
          )}
        >
          <div className="flex items-center gap-2">
            <span
              aria-hidden
              className={cn("h-1.5 w-1.5 shrink-0 rounded-full", HEALTH_DOT[r.health])}
            />
            <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-tp-ink">
              {r.hostname}
            </span>
            <span className="shrink-0 font-mono text-[10.5px] tabular-nums text-tp-ink-3">
              {formatLatency(r)}
            </span>
          </div>
          <div className="flex items-center gap-1.5 pl-3.5 font-mono text-[10px] text-tp-ink-4">
            <span
              className={cn(
                "rounded-sm px-1 py-px",
                r.health === "healthy" && "text-tp-ok",
                r.health === "degraded" && "text-tp-warn",
                r.health === "offline" && "text-tp-ink-4",
              )}
            >
              {t(HEALTH_LABEL_KEY[r.health])}
            </span>
            <span className="text-tp-ink-4">·</span>
            <span className="truncate">
              {caps.length === 0
                ? t("nodes.tp.capsNone")
                : caps.slice(0, 3).join(" · ") +
                  (caps.length > 3 ? ` · +${caps.length - 3}` : "")}
            </span>
          </div>
        </button>
      </li>
    );
  };

  return (
    <GlassPanel
      variant="soft"
      as="aside"
      className={cn("flex flex-col overflow-hidden", className)}
      data-testid="node-side-rail"
    >
      <header className="flex items-center justify-between border-b border-tp-glass-edge px-4 py-3">
        <div className="text-[13px] font-semibold text-tp-ink">
          {t("nodes.tp.sideRailTitle")}
        </div>
        <div className="font-mono text-[10.5px] text-tp-ink-3">
          {runners.length}
        </div>
      </header>
      <div className="flex flex-col gap-3 overflow-y-auto px-3 py-3">
        {innerRing.length > 0 ? (
          <section>
            <div className="mb-1.5 px-1 font-mono text-[10px] uppercase tracking-[0.12em] text-tp-ink-4">
              {t("nodes.tp.ringInner")} · {innerRing.length}
            </div>
            <ul className="flex flex-col gap-1.5">{innerRing.map(renderEntry)}</ul>
          </section>
        ) : null}
        {outerRing.length > 0 ? (
          <section>
            <div className="mb-1.5 px-1 font-mono text-[10px] uppercase tracking-[0.12em] text-tp-ink-4">
              {t("nodes.tp.ringOuter")} · {outerRing.length}
            </div>
            <ul className="flex flex-col gap-1.5">{outerRing.map(renderEntry)}</ul>
          </section>
        ) : null}
      </div>
    </GlassPanel>
  );
}

export default NodeSideRail;
