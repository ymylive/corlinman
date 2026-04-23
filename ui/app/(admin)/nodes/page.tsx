"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { useMotion } from "@/components/ui/motion-safe";
import { GlassPanel } from "@/components/ui/glass-panel";
import { TopologyGraph } from "@/components/nodes/topology-graph";
import { NodeSideRail } from "@/components/nodes/node-side-rail";
import { NodeDetailDrawer } from "@/components/nodes/node-detail-drawer";
import { PageHero } from "@/components/nodes/page-hero";
import { StatsRow } from "@/components/nodes/stats-row";
import { CapabilityFilter } from "@/components/nodes/capability-filter";
import {
  OfflineBlock,
  EmptyBlock,
} from "@/components/nodes/offline-block";
import { capabilityCounts, capabilityOf } from "@/components/nodes/capabilities";
import {
  fetchRunnersMock,
  summariseRunners,
  type Runner,
} from "@/lib/mocks/nodes";

/**
 * Distributed Nodes — Tidepool (Phase 5d) cutover.
 *
 * Layout:
 *   ┌── hero (prose) ────────────────────────────┐
 *   │  "Nodes" + one-sentence summary            │
 *   └────────────────────────────────────────────┘
 *   [ StatChip × 4: Total · Online · Degraded · Offline ]
 *   [ FilterChipGroup: all · <caps>… ]
 *   ┌── topology (glass strong) ──┬── side rail (glass soft) ──┐
 *   │  gateway + orbit rings       │  inner/outer runner entries │
 *   │  satellites (click → select) │  click → select              │
 *   └──────────────────────────────┴──────────────────────────────┘
 *   [ DetailDrawer beneath when a node is selected ]
 *
 * Data flow is unchanged from B4-FE2: React Query polls `fetchRunnersMock`
 * at a 5s cadence until B4-BE3 lands the real `/wstool/runners` endpoint +
 * SSE stream.
 */
// TODO(B4-BE3): replace with real apiFetch<Runner[]>("/wstool/runners") + SSE.

export default function NodesPage() {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  const query = useQuery<Runner[]>({
    queryKey: ["nodes"],
    queryFn: fetchRunnersMock,
    refetchInterval: 5_000,
    retry: false,
  });

  const runners = React.useMemo(() => query.data ?? [], [query.data]);

  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [capabilityFilter, setCapabilityFilter] = React.useState<string | null>(
    null,
  );

  const selected = React.useMemo(
    () => runners.find((r) => r.id === selectedId) ?? null,
    [runners, selectedId],
  );

  // When a capability filter is active and the current selection is dimmed,
  // keep the drawer open — operators often want to inspect the "why it
  // doesn't match" case. We only drop the selection if the runner itself
  // disappears (e.g. the whole list reloaded).
  React.useEffect(() => {
    if (selectedId && !runners.some((r) => r.id === selectedId)) {
      setSelectedId(null);
    }
  }, [selectedId, runners]);

  const stats = React.useMemo(() => summariseRunners(runners), [runners]);

  // Degraded-prose surface: pick the most "recent" degraded runner — we use
  // `lastPingMs` as a coarse proxy for recency (shorter = more recent in the
  // mock; this matches how operators think about the viz).
  const recentDegraded = React.useMemo(() => {
    const degraded = runners.filter((r) => r.health === "degraded");
    if (degraded.length === 0) return null;
    degraded.sort((a, b) => a.lastPingMs - b.lastPingMs);
    const top = degraded[0]!;
    const caps = capabilityOf(top);
    return {
      host: top.hostname,
      agoSec: Math.max(1, Math.round(top.lastPingMs / 1000)),
      capability: caps[0] ?? null,
    };
  }, [runners]);

  const degradedCount = runners.filter((r) => r.health === "degraded").length;

  const capabilityCount = React.useMemo(
    () => capabilityCounts(runners).size,
    [runners],
  );

  const onSelect = React.useCallback((runner: Runner | null) => {
    if (runner === null) {
      setSelectedId(null);
      return;
    }
    setSelectedId((prev) => (prev === runner.id ? null : runner.id));
  }, []);

  const live = !query.isError;
  const listIsEmpty = !query.isPending && !query.isError && runners.length === 0;

  return (
    <motion.div
      className="flex flex-col gap-5"
      initial={reduced ? undefined : { opacity: 0, y: 6 }}
      animate={reduced ? undefined : { opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
    >
      <PageHero
        onlineCount={stats.connected}
        capabilityCount={capabilityCount}
        degradedCount={degradedCount}
        offlineCount={stats.disconnected}
        total={runners.length}
        recentDegradedHost={recentDegraded?.host ?? null}
        recentDegradedAgoSec={recentDegraded?.agoSec ?? null}
        recentDegradedCapability={recentDegraded?.capability ?? null}
      />

      <StatsRow
        total={runners.length}
        online={stats.connected}
        degraded={degradedCount}
        offline={stats.disconnected}
        avgLatencyMs={stats.avgLatencyMs}
        live={live}
      />

      {runners.length > 0 ? (
        <CapabilityFilter
          runners={runners}
          value={capabilityFilter}
          onChange={setCapabilityFilter}
        />
      ) : null}

      {query.isPending ? (
        <TopologySkeleton />
      ) : query.isError ? (
        <OfflineBlock message={(query.error as Error | undefined)?.message} />
      ) : listIsEmpty ? (
        <EmptyBlock />
      ) : (
        <>
          <section className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
            <GlassPanel
              variant="strong"
              className="relative overflow-hidden p-4"
              data-testid="nodes-viz-panel"
            >
              {/* Warm aurora glow behind the topology — reinforces the
                  amber+ember dialect without competing with node colour.
                  Pointer-events: none so it never swallows clicks. */}
              <div
                aria-hidden
                className="pointer-events-none absolute inset-0"
                style={{
                  background:
                    "radial-gradient(ellipse at center, var(--tp-amber-glow), transparent 60%)",
                  opacity: 0.55,
                }}
              />
              <div className="relative">
                <header className="mb-2 flex items-center justify-between px-1">
                  <div className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-tp-ink-4">
                    {t("nodes.tp.vizTitle")}
                  </div>
                  <div className="font-mono text-[10.5px] tabular-nums text-tp-ink-3">
                    {t("nodes.tp.vizMeta", {
                      online: stats.connected,
                      total: runners.length,
                    })}
                  </div>
                </header>
                <div className="relative h-[480px] w-full">
                  <TopologyGraph
                    runners={runners}
                    selectedId={selectedId}
                    onSelect={onSelect}
                    capabilityFilter={capabilityFilter}
                    className="absolute inset-0 flex items-center justify-center [&>svg]:max-h-full"
                  />
                </div>
              </div>
            </GlassPanel>

            <NodeSideRail
              runners={runners}
              selectedId={selectedId}
              onSelect={onSelect}
              capabilityFilter={capabilityFilter}
              className="max-h-[520px] lg:sticky lg:top-4"
            />
          </section>

          {selected ? (
            <NodeDetailDrawer
              runner={selected}
              onReconnect={undefined}
              className="mt-1"
            />
          ) : null}
        </>
      )}

      {/* Screen-reader / no-JS fallback: a plain data table summarising every
          runner. Visually hidden via `sr-only`, but present in the DOM so
          assistive tech can enumerate the topology without parsing SVG. */}
      <details className="sr-only">
        <summary>{t("nodes.tp.a11yTableSummary")}</summary>
        <table>
          <thead>
            <tr>
              <th>{t("nodes.tp.a11yColName")}</th>
              <th>{t("nodes.tp.a11yColRing")}</th>
              <th>{t("nodes.tp.a11yColHealth")}</th>
              <th>{t("nodes.tp.a11yColLatency")}</th>
              <th>{t("nodes.tp.a11yColTools")}</th>
            </tr>
          </thead>
          <tbody>
            {runners.map((r) => (
              <tr key={r.id} data-testid={`runner-row-${r.id}`}>
                <td>{r.hostname}</td>
                <td>{r.ring === 0 ? "inner" : "outer"}</td>
                <td>{r.health}</td>
                <td>{r.latencyMs}ms</td>
                <td>{r.toolCount}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </motion.div>
  );
}

function TopologySkeleton() {
  return (
    <section className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div
        className="h-[520px] animate-pulse rounded-2xl border border-tp-glass-edge bg-tp-glass-inner/70"
        data-testid="nodes-viz-skeleton"
      />
      <div className="h-[520px] animate-pulse rounded-2xl border border-tp-glass-edge bg-tp-glass-inner/70" />
    </section>
  );
}
