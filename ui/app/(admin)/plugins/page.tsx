"use client";

import { useQuery } from "@tanstack/react-query";
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
import { useI18n } from "@/components/providers";
import { apiFetch, type PluginStatus, type PluginSummary } from "@/lib/api";

/**
 * Plugins admin page — real data (mock server) → real UI.
 *
 * Backing endpoint: GET /admin/plugins. In dev this comes from
 * ui/mock/server.ts; in M6 it will come from corlinman-gateway.
 */

function StatusBadge({ status }: { status: PluginStatus }) {
  if (status === "loaded") {
    return (
      <Badge className="border-transparent bg-emerald-600/20 text-emerald-300 hover:bg-emerald-600/30">
        loaded
      </Badge>
    );
  }
  if (status === "error") {
    return <Badge variant="destructive">error</Badge>;
  }
  return <Badge variant="secondary">disabled</Badge>;
}

export default function PluginsPage() {
  const { t } = useI18n();
  const query = useQuery<PluginSummary[]>({
    queryKey: ["admin", "plugins"],
    queryFn: () => apiFetch<PluginSummary[]>("/admin/plugins"),
  });

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">{t("nav.plugins")}</h1>
        <p className="text-sm text-muted-foreground">
          manifest-first 发现，显示 origin / 启停 / sandbox 配置 / doctor 输出。
          对应 corlinman-plugins::registry（plan §7）。
        </p>
      </header>

      <section className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("table.plugin.name")}</TableHead>
              <TableHead>{t("table.plugin.version")}</TableHead>
              <TableHead>{t("table.plugin.status")}</TableHead>
              <TableHead>{t("table.plugin.origin")}</TableHead>
              <TableHead>{t("table.plugin.capabilities")}</TableHead>
              <TableHead>{t("table.plugin.manifest")}</TableHead>
              <TableHead className="w-48">{t("table.plugin.actions")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isPending ? (
              Array.from({ length: 4 }).map((_, i) => (
                <TableRow key={`sk-${i}`}>
                  {Array.from({ length: 7 }).map((_, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-4 w-24" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : query.isError ? (
              <TableRow>
                <TableCell
                  colSpan={7}
                  className="py-8 text-center text-sm text-destructive"
                >
                  {t("state.error")}: {(query.error as Error).message}
                </TableCell>
              </TableRow>
            ) : !query.data || query.data.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={7}
                  className="py-8 text-center text-sm text-muted-foreground"
                >
                  {t("state.empty")}
                </TableCell>
              </TableRow>
            ) : (
              query.data.map((p) => (
                <TableRow key={p.name}>
                  <TableCell className="font-medium">
                    {p.name}
                    {p.error ? (
                      <span
                        className="ml-2 text-xs text-destructive"
                        title={p.error}
                      >
                        ⚠︎
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{p.version}</TableCell>
                  <TableCell>
                    <StatusBadge status={p.status} />
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {p.origin}
                  </TableCell>
                  <TableCell className="max-w-[240px] truncate text-xs text-muted-foreground">
                    {p.capabilities.join(", ")}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {p.manifest_path}
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-2">
                      <Button size="sm" variant="outline" disabled>
                        {p.status === "disabled"
                          ? t("action.enable")
                          : t("action.disable")}
                      </Button>
                      <Button size="sm" variant="ghost" disabled>
                        {t("action.doctor")}
                      </Button>
                    </div>
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
