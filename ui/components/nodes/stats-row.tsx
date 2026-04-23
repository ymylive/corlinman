"use client";

import { useTranslation } from "react-i18next";
import { StatChip } from "@/components/ui/stat-chip";

/**
 * Four-stat row for the Nodes page: Total · Online · Degraded · Offline.
 *
 * Sparkline paths reuse the same geometry as the dashboard — keeps the
 * visual dialect consistent across pages without a deps hop.
 *
 * `live={true}` on the primary chip when the query is reachable; when offline
 * every chip falls back to "—" with an "endpoint pending" foot, mirroring the
 * Approvals page pattern.
 */

const TOTAL_SPARK =
  "M0 22 L30 20 L60 22 L90 18 L120 20 L150 16 L180 18 L210 16 L240 14 L270 18 L300 14 L300 36 L0 36 Z";
const ONLINE_SPARK =
  "M0 26 L30 22 L60 20 L90 18 L120 18 L150 14 L180 16 L210 12 L240 14 L270 10 L300 12 L300 36 L0 36 Z";
const DEGRADED_SPARK =
  "M0 30 L30 28 L60 26 L90 24 L120 20 L150 22 L180 18 L210 20 L240 22 L270 18 L300 20 L300 36 L0 36 Z";
const OFFLINE_SPARK =
  "M0 32 L30 32 L60 30 L90 32 L120 30 L150 32 L180 30 L210 32 L240 30 L270 32 L300 30 L300 36 L0 36 Z";

export interface StatsRowProps {
  total: number;
  online: number;
  degraded: number;
  offline: number;
  avgLatencyMs: number;
  /** `true` when the runners endpoint is reachable — drives "live" flag. */
  live: boolean;
}

export function StatsRow({
  total,
  online,
  degraded,
  offline,
  avgLatencyMs,
  live,
}: StatsRowProps) {
  const { t } = useTranslation();
  const endpointPending = t("nodes.tp.statEndpointPending");

  const onlineFoot = live
    ? online === total && total > 0
      ? t("nodes.tp.statAllOnline")
      : t("nodes.tp.statAvgLatency", { ms: avgLatencyMs })
    : endpointPending;

  const degradedFoot = live
    ? degraded > 0
      ? t("nodes.tp.statDegradedFoot")
      : t("nodes.tp.statNoDegraded")
    : endpointPending;

  const offlineFoot = live
    ? offline > 0
      ? t("nodes.tp.statOfflineFoot")
      : t("nodes.tp.statNoOffline")
    : endpointPending;

  return (
    <section className="grid grid-cols-2 gap-3.5 md:grid-cols-4">
      <StatChip
        variant="primary"
        live={live}
        label={t("nodes.tp.statTotal")}
        value={live ? total : "—"}
        foot={live ? t("nodes.tp.statTotalFoot") : endpointPending}
        sparkPath={TOTAL_SPARK}
        sparkTone="amber"
        data-testid="nodes-stat-total"
      />
      <StatChip
        label={t("nodes.tp.statOnline")}
        value={live ? online : "—"}
        delta={
          live && total > 0
            ? {
                label: `${online}/${total}`,
                tone: online === total ? "up" : "flat",
              }
            : undefined
        }
        foot={onlineFoot}
        sparkPath={ONLINE_SPARK}
        sparkTone="peach"
        data-testid="nodes-stat-online"
      />
      <StatChip
        label={t("nodes.tp.statDegraded")}
        value={live ? degraded : "—"}
        foot={degradedFoot}
        sparkPath={DEGRADED_SPARK}
        sparkTone="ember"
        data-testid="nodes-stat-degraded"
      />
      <StatChip
        label={t("nodes.tp.statOffline")}
        value={live ? offline : "—"}
        foot={offlineFoot}
        sparkPath={OFFLINE_SPARK}
        sparkTone="ember"
        data-testid="nodes-stat-offline"
      />
    </section>
  );
}

export default StatsRow;
