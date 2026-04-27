"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import { Check } from "lucide-react";

import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import { cn } from "@/lib/utils";
import {
  applyEvolutionProposal,
  fetchEvolutionApproved,
  type EvolutionProposal,
  type MetricSnapshot,
} from "@/lib/api";

import { MetricsDelta } from "./MetricsDelta";
import { EvolutionEmptyState } from "./EmptyState";

/**
 * Approved tab — proposals that have been decided "approved" but not yet
 * applied. The Apply button POSTs `/admin/evolution/:id/apply` and
 * spring-animates the row out, mirroring the Pending tab's UX.
 *
 * Polling is slower than Pending (5s vs 3s) — Approved is review surface,
 * not a hot queue.
 */

const QUERY_KEY = ["admin", "evolution", "approved"] as const;
const POLL_MS = 5_000;
const APPLY_DEPART_MS = 420;

export function ApprovedList() {
  const { t } = useTranslation();
  const { reduced } = useMotion();
  const qc = useQueryClient();

  const query = useQuery<EvolutionProposal[]>({
    queryKey: QUERY_KEY,
    queryFn: fetchEvolutionApproved,
    refetchInterval: POLL_MS,
    retry: false,
  });

  const [departingIds, setDepartingIds] = React.useState<Set<string>>(
    () => new Set(),
  );

  const removeFromCache = (id: string) => {
    qc.setQueryData<EvolutionProposal[]>(QUERY_KEY, (prev) =>
      prev ? prev.filter((p) => p.id !== id) : prev,
    );
  };

  const markDeparting = (id: string) => {
    setDepartingIds((prev) => {
      const n = new Set(prev);
      n.add(id);
      return n;
    });
    window.setTimeout(() => {
      removeFromCache(id);
      setDepartingIds((prev) => {
        if (!prev.has(id)) return prev;
        const n = new Set(prev);
        n.delete(id);
        return n;
      });
    }, APPLY_DEPART_MS);
  };

  const applyMutation = useMutation({
    mutationFn: ({ id }: { id: string }) => applyEvolutionProposal(id),
    onSuccess: (_data, vars) => markDeparting(vars.id),
  });

  const rows = query.data ?? [];

  if (query.isPending) {
    return <ListSkeleton />;
  }

  if (rows.length === 0) {
    return <EvolutionEmptyState tab="approved" />;
  }

  return (
    <section className="flex flex-col gap-3.5">
      <AnimatePresence initial={false}>
        {rows.map((proposal) => (
          <motion.div
            key={proposal.id}
            layout={reduced ? false : "position"}
            initial={reduced ? false : { opacity: 0, y: 8 }}
            animate={
              departingIds.has(proposal.id)
                ? { opacity: 0, scale: 0.97, transition: { duration: 0.4 } }
                : { opacity: 1, scale: 1 }
            }
          >
            <ApprovedCard
              proposal={proposal}
              disabled={
                applyMutation.isPending || departingIds.has(proposal.id)
              }
              onApply={(id) => applyMutation.mutate({ id })}
              t={t}
            />
          </motion.div>
        ))}
      </AnimatePresence>
    </section>
  );
}

interface ApprovedCardProps {
  proposal: EvolutionProposal;
  disabled: boolean;
  onApply: (id: string) => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
}

function ApprovedCard({ proposal, disabled, onApply, t }: ApprovedCardProps) {
  const headingId = React.useId();
  const baseline = extractBaselineCounts(proposal.baseline_metrics_json);
  const current = extractCurrentCounts(proposal.shadow_metrics);
  const showDelta = baseline !== null && current !== null;

  return (
    <GlassPanel
      as="article"
      variant="soft"
      rounded="rounded-2xl"
      aria-labelledby={headingId}
      className={cn(
        "p-5 transition-all duration-200",
        "hover:-translate-y-[1px] hover:shadow-tp-hero hover:border-tp-amber/30",
      )}
    >
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <KindBadge kind={proposal.kind} />
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-full border px-2 py-[2px]",
                "border-tp-ok/40 bg-tp-ok-soft text-tp-ok",
                "font-mono text-[10.5px] uppercase tracking-[0.08em]",
              )}
            >
              {t("evolution.tp.statusApproved")}
            </span>
            <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
              #{proposal.id}
            </span>
          </div>
          <h2
            id={headingId}
            className="font-mono text-[12.5px] leading-tight text-tp-ink"
          >
            <span className="text-tp-ink-3">
              {t("evolution.tp.cardTargetLabel")} ·{" "}
            </span>
            <span className="break-all">{proposal.target}</span>
          </h2>
          {proposal.decided_by ? (
            <div className="text-[11px] text-tp-ink-3">
              {t("evolution.tp.approvedBy", {
                who: proposal.decided_by,
                when: formatRelative(proposal.decided_at, t),
              })}
            </div>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => onApply(proposal.id)}
            disabled={disabled}
            aria-label={t("evolution.tp.apply")}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full px-3.5 py-1 text-[12px] font-medium",
              "bg-tp-amber text-[#1a120d] shadow-tp-primary",
              "transition-transform duration-150 hover:-translate-y-[1px] active:translate-y-0",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/55",
              "disabled:pointer-events-none disabled:opacity-50",
            )}
          >
            <Check aria-hidden className="h-3.5 w-3.5" />
            {t("evolution.tp.apply")}
          </button>
        </div>
      </div>

      <div className="mt-4 border-t border-tp-glass-edge pt-3">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
          {t("evolution.tp.cardReasoningLabel")}
        </div>
        <p className="mt-1.5 text-[13px] leading-[1.7] text-tp-ink-2">
          {proposal.reasoning}
        </p>
      </div>

      {showDelta ? (
        <div className="mt-3">
          <MetricsDelta
            baseline={baseline}
            current={current}
            variant="compact"
            label={t("evolution.tp.shadowVsBaseline")}
          />
        </div>
      ) : null}
    </GlassPanel>
  );
}

/** Extract numeric `event_kind → count` from a `MetricSnapshot.counts`
 * object. Returns null when the input is missing — caller decides
 * whether to render the chart. */
function extractBaselineCounts(
  snapshot: MetricSnapshot | undefined,
): Record<string, number> | null {
  if (!snapshot || !snapshot.counts) return null;
  return snapshot.counts;
}

/** `shadow_metrics` is free-form per kind; harvest only the numeric
 * scalars so the delta chart doesn't choke on `success_rate` floats or
 * nested objects. */
function extractCurrentCounts(
  shadow: Record<string, unknown> | undefined,
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
  ts: number | undefined,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  if (!ts) return "";
  const ageMs = Math.max(0, Date.now() - ts);
  const min = Math.floor(ageMs / 60_000);
  if (min < 1) return t("evolution.tp.justNow");
  if (min < 60) return t("evolution.tp.cardAgoMin", { m: min });
  const hr = Math.floor(min / 60);
  return t("evolution.tp.cardAgoHr", { h: hr });
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

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-3.5">
      {Array.from({ length: 2 }).map((_, i) => (
        <div
          key={i}
          className={cn(
            "h-[120px] animate-pulse rounded-2xl border border-tp-glass-edge",
            "bg-tp-glass-inner/70",
          )}
        />
      ))}
    </div>
  );
}

export default ApprovedList;
