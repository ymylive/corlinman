"use client";

import * as React from "react";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";

import { FilterChipGroup } from "@/components/ui/filter-chip-group";
import { LiveRegion, useMotion } from "@/components/ui/motion-safe";
import { cn } from "@/lib/utils";
import {
  approveEvolutionProposal,
  denyEvolutionProposal,
  fetchEvolutionPending,
  type EvolutionProposal,
} from "@/lib/api";

import { EvolutionPageHeader } from "@/components/evolution/PageHeader";
import { StatsRow } from "@/components/evolution/StatsRow";
import { ProposalCard } from "@/components/evolution/ProposalCard";
import { EvolutionEmptyState } from "@/components/evolution/EmptyState";
import type { Tab } from "@/components/evolution/types";

/**
 * /evolution — Wave 1-D Phase 2 MVP.
 *
 * Layout:
 *   [ hero (GlassPanel strong, serif title) ]
 *   [ StatChip × 4 ]
 *   [ FilterChipGroup: Pending(N) · Approved · History ]
 *   [ FilterChipGroup: kind row — All · memory_op · ... ]
 *   [ ProposalCard list  |  Approved/History placeholder ]
 *
 * Data:
 *   - React Query polls `/admin/evolution?status=pending&limit=50` every 3s.
 *   - approve / deny use useMutation with optimistic spring-out: the card
 *     fades+scales out for 400ms, then the cache drops it.
 *   - Approved / History tabs render structural placeholders only — Phase 3
 *     wires the real data.
 *
 * Accessibility:
 *   - All buttons carry aria-labels (Approve / Deny / Show detail / etc).
 *   - sr-only LiveRegion announces approve / deny outcomes for screen
 *     readers.
 *   - Mobile: the proposal column is single-column at <md by default; the
 *     stat chips reflow to 2× on small screens.
 */
export default function EvolutionPage() {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  const [tab, setTab] = useState<Tab>("pending");
  const [kindFilter, setKindFilter] = useState<string>("__all__");
  const [now, setNow] = useState<number>(() => Date.now());
  const [departingIds, setDepartingIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [srMessage, setSrMessage] = useState<string | null>(null);

  // Coarse 1s tick for the "Xs ago" labels + held-for prose.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  // ─── pending query (3s polling) ───────────────────────────────────────
  const qc = useQueryClient();
  const queryKey = useMemo(() => ["admin", "evolution", "pending"], []);
  const query = useQuery<EvolutionProposal[]>({
    queryKey,
    queryFn: fetchEvolutionPending,
    refetchInterval: 3_000,
    retry: false,
  });

  const pendingRows = useMemo(() => query.data ?? [], [query.data]);
  const pendingLive = !query.isError;

  // Distinct kinds for the secondary filter row.
  const kinds = useMemo(() => {
    const set = new Set<string>();
    for (const p of pendingRows) set.add(p.kind);
    return Array.from(set).sort();
  }, [pendingRows]);

  const visibleRows = useMemo(() => {
    if (tab !== "pending") return [];
    if (kindFilter === "__all__") return pendingRows;
    return pendingRows.filter((p) => p.kind === kindFilter);
  }, [tab, kindFilter, pendingRows]);

  const oldestHeldMs = useMemo(() => {
    let oldest: number | null = null;
    for (const p of pendingRows) {
      const held = now - p.created_at;
      if (oldest === null || held > oldest) oldest = held;
    }
    return oldest;
  }, [pendingRows, now]);

  // ─── mutations ────────────────────────────────────────────────────────
  const removeFromCache = (id: string) => {
    qc.setQueryData<EvolutionProposal[]>(queryKey, (prev) =>
      prev ? prev.filter((p) => p.id !== id) : prev,
    );
  };

  const markDeparting = (id: string) => {
    setDepartingIds((prev) => {
      const n = new Set(prev);
      n.add(id);
      return n;
    });
    // After the spring-out finishes, drop the card from cache.
    window.setTimeout(() => {
      removeFromCache(id);
      setDepartingIds((prev) => {
        if (!prev.has(id)) return prev;
        const n = new Set(prev);
        n.delete(id);
        return n;
      });
    }, 420);
  };

  const approveMutation = useMutation({
    mutationFn: ({ id }: { id: string }) =>
      approveEvolutionProposal(id, t("evolution.tp.decidedBy")),
    onSuccess: (_data, vars) => {
      markDeparting(vars.id);
      setSrMessage(t("evolution.tp.srApproved", { id: vars.id }));
      setErrorBanner(null);
    },
    onError: (err, vars) => {
      void vars;
      setErrorBanner(
        t("evolution.tp.approveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const denyMutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
      denyEvolutionProposal(id, t("evolution.tp.decidedBy"), reason),
    onSuccess: (_data, vars) => {
      markDeparting(vars.id);
      setSrMessage(t("evolution.tp.srDenied", { id: vars.id }));
      setErrorBanner(null);
    },
    onError: (err) => {
      setErrorBanner(
        t("evolution.tp.denyFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const anyMutating = approveMutation.isPending || denyMutation.isPending;

  // ─── filter options ───────────────────────────────────────────────────
  const tabOptions = useMemo(
    () => [
      {
        value: "pending",
        label: t("evolution.tp.filterPending"),
        count: pendingRows.length,
        tone:
          pendingRows.length > 0 ? ("warn" as const) : ("neutral" as const),
      },
      {
        value: "approved",
        label: t("evolution.tp.filterApproved"),
        tone: "neutral" as const,
      },
      {
        value: "history",
        label: t("evolution.tp.filterHistory"),
        tone: "neutral" as const,
      },
    ],
    [pendingRows.length, t],
  );

  const kindOptions = useMemo(
    () => [
      {
        value: "__all__",
        label: t("evolution.tp.kindAll"),
        tone: "neutral" as const,
      },
      ...kinds.map((k) => ({
        value: k,
        label: k,
        tone: "neutral" as const,
      })),
    ],
    [kinds, t],
  );

  // Phase 2: the budget endpoint isn't shipped yet — total stays 0 and the
  // chip surfaces "live in Phase 3" copy.
  const budgetTotal = 0;
  const budgetUsed = 0;

  const refresh = () => {
    void qc.invalidateQueries({ queryKey });
  };

  const listIsEmpty =
    !query.isPending && !query.isError && visibleRows.length === 0;

  return (
    <motion.div
      className="flex flex-col gap-5"
      initial={reduced ? undefined : { opacity: 0, y: 6 }}
      animate={reduced ? undefined : { opacity: 1, y: 0 }}
      transition={{ duration: 0.32, ease: [0.16, 1, 0.3, 1] }}
    >
      <EvolutionPageHeader
        pendingCount={pendingRows.length}
        oldestHeldMs={oldestHeldMs}
        budgetTotal={budgetTotal}
        budgetUsed={budgetUsed}
        watching={pendingLive && !query.isPending}
        onRefresh={refresh}
        refreshing={query.isFetching}
      />

      <StatsRow
        pendingCount={pendingRows.length}
        pendingLive={pendingLive}
        budgetUsed={budgetUsed}
        budgetTotal={budgetTotal}
      />

      {errorBanner ? (
        <Banner
          tone="err"
          text={errorBanner}
          onDismiss={() => setErrorBanner(null)}
        />
      ) : null}
      {query.isError && !errorBanner ? (
        <Banner tone="info" text={t("evolution.tp.endpointOfflineBanner")} />
      ) : null}

      <div className="flex flex-col gap-2.5">
        <FilterChipGroup
          label={t("evolution.tp.title")}
          options={tabOptions}
          value={tab}
          onChange={(next) => setTab(next as Tab)}
        />
        {tab === "pending" && kindOptions.length > 1 ? (
          <FilterChipGroup
            label={t("evolution.tp.kindAll")}
            options={kindOptions}
            value={kindFilter}
            onChange={(next) => setKindFilter(next)}
          />
        ) : null}
      </div>

      {tab === "pending" ? (
        <section className="flex flex-col gap-3.5">
          {query.isPending ? (
            <ListSkeleton />
          ) : listIsEmpty ? (
            kindFilter !== "__all__" && pendingRows.length > 0 ? (
              <FilteredEmpty />
            ) : (
              <EvolutionEmptyState tab="pending" />
            )
          ) : (
            <AnimatePresence initial={false}>
              {visibleRows.map((proposal) => (
                <ProposalCard
                  key={proposal.id}
                  proposal={proposal}
                  now={now}
                  isDeparting={departingIds.has(proposal.id)}
                  disabled={anyMutating || departingIds.has(proposal.id)}
                  onApprove={(id) => approveMutation.mutate({ id })}
                  onDeny={(id, reason) => denyMutation.mutate({ id, reason })}
                />
              ))}
            </AnimatePresence>
          )}
        </section>
      ) : (
        <EvolutionEmptyState tab={tab} />
      )}

      <LiveRegion politeness="polite">{srMessage}</LiveRegion>
    </motion.div>
  );
}

// ─── atomic pieces ────────────────────────────────────────────────────────

function FilteredEmpty() {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        "rounded-2xl border border-dashed border-tp-glass-edge bg-tp-glass-inner/40",
        "px-6 py-10 text-center text-[12.5px] text-tp-ink-3",
      )}
    >
      {t("evolution.tp.emptyFiltered")}
    </div>
  );
}

function Banner({
  tone,
  text,
  onDismiss,
}: {
  tone: "err" | "info" | "warn";
  text: string;
  onDismiss?: () => void;
}) {
  const { t } = useTranslation();
  const cls = {
    warn: "border-tp-warn/30 bg-tp-warn-soft text-tp-warn",
    err: "border-tp-err/40 bg-tp-err-soft text-tp-err",
    info: "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-3",
  }[tone];
  return (
    <div
      role="alert"
      className={cn(
        "flex items-center justify-between gap-3 rounded-xl border px-3 py-2 text-[12.5px]",
        cls,
      )}
    >
      <span>{text}</span>
      {onDismiss ? (
        <button
          type="button"
          onClick={onDismiss}
          aria-label={t("common.close")}
          className={cn(
            "rounded-md px-2 py-1 text-[11px] font-medium",
            "bg-transparent hover:bg-tp-glass-inner-hover",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
        >
          {t("common.close")}
        </button>
      ) : null}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-3.5">
      {Array.from({ length: 3 }).map((_, i) => (
        <div
          key={i}
          className={cn(
            "h-[150px] animate-pulse rounded-2xl border border-tp-glass-edge",
            "bg-tp-glass-inner/70",
          )}
        />
      ))}
    </div>
  );
}
