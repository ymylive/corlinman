"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw } from "lucide-react";

import { cn } from "@/lib/utils";
import { DetailDrawer } from "@/components/ui/detail-drawer";
import { JsonView } from "@/components/ui/json-view";
import { MiniSparkline, type SparkBar } from "@/components/ui/mini-sparkline";
import type { Runner } from "@/lib/mocks/nodes";
import { capabilityOf } from "./capabilities";

/**
 * Per-node inspection drawer (Tidepool Phase 5d).
 *
 * Layout inside DetailDrawer:
 *   - meta row : health pill · hostname mono · latency mono
 *   - subsystem: capabilities inline (amber mono)
 *   - title    : runner id
 *   - Section 1: Heartbeat — last ping + connected-for + error rate + sparkline
 *   - Section 2: Capabilities — advertised tools as a JsonView payload
 *   - footer   : Reconnect button (currently a no-op placeholder)
 */

export interface NodeDetailDrawerProps {
  runner: Runner;
  onReconnect?: (runner: Runner) => void;
  className?: string;
}

function formatDuration(sec: number): string {
  if (sec <= 0) return "—";
  if (sec < 60) return `${sec}s`;
  if (sec < 3_600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  const h = Math.floor(sec / 3_600);
  const m = Math.floor((sec % 3_600) / 60);
  return `${h}h ${m}m`;
}

function formatLastPing(ms: number): string {
  if (ms < 1_000) return `${ms}ms`;
  return `${(ms / 1_000).toFixed(1)}s`;
}

/**
 * Deterministic latency series for the sparkline. Seeded by the runner's id
 * so the bars are stable across re-renders / SSR hydration.
 *
 * Real /wstool/runners response from B4-BE3 will expose a proper recent-ping
 * ring buffer; this fabrication is a plausible stand-in.
 */
function fabricateLatencyBars(runner: Runner): SparkBar[] {
  if (runner.health === "offline") {
    return Array.from({ length: 6 }, () => ({ height: 8, tone: "muted" as const }));
  }
  const seed = [...runner.id].reduce((a, c) => ((a * 31 + c.charCodeAt(0)) & 0xffff), 0);
  const bars: SparkBar[] = [];
  let x = seed;
  const tone: SparkBar["tone"] =
    runner.health === "degraded" ? "warn" : "ok";
  for (let i = 0; i < 6; i += 1) {
    x = (x * 1103515245 + 12345) & 0x7fffffff;
    const base = 52 + (x % 45); // 52–96
    const amp = runner.health === "degraded" ? 1.05 : 1.0;
    bars.push({ height: Math.min(100, Math.round(base * amp)), tone });
  }
  return bars;
}

export function NodeDetailDrawer({
  runner,
  onReconnect,
  className,
}: NodeDetailDrawerProps) {
  const { t } = useTranslation();
  const caps = capabilityOf(runner);
  const bars = React.useMemo(() => fabricateLatencyBars(runner), [runner]);

  const healthPillClass = {
    healthy: "bg-tp-ok-soft text-tp-ok border-tp-ok/25",
    degraded: "bg-tp-warn-soft text-tp-warn border-tp-warn/30",
    offline: "bg-tp-glass-inner text-tp-ink-3 border-tp-glass-edge",
  }[runner.health];

  const meta = (
    <>
      <span
        className={cn(
          "rounded-md border px-2 py-[2px] font-mono text-[10px] font-medium tracking-[0.04em]",
          healthPillClass,
        )}
        data-testid="node-drawer-health-pill"
      >
        {t(`nodes.tp.health${capitaliseHealth(runner.health)}`)}
      </span>
      <span className="font-mono text-[12.5px] tabular-nums text-tp-ink">
        {runner.hostname}
      </span>
      <span className="font-mono text-[11px] tabular-nums text-tp-ink-3">
        {runner.health === "offline" ? "—" : `${runner.latencyMs}ms`}
      </span>
    </>
  );

  const subsystemLine =
    caps.length > 0 ? caps.join(" · ") : t("nodes.tp.capsNone");

  // Payload of the advertised capabilities — surfaces exactly what a
  // /wstool/runners row looks like so operators can eyeball correctness.
  const advertPayload = React.useMemo(
    () => ({
      id: runner.id,
      hostname: runner.hostname,
      ring: runner.ring === 0 ? "inner" : "outer",
      capabilities: caps,
      tools: runner.tools,
    }),
    [runner, caps],
  );

  return (
    <DetailDrawer
      title={
        <span className="font-mono text-[14px] text-tp-ink">
          {runner.id}
        </span>
      }
      subsystem={subsystemLine}
      meta={meta}
      className={className}
      data-testid="node-detail-drawer"
    >
      <DetailDrawer.Section label={t("nodes.tp.sectionHeartbeat")}>
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 text-[12.5px]">
          <Field
            label={t("nodes.tp.fieldLastPing")}
            value={
              <span>
                {formatLastPing(runner.lastPingMs)}{" "}
                <span className="text-tp-ink-4">{t("nodes.tp.ago")}</span>
              </span>
            }
          />
          <Field
            label={t("nodes.tp.fieldConnectedFor")}
            value={formatDuration(runner.connectedForSec)}
          />
          <Field
            label={t("nodes.tp.fieldErrorRate")}
            value={
              <span className={runner.errorRate > 0.01 ? "text-tp-warn" : ""}>
                {(runner.errorRate * 100).toFixed(2)}%
              </span>
            }
          />
          <Field
            label={t("nodes.tp.fieldTools")}
            value={runner.toolCount}
          />
        </div>

        <div className="mt-4 flex items-center gap-3">
          <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-tp-ink-4">
            {t("nodes.tp.sparkLatency")}
          </span>
          <MiniSparkline
            bars={bars}
            height={18}
            label={t("nodes.tp.sparkAria", {
              host: runner.hostname,
            })}
          />
        </div>
      </DetailDrawer.Section>

      <DetailDrawer.Section label={t("nodes.tp.sectionCapabilities")}>
        {runner.tools.length === 0 ? (
          <div
            className={cn(
              "rounded-lg border border-dashed border-tp-glass-edge",
              "bg-tp-glass-inner p-4 text-center",
              "font-mono text-[11.5px] text-tp-ink-4",
            )}
          >
            {t("nodes.tp.capsEmpty")}
          </div>
        ) : (
          <JsonView value={advertPayload} />
        )}
      </DetailDrawer.Section>

      <DetailDrawer.Section label={t("nodes.tp.sectionActions")}>
        <button
          type="button"
          onClick={() => onReconnect?.(runner)}
          disabled={!onReconnect}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[12px] font-medium",
            "bg-tp-glass-inner border-tp-glass-edge text-tp-ink-2",
            "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
            "disabled:cursor-not-allowed disabled:opacity-60",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
          data-testid="node-drawer-reconnect"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          {t("nodes.tp.actionReconnect")}
        </button>
        <p className="mt-2 text-[11.5px] text-tp-ink-4">
          {t("nodes.tp.actionReconnectHint")}
        </p>
      </DetailDrawer.Section>
    </DetailDrawer>
  );
}

function Field({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-tp-ink-4">
        {label}
      </span>
      <span className="font-mono tabular-nums text-tp-ink">{value}</span>
    </div>
  );
}

function capitaliseHealth(h: Runner["health"]): string {
  return h.charAt(0).toUpperCase() + h.slice(1);
}

export default NodeDetailDrawer;
