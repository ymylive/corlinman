"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import {
  fetchRecentFederatedProposals,
  type RecentProposalsResult,
} from "@/lib/api/federation";

/**
 * Recent-proposals dialog (Phase 4 W2 B3 iter 6+).
 *
 * Renders the last 50 federated proposals received from a single source
 * tenant. Backed by `GET /admin/federation/peers/:source/recent_proposals`.
 *
 * Empty state intentional: a slug being valid + opted-in doesn't imply at
 * least one proposal has crossed yet, so the route returns 200 with `[]`
 * for that case rather than a 404. The dialog mirrors the contract.
 */
export function RecentProposalsDialog({
  sourceTenantId,
  open,
  onClose,
}: {
  sourceTenantId: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const slug = sourceTenantId ?? "";

  const query = useQuery<RecentProposalsResult>({
    queryKey: ["admin", "federation", "recent", slug],
    queryFn: () => fetchRecentFederatedProposals(slug),
    enabled: open && Boolean(sourceTenantId),
  });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        className="sm:max-w-2xl"
        data-testid="federation-recent-dialog"
      >
        <DialogHeader>
          <DialogTitle>
            {t("federation.recent.title", "Recent federated proposals")}
            <span className="ml-2 font-mono text-xs text-tp-ink-3">
              {sourceTenantId ?? ""}
            </span>
          </DialogTitle>
          <DialogDescription>
            {t(
              "federation.recent.subtitle",
              "Last 50 proposals this tenant received from the selected source. Reads the recipient's evolution.sqlite — empty until at least one proposal has crossed the federation boundary.",
            )}
          </DialogDescription>
        </DialogHeader>

        <section
          className="overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass"
          data-testid="federation-recent-table"
        >
          <Table>
            <TableHeader>
              <TableRow className="border-b border-tp-glass-edge hover:bg-transparent">
                <TableHead>{t("federation.recent.colKind", "Kind")}</TableHead>
                <TableHead>
                  {t("federation.recent.colStatus", "Status")}
                </TableHead>
                <TableHead>
                  {t("federation.recent.colCreatedAt", "Created at")}
                </TableHead>
                <TableHead>
                  {t(
                    "federation.recent.colSourceProposalId",
                    "Source proposal id",
                  )}
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.isPending ? (
                <RecentTableSkeleton />
              ) : query.isError ? (
                <TableRow>
                  <TableCell
                    colSpan={4}
                    className="py-8 text-center text-sm text-destructive"
                    data-testid="federation-recent-load-failed"
                  >
                    {t("federation.recent.loadFailed", "Could not load proposals")}
                    : {(query.error as Error).message}
                  </TableCell>
                </TableRow>
              ) : query.data?.kind === "tenants_disabled" ? (
                <TableRow>
                  <TableCell
                    colSpan={4}
                    className="py-8 text-center text-sm text-tp-ink-3"
                    data-testid="federation-recent-disabled-row"
                  >
                    {t(
                      "federation.disabledHint",
                      "Multi-tenant mode is off — enable [tenants].enabled = true in config.toml",
                    )}
                  </TableCell>
                </TableRow>
              ) : query.data?.kind === "not_found" ? (
                <TableRow>
                  <TableCell
                    colSpan={4}
                    className="py-8 text-center text-sm text-tp-ink-3"
                    data-testid="federation-recent-not-found"
                  >
                    {t(
                      "federation.recent.notFound",
                      "Source tenant slug rejected — has it been removed?",
                    )}
                  </TableCell>
                </TableRow>
              ) : !query.data || query.data.proposals.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={4}
                    className="py-8 text-center text-sm text-tp-ink-3"
                    data-testid="federation-recent-empty"
                  >
                    {t(
                      "federation.recent.empty",
                      "No federated proposals received yet.",
                    )}
                  </TableCell>
                </TableRow>
              ) : (
                query.data.proposals.map((p) => (
                  <TableRow
                    key={p.id}
                    data-testid={`federation-recent-row-${p.id}`}
                    className="border-b border-tp-glass-edge"
                  >
                    <TableCell className="font-mono text-xs">{p.kind}</TableCell>
                    <TableCell className="text-tp-ink-2">{p.status}</TableCell>
                    <TableCell className="text-xs text-tp-ink-3">
                      {new Date(p.created_at).toLocaleString()}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {p.federated_from.source_proposal_id}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </section>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            {t("common.close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RecentTableSkeleton() {
  return (
    <>
      {Array.from({ length: 3 }).map((_, i) => (
        <TableRow
          key={`recent-sk-${i}`}
          className="border-b border-tp-glass-edge"
        >
          <TableCell>
            <Skeleton className="h-4 w-24" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-16" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-32" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-40" />
          </TableCell>
        </TableRow>
      ))}
    </>
  );
}
