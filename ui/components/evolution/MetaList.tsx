"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Sparkles } from "lucide-react";

import { GlassPanel } from "@/components/ui/glass-panel";
import { cn } from "@/lib/utils";
import {
  applyEvolutionProposal,
  fetchEvolutionApproved,
  fetchEvolutionPending,
  type EvolutionProposal,
} from "@/lib/api";

import { isMetaKind, META_KINDS } from "./types";
import { MetaReviewDialog } from "./MetaReviewDialog";

/**
 * Meta tab — Phase 4 W2 B1 iter 6+7.
 *
 * Filters the same `pending` and `approved` queries as the regular
 * tabs but keeps only the rows whose `kind` is one of the four meta
 * `EvolutionKind`s (`engine_config`, `engine_prompt`, `observer_filter`,
 * `cluster_threshold`). Visual treatment is amber/orange-leaning and
 * carries a "self-improvement" badge so operators don't conflate meta
 * with regular memory_op / agent_prompt rewrites.
 *
 * Each row's "Review" button opens `<MetaReviewDialog />` which carries
 * the per-kind diff renderer plus the kind-aware double-confirm /
 * generic-confirm Apply flow.
 */

const PENDING_KEY = ["admin", "evolution", "pending"] as const;
const APPROVED_KEY = ["admin", "evolution", "approved"] as const;
const POLL_MS = 5_000;

export function MetaList() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const pendingQuery = useQuery<EvolutionProposal[]>({
    queryKey: PENDING_KEY,
    queryFn: fetchEvolutionPending,
    refetchInterval: POLL_MS,
    retry: false,
  });

  const approvedQuery = useQuery<EvolutionProposal[]>({
    queryKey: APPROVED_KEY,
    queryFn: fetchEvolutionApproved,
    refetchInterval: POLL_MS,
    retry: false,
  });

  const rows = React.useMemo<EvolutionProposal[]>(() => {
    const out: EvolutionProposal[] = [];
    const seen = new Set<string>();
    const merge = (data: EvolutionProposal[] | undefined) => {
      if (!data) return;
      for (const p of data) {
        if (!isMetaKind(p.kind)) continue;
        if (seen.has(p.id)) continue;
        seen.add(p.id);
        out.push(p);
      }
    };
    merge(pendingQuery.data);
    merge(approvedQuery.data);
    return out.sort((a, b) => b.created_at - a.created_at);
  }, [pendingQuery.data, approvedQuery.data]);

  const [openId, setOpenId] = React.useState<string | null>(null);
  const openProposal = React.useMemo(
    () => rows.find((r) => r.id === openId) ?? null,
    [rows, openId],
  );

  const applyMutation = useMutation({
    mutationFn: ({ id }: { id: string }) => applyEvolutionProposal(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PENDING_KEY });
      void qc.invalidateQueries({ queryKey: APPROVED_KEY });
    },
  });

  // mutateAsync surfaces the 403 envelope as a thrown CorlinmanApiError
  // so MetaReviewDialog can route it to inline help.
  const handleApply = React.useCallback(
    async (id: string) => {
      await applyMutation.mutateAsync({ id });
    },
    [applyMutation],
  );

  const isPending = pendingQuery.isPending && approvedQuery.isPending;

  return (
    <section className="flex flex-col gap-3.5">
      <MetaTabBanner />

      {isPending ? (
        <ListSkeleton />
      ) : rows.length === 0 ? (
        <MetaEmpty />
      ) : (
        <div data-testid="meta-rows" className="flex flex-col gap-3">
          {rows.map((proposal) => (
            <MetaRow
              key={proposal.id}
              proposal={proposal}
              onReview={() => setOpenId(proposal.id)}
              t={t}
            />
          ))}
        </div>
      )}

      <MetaReviewDialog
        open={openId !== null && openProposal !== null}
        proposal={openProposal}
        onOpenChange={(next) => {
          if (!next) setOpenId(null);
        }}
        onApply={handleApply}
      />
    </section>
  );
}

function MetaTabBanner() {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-xl border px-3 py-2.5",
        "border-tp-amber/30 bg-tp-amber-soft text-tp-ink",
      )}
    >
      <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-tp-amber" aria-hidden />
      <div className="flex flex-col gap-0.5">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-amber">
          {t("evolution.tp.metaSelfImprovement")}
        </span>
        <span className="text-[12px] leading-[1.55] text-tp-ink-2">
          {t("evolution.tp.metaTabDesc")}
        </span>
      </div>
    </div>
  );
}

interface MetaRowProps {
  proposal: EvolutionProposal;
  onReview: () => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
}

function MetaRow({ proposal, onReview, t }: MetaRowProps) {
  const headingId = React.useId();
  return (
    <GlassPanel
      as="article"
      variant="soft"
      rounded="rounded-2xl"
      aria-labelledby={headingId}
      className={cn(
        "p-4 transition-all duration-200",
        // Amber/orange accent — left border for the meta visual treatment.
        "border-l-[3px] border-l-tp-amber/70",
        "hover:-translate-y-[1px] hover:shadow-tp-hero hover:border-tp-amber/40",
      )}
      data-meta-kind={proposal.kind}
    >
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-full border px-2 py-[2px]",
                "border-tp-amber/40 bg-tp-amber-soft text-tp-amber",
                "font-mono text-[10.5px] uppercase tracking-[0.08em]",
              )}
            >
              {t("evolution.tp.metaSelfImprovement")}
            </span>
            <KindBadge kind={proposal.kind} />
            <StatusBadge status={proposal.status} t={t} />
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
        </div>

        <div className="flex shrink-0 items-center">
          <button
            type="button"
            onClick={onReview}
            aria-label={t("evolution.tp.metaReview")}
            className={cn(
              "rounded-full border px-3.5 py-1 text-[12px] font-medium",
              "border-tp-amber/40 bg-tp-amber-soft text-tp-amber",
              "hover:-translate-y-[1px] active:translate-y-0",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/55",
              "transition-transform duration-150",
            )}
          >
            {t("evolution.tp.metaReview")}
          </button>
        </div>
      </div>
    </GlassPanel>
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

function StatusBadge({
  status,
  t,
}: {
  status: string;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  if (status === "approved") {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full border px-2 py-[2px]",
          "border-tp-ok/40 bg-tp-ok-soft text-tp-ok",
          "font-mono text-[10.5px] uppercase tracking-[0.08em]",
        )}
      >
        {t("evolution.tp.statusApproved")}
      </span>
    );
  }
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-[2px]",
        "border-tp-glass-edge bg-tp-glass-inner-strong text-tp-ink-3",
        "font-mono text-[10.5px] uppercase tracking-[0.08em]",
      )}
    >
      {status}
    </span>
  );
}

function MetaEmpty() {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 py-12 text-center",
        "rounded-2xl border border-dashed border-tp-amber/30 bg-tp-amber-soft/40",
      )}
    >
      <Sparkles className="h-7 w-7 text-tp-amber/80" aria-hidden />
      <p className="text-[13px] leading-[1.55] text-tp-ink-2">
        {t("evolution.tp.emptyMeta")}
      </p>
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      {Array.from({ length: 2 }).map((_, i) => (
        <div
          key={i}
          className={cn(
            "h-[88px] animate-pulse rounded-2xl border border-tp-glass-edge",
            "bg-tp-glass-inner/70",
          )}
        />
      ))}
    </div>
  );
}

// Re-export META_KINDS for callers that only import from this barrel.
export { META_KINDS };
export default MetaList;
