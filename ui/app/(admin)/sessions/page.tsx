"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { PowerOff } from "lucide-react";

import { cn } from "@/lib/utils";
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
  fetchSessions,
  type SessionSummary,
  type SessionsListResult,
} from "@/lib/api/sessions";
import { ReplayDialog } from "@/components/sessions/replay-dialog";
import { SessionRow } from "@/components/sessions/session-row";

/**
 * `/admin/sessions` — Phase 4 Wave 2 task 4-2D.
 *
 * Lists session keys with last-message timestamp + message count and a
 * per-row Replay button. Selecting Replay opens `<ReplayDialog>` which
 * defaults to `mode = "transcript"` (deterministic dump) and renders the
 * returned transcript chat-style.
 *
 * Mirrors the list-page-with-action shape that Phase 4 W1 4-1B
 * (`/admin/tenants`) is expected to ship — until that surface lands the
 * closest precedent is `/admin/agents`. We use `useQuery` over Agent A's
 * Rust route, with the Sessions API client returning a tagged
 * `SessionsListResult` so 503 `sessions_disabled` is rendered as a banner
 * rather than a red error.
 */

export default function SessionsPage() {
  const { t } = useTranslation();
  const [active, setActive] = React.useState<SessionSummary | null>(null);

  const query = useQuery<SessionsListResult>({
    queryKey: ["admin", "sessions"],
    queryFn: () => fetchSessions(),
  });

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("sessions.title")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("sessions.subtitle")}</p>
      </header>

      {query.data?.kind === "disabled" ? (
        <SessionsDisabledBanner />
      ) : null}

      <section className="overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-tp-glass-edge hover:bg-transparent">
              <TableHead className="pl-4">
                {t("sessions.colSessionKey")}
              </TableHead>
              <TableHead className="w-32">
                {t("sessions.colMessageCount")}
              </TableHead>
              <TableHead className="w-56">
                {t("sessions.colLastMessageAt")}
              </TableHead>
              <TableHead className="w-32 pr-4 text-right">
                {t("sessions.colActions")}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isPending ? (
              <SessionsTableSkeleton />
            ) : query.isError ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="py-10 text-center text-sm text-destructive"
                  data-testid="sessions-load-failed"
                >
                  {t("sessions.loadFailed")}: {(query.error as Error).message}
                </TableCell>
              </TableRow>
            ) : query.data?.kind === "disabled" ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="py-10 text-center text-sm text-tp-ink-3"
                  data-testid="sessions-disabled-row"
                >
                  {t("sessions.sessionsDisabledHint")}
                </TableCell>
              </TableRow>
            ) : !query.data || query.data.sessions.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="py-10 text-center text-sm text-tp-ink-3"
                  data-testid="sessions-empty"
                >
                  {t("sessions.empty")}
                </TableCell>
              </TableRow>
            ) : (
              query.data.sessions.map((s) => (
                <SessionRow
                  key={s.session_key}
                  session={s}
                  onReplay={setActive}
                />
              ))
            )}
          </TableBody>
        </Table>
      </section>

      <ReplayDialog session={active} onClose={() => setActive(null)} />
    </>
  );
}

function SessionsDisabledBanner() {
  const { t } = useTranslation();
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-3 rounded-lg border px-4 py-3",
        "border-amber-500/40 bg-amber-500/10 text-amber-200",
      )}
      data-testid="sessions-disabled-banner"
    >
      <PowerOff
        aria-hidden="true"
        className="mt-0.5 h-4 w-4 shrink-0 text-amber-400"
      />
      <div className="space-y-1">
        <div className="text-sm font-medium">
          {t("sessions.sessionsDisabledTitle")}
        </div>
        <div className="text-xs text-amber-200/80">
          {t("sessions.sessionsDisabledHint")}
        </div>
      </div>
    </div>
  );
}

function SessionsTableSkeleton() {
  return (
    <>
      {Array.from({ length: 3 }).map((_, i) => (
        <TableRow
          key={`session-sk-${i}`}
          className="border-b border-tp-glass-edge"
        >
          <TableCell className="pl-4">
            <Skeleton className="h-4 w-32" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-10" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-40" />
          </TableCell>
          <TableCell className="pr-4 text-right">
            <Skeleton className="ml-auto h-7 w-20" />
          </TableCell>
        </TableRow>
      ))}
    </>
  );
}

