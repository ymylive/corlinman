"use client";

import * as React from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import {
  apiFetch,
  decideApproval,
  decideApprovalsBatch,
  fetchApprovals,
  openEventStream,
  type Approval,
} from "@/lib/api";

import { ApprovalsEmptyState } from "@/components/approvals/EmptyState";
import { ArgsDialog } from "@/components/approvals/ArgsDialog";
import { DenyReasonDialog } from "@/components/approvals/DenyReasonDialog";
import { BatchToolbar } from "@/components/approvals/BatchToolbar";
import { FilterBar } from "@/components/approvals/FilterBar";
import { Checkbox } from "@/components/approvals/Checkbox";
import type { StreamEvent, Tab } from "@/components/approvals/types";

/**
 * Admin approvals page — Sprint 2 T3 wiring + Sprint 5 T4 polish.
 *
 * Dual-channel data model (unchanged from T3):
 *   1. React Query polls `GET /admin/approvals` (authoritative, 15s
 *      safety net).
 *   2. `EventSource` subscribes to `/admin/approvals/stream` and nudges
 *      the cache so pending/decided events reflect instantly.
 *
 * T4 additions:
 *   - Empty states, search + plugin filter, batch select/approve/deny.
 *   - Deny requires a reason (frontend-enforced; Rust already accepts it).
 *   - SSE `lag` named event surfaces as an inline banner.
 *   - SSE `pending` highlights the new row for ~1.2s; `decided` fades it
 *     out before it's removed (purely visual — the cache mutation is what
 *     actually removes it).
 *   - SSE reconnect uses exponential backoff in `lib/sse.ts`.
 *
 * TODO(S5+): approve-with-reason audit trail + a `sonner` toast host for
 * lag/error notifications. Virtual scroll (@tanstack/react-virtual) if we
 * ever see >500 pending approvals in steady state.
 */

// --- helpers ----------------------------------------------------------------

const ARGS_PREVIEW_LIMIT = 60;

function truncateArgs(raw: string): string {
  // Prefer a one-line JSON preview; fall back to raw bytes otherwise.
  try {
    const serialized = JSON.stringify(JSON.parse(raw));
    return serialized.length > ARGS_PREVIEW_LIMIT
      ? serialized.slice(0, ARGS_PREVIEW_LIMIT) + "…"
      : serialized;
  } catch {
    return raw.length > ARGS_PREVIEW_LIMIT
      ? raw.slice(0, ARGS_PREVIEW_LIMIT) + "…"
      : raw;
  }
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function DecisionBadge({ decision }: { decision: string | null }) {
  const { t } = useTranslation();
  if (!decision)
    return <Badge variant="secondary">{t("approvals.statusPending")}</Badge>;
  if (decision === "approved")
    return (
      <Badge className="border-transparent bg-emerald-600/20 text-emerald-300">
        {t("approvals.statusApproved")}
      </Badge>
    );
  if (decision === "denied")
    return (
      <Badge variant="destructive">{t("approvals.statusDenied")}</Badge>
    );
  return <Badge variant="outline">{decision}</Badge>;
}

// Keep `apiFetch` referenced so tree-shaking doesn't drop it — the rest of
// the admin surface still uses it and importing from `@/lib/api` here is
// load-bearing for the test suite.
void apiFetch;

// Visual highlight window for a freshly-pushed Pending row.
const HIGHLIGHT_MS = 1_200;
// Fade-out window for a row that was just decided.
const FADE_MS = 400;

// --- page -------------------------------------------------------------------

export default function ApprovalsPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("pending");
  const [search, setSearch] = useState("");
  const [pluginFilter, setPluginFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [denyDialog, setDenyDialog] = useState<
    | { kind: "single"; id: string }
    | { kind: "batch"; ids: string[] }
    | null
  >(null);
  // Rows that were just inserted via SSE Pending → get a pulse highlight.
  const [highlightIds, setHighlightIds] = useState<Set<string>>(() => new Set());
  // Rows that were just resolved → fade before cache removal catches up.
  const [fadingIds, setFadingIds] = useState<Set<string>>(() => new Set());
  // Transient lag-event banner.
  const [lagBanner, setLagBanner] = useState<string | null>(null);
  // Aggregated batch-failure banner.
  const [errorBanner, setErrorBanner] = useState<string | null>(null);

  const qc = useQueryClient();
  const queryKey = useMemo(() => ["admin", "approvals", tab], [tab]);
  const query = useQuery<Approval[]>({
    queryKey,
    queryFn: () => fetchApprovals(tab === "history"),
    refetchInterval: 15_000,
  });

  // -- mutations ------------------------------------------------------------

  // Tracks the previous pending snapshot for optimistic rollback. We stash
  // it here rather than in mutation context so a batch failure can revert
  // just the failed ids rather than the whole list.
  const pendingSnapshotRef = useRef<Approval[] | undefined>(undefined);

  const snapshotPending = () => {
    pendingSnapshotRef.current = qc.getQueryData<Approval[]>([
      "admin",
      "approvals",
      "pending",
    ]);
  };

  const removePendingLocally = (ids: Iterable<string>) => {
    const drop = new Set(ids);
    qc.setQueryData<Approval[]>(["admin", "approvals", "pending"], (prev) =>
      prev ? prev.filter((r) => !drop.has(r.id)) : prev,
    );
  };

  const restoreFailed = (failedIds: Iterable<string>) => {
    const failed = new Set(failedIds);
    const snap = pendingSnapshotRef.current;
    if (!snap) return;
    qc.setQueryData<Approval[]>(["admin", "approvals", "pending"], (prev) => {
      const current = prev ?? [];
      const seen = new Set(current.map((r) => r.id));
      // Reinsert any row that we optimistically removed but whose POST failed.
      const missing = snap.filter((r) => failed.has(r.id) && !seen.has(r.id));
      return [...current, ...missing];
    });
  };

  const singleMutation = useMutation({
    mutationFn: ({
      id,
      approve,
      reason,
    }: {
      id: string;
      approve: boolean;
      reason?: string;
    }) => decideApproval(id, approve, reason),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: ["admin", "approvals", "pending"] });
      snapshotPending();
      removePendingLocally([id]);
    },
    onError: (err, vars) => {
      restoreFailed([vars.id]);
      setErrorBanner(
        t("approvals.decideFailed", {
          id: vars.id,
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["admin", "approvals"] });
    },
  });

  const batchMutation = useMutation({
    mutationFn: ({
      ids,
      approve,
      reason,
    }: {
      ids: string[];
      approve: boolean;
      reason?: string;
    }) => decideApprovalsBatch(ids, approve, reason),
    onMutate: async ({ ids }) => {
      await qc.cancelQueries({ queryKey: ["admin", "approvals", "pending"] });
      snapshotPending();
      removePendingLocally(ids);
    },
    onSuccess: (outcomes) => {
      const failed = outcomes.filter((o) => !o.ok);
      if (failed.length > 0) {
        restoreFailed(failed.map((o) => o.id));
        setErrorBanner(
          t("approvals.batchSomeFailed", {
            n: failed.length,
            details: failed
              .map((f) => `${f.id}${f.error ? ` (${f.error})` : ""}`)
              .join("; "),
          }),
        );
      } else {
        setErrorBanner(null);
      }
      setSelected(new Set());
    },
    onError: (err, vars) => {
      restoreFailed(vars.ids);
      setErrorBanner(
        t("approvals.batchFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["admin", "approvals"] });
    },
  });

  const anyMutating = singleMutation.isPending || batchMutation.isPending;

  // -- SSE wiring -----------------------------------------------------------

  useEffect(() => {
    // `pending` and `decided` arrive as default `"message"` events; `lag`
    // uses the named event `"lag"` (see Rust `broadcast_to_sse`).
    const close = openEventStream<StreamEvent | { message?: string }>(
      "/admin/approvals/stream",
      {
        events: ["message", "lag"],
        onMessage: ({ event, data }) => {
          if (event === "lag") {
            const message =
              typeof (data as { message?: string }).message === "string"
                ? (data as { message: string }).message
                : typeof data === "string"
                  ? (data as string)
                  : t("approvals.lagEventSkipped");
            setLagBanner(t("approvals.lagBanner", { msg: message }));
            // Force a refetch so ground truth resyncs.
            qc.invalidateQueries({ queryKey: ["admin", "approvals"] });
            return;
          }
          if (!data || typeof data !== "object" || !("kind" in data)) return;
          const evt = data as StreamEvent;
          if (evt.kind === "pending") {
            qc.setQueryData<Approval[]>(
              ["admin", "approvals", "pending"],
              (prev) => {
                const next = prev ? [...prev] : [];
                if (!next.some((r) => r.id === evt.approval.id)) {
                  next.push(evt.approval);
                }
                return next;
              },
            );
            setHighlightIds((prev) => {
              const n = new Set(prev);
              n.add(evt.approval.id);
              return n;
            });
            const id = evt.approval.id;
            window.setTimeout(() => {
              setHighlightIds((prev) => {
                if (!prev.has(id)) return prev;
                const n = new Set(prev);
                n.delete(id);
                return n;
              });
            }, HIGHLIGHT_MS);
          } else if (evt.kind === "decided") {
            const id = evt.id;
            setFadingIds((prev) => {
              const n = new Set(prev);
              n.add(id);
              return n;
            });
            window.setTimeout(() => {
              qc.setQueryData<Approval[]>(
                ["admin", "approvals", "pending"],
                (prev) => (prev ? prev.filter((r) => r.id !== id) : prev),
              );
              setFadingIds((prev) => {
                if (!prev.has(id)) return prev;
                const n = new Set(prev);
                n.delete(id);
                return n;
              });
              qc.invalidateQueries({
                queryKey: ["admin", "approvals", "history"],
              });
            }, FADE_MS);
          }
        },
      },
    );
    return close;
  }, [qc, t]);

  // -- derived --------------------------------------------------------------

  const rawRows = useMemo(() => query.data ?? [], [query.data]);

  const pluginOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const r of rawRows) seen.add(r.plugin);
    return Array.from(seen).sort();
  }, [rawRows]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rawRows.filter((r) => {
      if (pluginFilter && r.plugin !== pluginFilter) return false;
      if (q) {
        const hay = `${r.plugin}.${r.tool}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [rawRows, search, pluginFilter]);

  const selectableIds = useMemo(
    () => filtered.filter((r) => r.decision === null).map((r) => r.id),
    [filtered],
  );

  const allSelected =
    selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));
  const someSelected = selectableIds.some((id) => selected.has(id));

  const toggleAll = () => {
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(selectableIds));
    }
  };

  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  // -- batch action dispatch ------------------------------------------------

  const confirmAndBatchApprove = () => {
    const ids = Array.from(selected);
    if (ids.length === 0) return;
    if (
      !window.confirm(t("approvals.batchApproveConfirm", { n: ids.length }))
    )
      return;
    batchMutation.mutate({ ids, approve: true });
  };

  const openBatchDeny = () => {
    const ids = Array.from(selected);
    if (ids.length === 0) return;
    setDenyDialog({ kind: "batch", ids });
  };

  const handleDenyConfirm = (reason: string) => {
    if (!denyDialog) return;
    if (denyDialog.kind === "single") {
      const id = denyDialog.id;
      setDenyDialog(null);
      singleMutation.mutate({ id, approve: false, reason });
    } else {
      const ids = denyDialog.ids;
      setDenyDialog(null);
      batchMutation.mutate({ ids, approve: false, reason });
    }
  };

  // --- render --------------------------------------------------------------

  const showEmpty =
    !query.isPending && !query.isError && filtered.length === 0;

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("approvals.title")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {t("approvals.subtitle")}
        </p>
      </header>

      {lagBanner ? (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200"
        >
          <span>{lagBanner}</span>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setLagBanner(null)}
            aria-label={t("approvals.closeLagAria")}
          >
            {t("common.close")}
          </Button>
        </div>
      ) : null}
      {errorBanner ? (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-foreground"
        >
          <span>{errorBanner}</span>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setErrorBanner(null)}
            aria-label={t("approvals.closeErrorAria")}
          >
            {t("common.close")}
          </Button>
        </div>
      ) : null}

      <div
        role="tablist"
        aria-label={t("approvals.tabsAria")}
        className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface p-0.5"
      >
        <button
          type="button"
          role="tab"
          aria-selected={tab === "pending"}
          onClick={() => {
            setTab("pending");
            setSelected(new Set());
          }}
          className={cn(
            "inline-flex h-7 items-center rounded px-3 text-xs font-medium transition-colors",
            tab === "pending"
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {t("approvals.tabPending")}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "history"}
          onClick={() => {
            setTab("history");
            setSelected(new Set());
          }}
          className={cn(
            "inline-flex h-7 items-center rounded px-3 text-xs font-medium transition-colors",
            tab === "history"
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {t("approvals.tabHistory")}
        </button>
      </div>

      <FilterBar
        search={search}
        onSearchChange={setSearch}
        pluginFilter={pluginFilter}
        onPluginFilterChange={setPluginFilter}
        pluginOptions={pluginOptions}
      />

      {tab === "pending" ? (
        <BatchToolbar
          selectedCount={selected.size}
          onApproveAll={confirmAndBatchApprove}
          onDenyAll={openBatchDeny}
          onClear={() => setSelected(new Set())}
          disabled={anyMutating}
        />
      ) : null}

      <section className="overflow-hidden rounded-lg border border-border bg-panel">
        <Table>
          <TableHeader>
            <TableRow>
              {tab === "pending" ? (
                <TableHead className="w-10">
                  <Checkbox
                    aria-label={
                      allSelected
                        ? t("approvals.deselectAll")
                        : t("approvals.selectAll")
                    }
                    checked={allSelected}
                    ref={(el) => {
                      if (el) el.indeterminate = !allSelected && someSelected;
                    }}
                    onChange={toggleAll}
                    disabled={selectableIds.length === 0 || anyMutating}
                  />
                </TableHead>
              ) : null}
              <TableHead>{t("approvals.colPluginTool")}</TableHead>
              <TableHead>{t("approvals.colSession")}</TableHead>
              <TableHead>{t("approvals.colArgs")}</TableHead>
              <TableHead>{t("approvals.colRequested")}</TableHead>
              <TableHead>{t("approvals.colStatus")}</TableHead>
              <TableHead className="w-72">
                {t("approvals.colActions")}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isPending ? (
              Array.from({ length: 3 }).map((_, i) => (
                <TableRow key={`sk-${i}`}>
                  {Array.from({ length: tab === "pending" ? 7 : 6 }).map(
                    (_, j) => (
                      <TableCell key={j}>
                        <Skeleton className="h-4 w-24" />
                      </TableCell>
                    ),
                  )}
                </TableRow>
              ))
            ) : query.isError ? (
              <TableRow>
                <TableCell
                  colSpan={tab === "pending" ? 7 : 6}
                  className="py-8 text-center text-sm text-destructive"
                >
                  {t("approvals.loadFailed")}: {(query.error as Error).message}
                </TableCell>
              </TableRow>
            ) : showEmpty ? (
              <TableRow>
                <TableCell colSpan={tab === "pending" ? 7 : 6} className="p-0">
                  <ApprovalsEmptyState tab={tab} />
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((row) => {
                const isPending = row.decision === null;
                const isSelected = selected.has(row.id);
                const isHighlight = highlightIds.has(row.id);
                const isFading = fadingIds.has(row.id);
                return (
                  <TableRow
                    key={row.id}
                    className={cn(
                      "transition-opacity duration-300",
                      isHighlight && "bg-emerald-500/10",
                      isFading && "opacity-30",
                    )}
                  >
                    {tab === "pending" ? (
                      <TableCell>
                        {isPending ? (
                          <Checkbox
                            aria-label={t("approvals.selectOneAria", {
                              plugin: row.plugin,
                              tool: row.tool,
                            })}
                            checked={isSelected}
                            onChange={() => toggleOne(row.id)}
                            disabled={anyMutating}
                          />
                        ) : null}
                      </TableCell>
                    ) : null}
                    <TableCell className="font-mono text-xs">
                      {row.plugin}.{row.tool}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {row.session_key || t("approvals.noneValue")}
                    </TableCell>
                    <TableCell className="max-w-[16rem] truncate font-mono text-xs text-muted-foreground">
                      {truncateArgs(row.args_json)}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatTime(row.requested_at)}
                    </TableCell>
                    <TableCell>
                      <DecisionBadge decision={row.decision} />
                    </TableCell>
                    <TableCell>
                      <div className="flex gap-2">
                        <ArgsDialog approval={row} />
                        {isPending ? (
                          <>
                            <Button
                              size="sm"
                              onClick={() =>
                                singleMutation.mutate({
                                  id: row.id,
                                  approve: true,
                                })
                              }
                              disabled={anyMutating}
                            >
                              {t("approvals.approve")}
                            </Button>
                            <Button
                              size="sm"
                              variant="destructive"
                              onClick={() =>
                                setDenyDialog({ kind: "single", id: row.id })
                              }
                              disabled={anyMutating}
                            >
                              {t("approvals.deny")}
                            </Button>
                          </>
                        ) : null}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </section>

      <DenyReasonDialog
        open={denyDialog !== null}
        onOpenChange={(open) => {
          if (!open) setDenyDialog(null);
        }}
        targetLabel={
          denyDialog?.kind === "batch"
            ? t("approvals.batchTarget", { n: denyDialog.ids.length })
            : t("approvals.singleTarget")
        }
        onConfirm={handleDenyConfirm}
        submitting={anyMutating}
      />
    </>
  );
}
