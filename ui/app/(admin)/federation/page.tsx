"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Network, PowerOff } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  addFederationPeer,
  fetchFederationPeers,
  removeFederationPeer,
  type FederationListResult,
  type FederationPeer,
} from "@/lib/api/federation";
import { RecentProposalsDialog } from "@/components/federation/recent-proposals-dialog";

/**
 * `/admin/federation` — Phase 4 W2 B3 iter 6+.
 *
 * Two-pane operator surface for tenant federation peers. The left pane
 * ("Accepted from") is the recipient view: tenants the operator has opted
 * to accept federated proposals from. The right pane ("Peers of us") is
 * the publishing view: read-only — adding ourselves there is a remote-side
 * action, not something we can effect locally.
 *
 * Selecting a slug in "Accepted from" opens a recent-proposals dialog
 * showing the last 50 federated rows from that source. The 503 disabled
 * envelope is rendered as a banner in the same shape `/admin/sessions` and
 * `/admin/identity` use.
 */
export default function FederationPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [activeSource, setActiveSource] = React.useState<string | null>(null);

  const query = useQuery<FederationListResult>({
    queryKey: ["admin", "federation", "peers"],
    queryFn: () => fetchFederationPeers(),
  });

  const refetch = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "federation", "peers"] });

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("federation.title")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("federation.subtitle")}</p>
      </header>

      {query.data?.kind === "tenants_disabled" ? <DisabledBanner /> : null}

      <div className="grid gap-4 md:grid-cols-2">
        <section
          className="space-y-3 rounded-lg border border-tp-glass-edge bg-tp-glass p-4"
          data-testid="federation-accepted-from"
        >
          <div className="flex items-baseline justify-between">
            <h2 className="text-sm font-medium">
              {t("federation.acceptedFromTitle")}
            </h2>
            <p className="text-[11px] text-tp-ink-3">
              {t("federation.acceptedFromHint")}
            </p>
          </div>

          <PeersTable
            kind="accepted_from"
            data={query.data}
            isPending={query.isPending}
            isError={query.isError}
            errorMessage={
              query.error instanceof Error ? query.error.message : ""
            }
            onSelectSource={(slug) => setActiveSource(slug)}
            onRowMutated={() => void refetch()}
          />

          <AddSourceForm
            disabled={query.data?.kind === "tenants_disabled"}
            onAdded={() => void refetch()}
          />
        </section>

        <section
          className="space-y-3 rounded-lg border border-tp-glass-edge bg-tp-glass p-4"
          data-testid="federation-peers-of-us"
        >
          <div className="flex items-baseline justify-between">
            <h2 className="text-sm font-medium">
              {t("federation.peersOfUsTitle")}
            </h2>
            <p className="text-[11px] text-tp-ink-3">
              {t("federation.peersOfUsHint")}
            </p>
          </div>

          <PeersTable
            kind="peers_of_us"
            data={query.data}
            isPending={query.isPending}
            isError={query.isError}
            errorMessage={
              query.error instanceof Error ? query.error.message : ""
            }
          />
        </section>
      </div>

      <RecentProposalsDialog
        sourceTenantId={activeSource}
        open={Boolean(activeSource)}
        onClose={() => setActiveSource(null)}
      />
    </>
  );
}

/* ------------------------------------------------------------------ */
/*                       Disabled-state banner                         */
/* ------------------------------------------------------------------ */

function DisabledBanner() {
  const { t } = useTranslation();
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-3 rounded-lg border px-4 py-3",
        "border-amber-500/40 bg-amber-500/10 text-amber-200",
      )}
      data-testid="federation-disabled-banner"
    >
      <PowerOff
        aria-hidden="true"
        className="mt-0.5 h-4 w-4 shrink-0 text-amber-400"
      />
      <div className="space-y-1">
        <div className="text-sm font-medium">
          {t("federation.disabledTitle")}
        </div>
        <div className="text-xs text-amber-200/80">
          {t("federation.disabledHint")}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*                              Tables                                 */
/* ------------------------------------------------------------------ */

interface PeersTableProps {
  kind: "accepted_from" | "peers_of_us";
  data: FederationListResult | undefined;
  isPending: boolean;
  isError: boolean;
  errorMessage: string;
  /** Only set on the recipient pane — drives the recent-proposals dialog. */
  onSelectSource?: (slug: string) => void;
  /** Only set on the recipient pane — refetches after a successful remove. */
  onRowMutated?: () => void;
}

function PeersTable({
  kind,
  data,
  isPending,
  isError,
  errorMessage,
  onSelectSource,
  onRowMutated,
}: PeersTableProps) {
  const { t } = useTranslation();
  const rows: FederationPeer[] | null =
    data?.kind === "ok"
      ? kind === "accepted_from"
        ? data.accepted_from
        : data.peers_of_us
      : null;

  // Recipient pane shows the source slug; publishing pane shows the peer slug.
  const slugColLabel =
    kind === "accepted_from"
      ? t("federation.colSource")
      : t("federation.colPeer");

  const colSpan = kind === "accepted_from" ? 4 : 3;

  return (
    <div className="overflow-hidden rounded-md border border-tp-glass-edge">
      <Table>
        <TableHeader>
          <TableRow className="border-b border-tp-glass-edge hover:bg-transparent">
            <TableHead>{slugColLabel}</TableHead>
            <TableHead className="w-40">
              {t("federation.colAcceptedBy")}
            </TableHead>
            <TableHead className="w-48">
              {t("federation.colAcceptedAt")}
            </TableHead>
            {kind === "accepted_from" ? (
              <TableHead className="w-24 text-right">
                {t("federation.colActions")}
              </TableHead>
            ) : null}
          </TableRow>
        </TableHeader>
        <TableBody>
          {isPending ? (
            <PeersTableSkeleton colSpan={colSpan} />
          ) : isError ? (
            <TableRow>
              <TableCell
                colSpan={colSpan}
                className="py-10 text-center text-sm text-destructive"
                data-testid={`federation-${kind.replaceAll("_", "-")}-load-failed`}
              >
                {t("federation.loadFailed")}: {errorMessage}
              </TableCell>
            </TableRow>
          ) : data?.kind === "tenants_disabled" ? (
            <TableRow>
              <TableCell
                colSpan={colSpan}
                className="py-10 text-center text-sm text-tp-ink-3"
                data-testid={`federation-${kind.replaceAll("_", "-")}-disabled-row`}
              >
                {t("federation.disabledHint")}
              </TableCell>
            </TableRow>
          ) : !rows || rows.length === 0 ? (
            <TableRow>
              <TableCell
                colSpan={colSpan}
                className="py-10 text-center text-sm text-tp-ink-3"
                data-testid={`federation-${kind.replaceAll("_", "-")}-empty`}
              >
                {kind === "accepted_from"
                  ? t("federation.acceptedFromEmpty")
                  : t("federation.peersOfUsEmpty")}
              </TableCell>
            </TableRow>
          ) : (
            rows.map((row) => (
              <PeerRow
                key={`${row.peer_tenant_id}|${row.source_tenant_id}`}
                row={row}
                kind={kind}
                onSelectSource={onSelectSource}
                onRemoved={onRowMutated}
              />
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}

function PeerRow({
  row,
  kind,
  onSelectSource,
  onRemoved,
}: {
  row: FederationPeer;
  kind: "accepted_from" | "peers_of_us";
  onSelectSource?: (slug: string) => void;
  onRemoved?: () => void;
}) {
  const { t } = useTranslation();
  const slug =
    kind === "accepted_from" ? row.source_tenant_id : row.peer_tenant_id;
  const isRecipient = kind === "accepted_from";

  const remove = useMutation({
    mutationFn: () => removeFederationPeer(row.source_tenant_id),
    onSuccess: (res) => {
      if (res.kind === "ok") {
        toast.success(t("federation.removed"));
        onRemoved?.();
      } else if (res.kind === "not_found") {
        toast.error(t("federation.removeNotFound"));
        onRemoved?.();
      } else if (res.kind === "tenants_disabled") {
        toast.error(t("federation.disabledTitle"));
      }
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : t("common.error"),
      );
    },
  });

  return (
    <TableRow
      className="border-b border-tp-glass-edge"
      data-testid={`federation-${kind.replaceAll("_", "-")}-row-${slug}`}
    >
      <TableCell className="font-mono text-xs">
        {isRecipient && onSelectSource ? (
          <button
            type="button"
            className="text-left text-tp-ink underline-offset-2 hover:underline"
            onClick={() => onSelectSource(row.source_tenant_id)}
            data-testid={`federation-source-link-${slug}`}
          >
            {slug}
          </button>
        ) : (
          <span>{slug}</span>
        )}
      </TableCell>
      <TableCell className="text-xs text-tp-ink-2">
        {row.accepted_by ?? "—"}
      </TableCell>
      <TableCell className="text-xs text-tp-ink-3">
        {new Date(row.accepted_at_ms).toLocaleString()}
      </TableCell>
      {isRecipient ? (
        <TableCell className="pr-2 text-right">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={remove.isPending}
            onClick={() => remove.mutate()}
            data-testid={`federation-remove-${slug}`}
          >
            {remove.isPending ? t("common.saving") : t("common.remove")}
          </Button>
        </TableCell>
      ) : null}
    </TableRow>
  );
}

function PeersTableSkeleton({ colSpan }: { colSpan: number }) {
  return (
    <>
      {Array.from({ length: 2 }).map((_, i) => (
        <TableRow
          key={`fed-sk-${i}`}
          className="border-b border-tp-glass-edge"
        >
          <TableCell>
            <Skeleton className="h-4 w-24" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-20" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-32" />
          </TableCell>
          {colSpan === 4 ? (
            <TableCell className="pr-2 text-right">
              <Skeleton className="ml-auto h-7 w-16" />
            </TableCell>
          ) : null}
        </TableRow>
      ))}
    </>
  );
}

/* ------------------------------------------------------------------ */
/*                          Add-source form                            */
/* ------------------------------------------------------------------ */

function AddSourceForm({
  disabled,
  onAdded,
}: {
  disabled: boolean;
  onAdded: () => void;
}) {
  const { t } = useTranslation();
  const [slug, setSlug] = React.useState("");

  const add = useMutation({
    mutationFn: () => addFederationPeer(slug.trim()),
    onSuccess: (res) => {
      if (res.kind === "ok") {
        toast.success(t("federation.added"));
        setSlug("");
        onAdded();
      } else if (res.kind === "invalid_input") {
        toast.error(res.message || t("federation.invalidSlug"));
      } else if (res.kind === "tenants_disabled") {
        toast.error(t("federation.disabledTitle"));
      }
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : t("common.error"),
      );
    },
  });

  return (
    <form
      className="flex items-center gap-2"
      data-testid="federation-add-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (!slug.trim()) return;
        add.mutate();
      }}
    >
      <Network
        aria-hidden="true"
        className="h-4 w-4 shrink-0 text-tp-ink-3"
      />
      <Input
        type="text"
        value={slug}
        onChange={(e) => setSlug(e.target.value)}
        placeholder={t("federation.addPlaceholder")}
        aria-label={t("federation.addLabel")}
        disabled={disabled || add.isPending}
        data-testid="federation-add-input"
        className="h-8 flex-1"
      />
      <Button
        type="submit"
        size="sm"
        disabled={disabled || add.isPending || slug.trim().length === 0}
        data-testid="federation-add-submit"
      >
        {add.isPending ? t("common.saving") : t("federation.addSubmit")}
      </Button>
    </form>
  );
}
