"use client";

import { useTranslation } from "react-i18next";
import { StatChip } from "@/components/ui/stat-chip";

/**
 * Four-stat row for the evolution page.
 *
 * Phase 2 only the first chip ("Pending") is live — the others surface "—"
 * with "Phase 3" footnotes so the layout stays honest about missing data.
 *
 * Sparkline geometries are baked in to keep the visual dialect consistent
 * with the rest of the admin shell (`/dashboard`, `/approvals`).
 */
const PENDING_SPARK =
  "M0 26 L30 24 L60 22 L90 24 L120 18 L150 20 L180 14 L210 16 L240 12 L270 16 L300 10 L300 36 L0 36 Z";
const AUTO_SPARK =
  "M0 30 L30 28 L60 26 L90 24 L120 24 L150 22 L180 22 L210 20 L240 20 L270 18 L300 18 L300 36 L0 36 Z";
const AVG_SPARK =
  "M0 18 L30 22 L60 18 L90 22 L120 14 L150 22 L180 16 L210 22 L240 14 L270 22 L300 14 L300 36 L0 36 Z";
const BUDGET_SPARK =
  "M0 28 L30 26 L60 22 L90 18 L120 18 L150 14 L180 14 L210 12 L240 12 L270 8 L300 8 L300 36 L0 36 Z";

export interface StatsRowProps {
  pendingCount: number;
  pendingLive: boolean;
  budgetUsed: number;
  budgetTotal: number;
}

export function StatsRow({
  pendingCount,
  pendingLive,
  budgetUsed,
  budgetTotal,
}: StatsRowProps) {
  const { t } = useTranslation();
  const phase3 = t("evolution.tp.statAutoPending");
  const budgetPct =
    budgetTotal === 0 ? 0 : Math.round((budgetUsed / budgetTotal) * 100);

  return (
    <section className="grid grid-cols-2 gap-3.5 md:grid-cols-4">
      <StatChip
        variant="primary"
        live={pendingLive}
        label={t("evolution.tp.statPending")}
        value={pendingLive ? pendingCount : "—"}
        foot={
          pendingLive
            ? pendingCount > 0
              ? t("evolution.tp.statPendingFoot")
              : t("evolution.tp.statCaughtUp")
            : phase3
        }
        sparkPath={PENDING_SPARK}
        sparkTone="amber"
      />
      <StatChip
        label={t("evolution.tp.statAuto24h")}
        value="—"
        foot={t("evolution.tp.statAutoFoot")}
        sparkPath={AUTO_SPARK}
        sparkTone="peach"
      />
      <StatChip
        label={t("evolution.tp.statAvgDecide")}
        value="—"
        foot={t("evolution.tp.statAvgFoot")}
        sparkPath={AVG_SPARK}
        sparkTone="ember"
      />
      <StatChip
        label={t("evolution.tp.statBudget")}
        value={
          budgetTotal > 0
            ? t("evolution.tp.statBudgetUsed", {
                used: budgetUsed,
                total: budgetTotal,
              })
            : "—"
        }
        foot={
          budgetTotal > 0
            ? t("evolution.tp.statBudgetFoot", { pct: budgetPct })
            : t("evolution.tp.statBudgetEmpty")
        }
        sparkPath={BUDGET_SPARK}
        sparkTone="amber"
      />
    </section>
  );
}
