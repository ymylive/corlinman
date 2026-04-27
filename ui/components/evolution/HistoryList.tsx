"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronUp } from "lucide-react";

import { GlassPanel } from "@/components/ui/glass-panel";
import { cn } from "@/lib/utils";
import {
  fetchEvolutionHistory,
  type HistoryEntry,
  type MetricSnapshot,
} from "@/lib/api";

import { MetricsDelta } from "./MetricsDelta";
import { EvolutionEmptyState } from "./EmptyState";

/**
 * History tab — terminal-state proposals (applied / rolled_back) with
 * the W1-B `metrics_baseline` snapshot, the optional W1-A shadow
 * metrics, and the audit-trail SHAs. Read-only; no mutations.
 *
 * Polling is slowest of the three tabs (10s) — these rows don't change
 * often once a proposal lands in the audit log.
 */

const QUERY_KEY = ["admin", "evolution", "history"] as const;
const POLL_MS = 10_000;

export function HistoryList() {
  const query = useQuery<HistoryEntry[]>({
    queryKey: QUERY_KEY,
    queryFn: fetchEvolutionHistory,
    refetchInterval: POLL_MS,
    retry: false,
  });

  if (query.isPending) {
    return <ListSkeleton />;
  }

  const rows = query.data ?? [];
  if (rows.length === 0) {
    return <EvolutionEmptyState tab="history" />;
  }

  return (
    <section className="flex flex-col gap-3.5">
      {rows.map((entry) => (
        <HistoryCard key={entry.proposal_id} entry={entry} />
      ))}
    </section>
  );
}

function HistoryCard({ entry }: { entry: HistoryEntry }) {
  const { t } = useTranslation();
  const headingId = React.useId();
  const [expanded, setExpanded] = React.useState(false);

  const isRolledBack = entry.status === "rolled_back";
  // Auto-rollback reason wins when present; falls back to the manual one.
  const rollbackReason =
    entry.auto_rollback_reason ?? entry.rollback_reason ?? null;

  const baselineCounts = extractCounts(entry.metrics_baseline);
  const shadowCounts = extractShadowCounts(entry.shadow_metrics);
  const showDelta = isRolledBack && baselineCounts && shadowCounts;

  return (
    <GlassPanel
      as="article"
      variant="soft"
      rounded="rounded-2xl"
      aria-labelledby={headingId}
      className={cn(
        "p-5 transition-all duration-200",
        "hover:-translate-y-[1px] hover:shadow-tp-hero",
        isRolledBack && "border-tp-err/25",
      )}
    >
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <KindBadge kind={entry.kind} />
            <StatusBadge status={entry.status} t={t} />
            <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
              #{entry.proposal_id}
            </span>
          </div>
          <h2
            id={headingId}
            className="font-mono text-[12.5px] leading-tight text-tp-ink"
          >
            <span className="text-tp-ink-3">
              {t("evolution.tp.cardTargetLabel")} ·{" "}
            </span>
            <span className="break-all">{entry.target}</span>
          </h2>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-tp-ink-3">
            <span>
              {t("evolution.tp.appliedAt", {
                when: formatRelative(entry.applied_at, t),
              })}
            </span>
            {entry.rolled_back_at ? (
              <>
                <span aria-hidden className="text-tp-ink-4">
                  ·
                </span>
                <span className="text-tp-err">
                  {t("evolution.tp.rolledBackAt", {
                    when: formatRelative(entry.rolled_back_at, t),
                  })}
                </span>
              </>
            ) : null}
          </div>
          {isRolledBack && rollbackReason ? (
            <div
              className={cn(
                "rounded-lg border px-3 py-2 text-[11.5px]",
                "border-tp-err/30 bg-tp-err-soft/40 text-tp-err",
              )}
            >
              <span className="font-mono text-[10.5px] uppercase tracking-[0.08em]">
                {entry.auto_rollback_reason
                  ? t("evolution.tp.autoRollbackReason")
                  : t("evolution.tp.manualRollbackReason")}
              </span>
              <div className="mt-1 text-[12px] text-tp-ink-2">
                {rollbackReason}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      {showDelta ? (
        <div className="mt-3">
          <MetricsDelta
            baseline={baselineCounts}
            current={shadowCounts}
            variant="compact"
            label={t("evolution.tp.regressionVsBaseline")}
          />
        </div>
      ) : null}

      <div className="mt-3 flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls={`${headingId}-detail`}
          className={cn(
            "inline-flex items-center gap-1 rounded-full px-2 py-1",
            "font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-3",
            "hover:bg-tp-glass-inner-hover hover:text-tp-ink-2",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
        >
          {expanded ? (
            <ChevronUp aria-hidden className="h-3.5 w-3.5" />
          ) : (
            <ChevronDown aria-hidden className="h-3.5 w-3.5" />
          )}
          {expanded
            ? t("evolution.tp.cardCollapse")
            : t("evolution.tp.historyExpand")}
        </button>
      </div>

      {expanded ? (
        <div
          id={`${headingId}-detail`}
          className="mt-3 flex flex-col gap-3 border-t border-tp-glass-edge pt-3"
        >
          <div>
            <div className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
              {t("evolution.tp.cardReasoningLabel")}
            </div>
            <p className="mt-1.5 text-[13px] leading-[1.7] text-tp-ink-2">
              {entry.reasoning}
            </p>
          </div>
          <ShaRow label={t("evolution.tp.beforeSha")} sha={entry.before_sha} />
          <ShaRow label={t("evolution.tp.afterSha")} sha={entry.after_sha} />
          {entry.eval_run_id ? (
            <div>
              <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
                {t("evolution.tp.evalRunId")}
              </span>
              <div className="mt-0.5 font-mono text-[11.5px] text-tp-ink-2">
                {entry.eval_run_id}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </GlassPanel>
  );
}

function StatusBadge({
  status,
  t,
}: {
  status: string;
  t: (key: string) => string;
}) {
  const isApplied = status === "applied";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-[2px]",
        "font-mono text-[10.5px] uppercase tracking-[0.08em]",
        isApplied
          ? "border-tp-ok/40 bg-tp-ok-soft text-tp-ok"
          : "border-tp-err/40 bg-tp-err-soft text-tp-err",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-[5px] w-[5px] rounded-full",
          isApplied ? "bg-tp-ok" : "bg-tp-err",
        )}
      />
      {isApplied
        ? t("evolution.tp.statusApplied")
        : t("evolution.tp.statusRolledBack")}
    </span>
  );
}

function ShaRow({ label, sha }: { label: string; sha: string }) {
  return (
    <div>
      <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
        {label}
      </span>
      <div className="mt-0.5 truncate font-mono text-[11.5px] text-tp-ink-2">
        {sha}
      </div>
    </div>
  );
}

function KindBadge({ kind }: { kind: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-[2px]",
        "border-tp-glass-edge bg-tp-glass-inner-strong",
        "font-mono text-[10.5px] tracking-wide text-tp-ink-2",
      )}
    >
      {kind}
    </span>
  );
}

function extractCounts(
  snapshot: MetricSnapshot | null | undefined,
): Record<string, number> | null {
  if (!snapshot || !snapshot.counts) return null;
  return snapshot.counts;
}

function extractShadowCounts(
  shadow: Record<string, unknown> | null | undefined,
): Record<string, number> | null {
  if (!shadow) return null;
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(shadow)) {
    if (typeof v === "number" && Number.isFinite(v) && Number.isInteger(v)) {
      out[k] = v;
    }
  }
  return Object.keys(out).length > 0 ? out : null;
}

function formatRelative(
  ts: number,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const ageMs = Math.max(0, Date.now() - ts);
  const min = Math.floor(ageMs / 60_000);
  if (min < 1) return t("evolution.tp.justNow");
  if (min < 60) return t("evolution.tp.cardAgoMin", { m: min });
  const hr = Math.floor(min / 60);
  if (hr < 24) return t("evolution.tp.cardAgoHr", { h: hr });
  const day = Math.floor(hr / 24);
  return t("evolution.tp.cardAgoDay", { d: day });
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-3.5">
      {Array.from({ length: 3 }).map((_, i) => (
        <div
          key={i}
          className={cn(
            "h-[110px] animate-pulse rounded-2xl border border-tp-glass-edge",
            "bg-tp-glass-inner/70",
          )}
        />
      ))}
    </div>
  );
}

export default HistoryList;
