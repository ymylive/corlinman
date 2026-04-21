"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { RefreshCw, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
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
import { cn } from "@/lib/utils";
import { apiFetch, type PluginStatus, type PluginSummary } from "@/lib/api";

/**
 * Plugins admin page. Live against GET /admin/plugins. The row-click
 * navigates via `?name=` query param (no dynamic segment — keeps the
 * Next static export happy).
 */

function StatusDot({ status }: { status: PluginStatus }) {
  const tone =
    status === "loaded"
      ? "bg-ok"
      : status === "error"
        ? "bg-err"
        : "bg-muted-foreground/50";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span className={cn("inline-block h-1.5 w-1.5 rounded-full", tone)} />
      <span
        className={cn(
          status === "loaded"
            ? "text-ok"
            : status === "error"
              ? "text-err"
              : "text-muted-foreground",
        )}
      >
        {status}
      </span>
    </span>
  );
}

function TypeBadge({ type }: { type: string }) {
  const color =
    type === "synchronous" || type === "sync"
      ? "bg-primary/15 text-primary"
      : type === "asynchronous" || type === "async"
        ? "bg-warn/15 text-warn"
        : "bg-muted text-muted-foreground";
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded px-1.5 font-mono text-[10px] uppercase tracking-wider",
        color,
      )}
    >
      {type}
    </span>
  );
}

export default function PluginsPage() {
  const { t } = useTranslation();
  const [search, setSearch] = React.useState("");
  const query = useQuery<PluginSummary[]>({
    queryKey: ["admin", "plugins"],
    queryFn: () => apiFetch<PluginSummary[]>("/admin/plugins"),
  });

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return query.data ?? [];
    return (query.data ?? []).filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.capabilities.some((c) => c.toLowerCase().includes(q)),
    );
  }, [query.data, search]);

  return (
    <>
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("plugins.title")}
          </h1>
          <p className="text-sm text-muted-foreground">
            {t("plugins.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder={t("plugins.filterPlaceholder")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 w-56 pl-8 text-xs"
            />
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
            aria-label={t("plugins.refreshAria")}
          >
            <RefreshCw
              className={cn(
                "h-3.5 w-3.5",
                query.isFetching && "animate-spin",
              )}
            />
            {t("plugins.refresh")}
          </Button>
        </div>
      </header>

      <section className="overflow-hidden rounded-lg border border-border bg-panel">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border hover:bg-transparent">
              <TableHead className="pl-4">{t("plugins.colName")}</TableHead>
              <TableHead>{t("plugins.colType")}</TableHead>
              <TableHead>{t("plugins.colStatus")}</TableHead>
              <TableHead>{t("plugins.colTools")}</TableHead>
              <TableHead>{t("plugins.colOrigin")}</TableHead>
              <TableHead>{t("plugins.colLastTouched")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isPending ? (
              Array.from({ length: 4 }).map((_, i) => (
                <TableRow key={`sk-${i}`} className="border-b border-border">
                  {Array.from({ length: 6 }).map((_, j) => (
                    <TableCell key={j} className={j === 0 ? "pl-4" : undefined}>
                      <Skeleton className="h-4 w-24" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : query.isError ? (
              <TableRow>
                <TableCell
                  colSpan={6}
                  className="py-10 text-center text-sm text-destructive"
                >
                  {t("plugins.loadFailed")}: {(query.error as Error).message}
                </TableCell>
              </TableRow>
            ) : filtered.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={6}
                  className="py-10 text-center text-sm text-muted-foreground"
                >
                  {search ? t("plugins.noMatches") : t("plugins.noneRegistered")}
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((p) => (
                <TableRow
                  key={p.name}
                  className="border-b border-border transition-colors hover:bg-accent/30"
                >
                  <TableCell className="pl-4 font-medium">
                    <Link
                      href={{
                        pathname: "/plugins/detail",
                        query: { name: p.name },
                      }}
                      className="hover:text-primary"
                      data-testid={`plugin-link-${p.name}`}
                    >
                      {p.name}
                    </Link>
                    <span className="ml-2 font-mono text-xs text-muted-foreground">
                      {p.version}
                    </span>
                    {p.error ? (
                      <span className="ml-2 text-xs text-destructive" title={p.error}>
                        ⚠
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    <TypeBadge type={p.plugin_type} />
                  </TableCell>
                  <TableCell>
                    <StatusDot status={p.status} />
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {p.capabilities.length}
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className="font-mono text-[10px]">
                      {p.origin}
                    </Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {formatRelative(p.last_touched_at, t)}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </section>
    </>
  );
}

function formatRelative(
  iso: string,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  try {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const s = Math.round((now - then) / 1000);
    if (s < 60) return t("common.secondsAgo", { n: s });
    if (s < 3600) return t("common.minutesAgo", { n: Math.round(s / 60) });
    if (s < 86400) return t("common.hoursAgo", { n: Math.round(s / 3600) });
    return t("common.daysAgo", { n: Math.round(s / 86400) });
  } catch {
    return iso;
  }
}
