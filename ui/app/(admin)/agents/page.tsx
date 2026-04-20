"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import { apiFetch, type AgentSummary } from "@/lib/api";

/**
 * Agents admin page — lists Agent/*.txt, click a row to see the (future)
 * Monaco editor. Currently the dialog is a placeholder per plan §17.
 *
 * Backing endpoint: GET /admin/agents served by ui/mock/server.ts in dev.
 */

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
  const { t } = useI18n();
  const [selected, setSelected] = React.useState<AgentSummary | null>(null);

  const query = useQuery<AgentSummary[]>({
    queryKey: ["admin", "agents"],
    queryFn: () => apiFetch<AgentSummary[]>("/admin/agents"),
  });

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("nav.agents")}
        </h1>
        <p className="text-sm text-muted-foreground">
          编辑 `Agent/*.txt`，点击行打开编辑器占位（M6 接 Monaco）。文件名遵循
          Markdown frontmatter 约定（plan §17）。
        </p>
      </header>

      <section className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("table.agent.name")}</TableHead>
              <TableHead>{t("table.agent.path")}</TableHead>
              <TableHead className="w-32">{t("table.agent.bytes")}</TableHead>
              <TableHead className="w-56">
                {t("table.agent.last_modified")}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isPending ? (
              Array.from({ length: 3 }).map((_, i) => (
                <TableRow key={`sk-${i}`}>
                  {Array.from({ length: 4 }).map((_, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-4 w-24" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : query.isError ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="py-8 text-center text-sm text-destructive"
                >
                  {t("state.error")}: {(query.error as Error).message}
                </TableCell>
              </TableRow>
            ) : !query.data || query.data.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="py-8 text-center text-sm text-muted-foreground"
                >
                  {t("state.empty")}
                </TableCell>
              </TableRow>
            ) : (
              query.data.map((a) => (
                <TableRow
                  key={a.name}
                  onClick={() => setSelected(a)}
                  className="cursor-pointer"
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") setSelected(a);
                  }}
                >
                  <TableCell className="font-medium">{a.name}</TableCell>
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

      <Dialog open={selected !== null} onOpenChange={(o) => !o && setSelected(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {selected?.name ?? ""}{" "}
              <span className="font-mono text-xs text-muted-foreground">
                {selected?.file_path}
              </span>
            </DialogTitle>
            <DialogDescription>{t("agent.editor.placeholder")}</DialogDescription>
          </DialogHeader>
          <pre className="max-h-80 overflow-auto rounded-md bg-muted p-3 text-xs text-muted-foreground">
            {`// ${t("agent.editor.monaco_todo")}
// bytes:        ${selected ? formatBytes(selected.bytes) : ""}
// last modified ${selected ? formatTime(selected.last_modified) : ""}
//
// M6: wire up @monaco-editor/react + GET /admin/agents/:name`}
          </pre>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSelected(null)}>
              {t("action.cancel")}
            </Button>
            <Button disabled>{t("action.save")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
