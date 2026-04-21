"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { FileText } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiFetch, type AgentSummary } from "@/lib/api";

/** Lists `Agent/*.md` files. Click a row → Monaco editor at `/agents/detail?name=`. */

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function AgentsPage() {
  const { t } = useTranslation();
  const query = useQuery<AgentSummary[]>({
    queryKey: ["admin", "agents"],
    queryFn: () => apiFetch<AgentSummary[]>("/admin/agents"),
  });

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("agents.title")}
        </h1>
        <p className="text-sm text-muted-foreground">{t("agents.subtitle")}</p>
      </header>

      <section className="overflow-hidden rounded-lg border border-border bg-panel">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border hover:bg-transparent">
              <TableHead className="pl-4">{t("agents.colName")}</TableHead>
              <TableHead>{t("agents.colPath")}</TableHead>
              <TableHead className="w-32">{t("agents.colBytes")}</TableHead>
              <TableHead className="w-56">
                {t("agents.colLastModified")}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isPending ? (
              Array.from({ length: 3 }).map((_, i) => (
                <TableRow key={`sk-${i}`} className="border-b border-border">
                  {Array.from({ length: 4 }).map((_, j) => (
                    <TableCell key={j} className={j === 0 ? "pl-4" : undefined}>
                      <Skeleton className="h-4 w-24" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : query.isError ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="py-10 text-center text-sm text-destructive"
                >
                  {t("agents.loadFailed")}: {(query.error as Error).message}
                </TableCell>
              </TableRow>
            ) : !query.data || query.data.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="py-10 text-center text-sm text-muted-foreground"
                >
                  {t("agents.empty")}
                </TableCell>
              </TableRow>
            ) : (
              query.data.map((a) => (
                <TableRow
                  key={a.name}
                  className="border-b border-border transition-colors hover:bg-accent/30"
                >
                  <TableCell className="pl-4 font-medium">
                    <Link
                      href={{
                        pathname: "/agents/detail",
                        query: { name: a.name },
                      }}
                      className="inline-flex items-center gap-2 hover:text-primary"
                      data-testid={`agent-link-${a.name}`}
                    >
                      <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                      {a.name}
                    </Link>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {a.file_path}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {formatBytes(a.bytes)}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatTime(a.last_modified)}
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
