"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Database, FileText, Tag } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  fetchRagStats,
  queryRag,
  rebuildRag,
  type RagHit,
  type RagQueryResponse,
  type RagStats,
} from "@/lib/api";

/**
 * RAG admin page. Live against /admin/rag/stats · /query · /rebuild.
 * Query panel runs BM25 only (dense vectors need the embedding service).
 * Rebuild is guarded by a `window.confirm` because it rescans all chunks.
 */
export default function RagPage() {
  const qc = useQueryClient();
  const stats = useQuery<RagStats>({
    queryKey: ["admin", "rag", "stats"],
    queryFn: fetchRagStats,
    refetchInterval: 30_000,
  });

  const [q, setQ] = React.useState("");
  const [k, setK] = React.useState(10);
  const [tagFilter, setTagFilter] = React.useState<string[]>([]);
  const [tagDraft, setTagDraft] = React.useState("");
  const [results, setResults] = React.useState<RagQueryResponse | null>(null);
  const [queryError, setQueryError] = React.useState<string | null>(null);

  const queryMutation = useMutation({
    mutationFn: ({ q, k }: { q: string; k: number }) => queryRag(q, k),
    onSuccess: (data) => {
      setResults(data);
      setQueryError(null);
    },
    onError: (err) => {
      setQueryError(err instanceof Error ? err.message : String(err));
      setResults(null);
    },
  });

  const rebuildMutation = useMutation({
    mutationFn: rebuildRag,
    onSuccess: () => {
      toast.success("Rebuild complete");
      qc.invalidateQueries({ queryKey: ["admin", "rag", "stats"] });
    },
    onError: (err) => {
      toast.error(`Rebuild failed: ${err instanceof Error ? err.message : String(err)}`);
    },
  });

  const handleQuery = (e: React.FormEvent) => {
    e.preventDefault();
    if (!q.trim()) return;
    queryMutation.mutate({ q: q.trim(), k });
  };

  const handleRebuild = () => {
    if (
      !window.confirm(
        "Rebuild chunks_fts index? This rescans every chunk in the store.",
      )
    )
      return;
    rebuildMutation.mutate();
  };

  const addTag = (raw: string) => {
    const t = raw.trim();
    if (!t) return;
    if (tagFilter.includes(t)) return;
    setTagFilter([...tagFilter, t]);
    setTagDraft("");
  };
  const removeTag = (t: string) =>
    setTagFilter(tagFilter.filter((x) => x !== t));

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">RAG</h1>
        <p className="text-sm text-muted-foreground">
          `/admin/rag/stats` · `/query` · `/rebuild` — BM25 debug scan; dense
          vectors via embedding service.
        </p>
      </header>

      <section className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <RagStat
          label="Chunks"
          value={stats.data?.chunks}
          loading={stats.isPending}
          icon={<Database className="h-4 w-4" />}
        />
        <RagStat
          label="Files"
          value={stats.data?.files}
          loading={stats.isPending}
          icon={<FileText className="h-4 w-4" />}
        />
        <RagStat
          label="Tags"
          value={stats.data?.tags}
          loading={stats.isPending}
          icon={<Tag className="h-4 w-4" />}
        />
      </section>

      <section className="space-y-4 rounded-lg border border-border bg-panel p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Query debug (BM25)</h2>
        </div>
        <form
          className="space-y-3"
          onSubmit={handleQuery}
          aria-label="rag query"
        >
          <div className="flex flex-wrap gap-2">
            <Input
              placeholder="Query..."
              value={q}
              onChange={(e) => setQ(e.target.value)}
              className="max-w-md"
              data-testid="rag-query-input"
            />
            <div className="flex items-center gap-2">
              <label className="text-[10px] uppercase tracking-wider text-muted-foreground">
                k
              </label>
              <input
                type="range"
                min={1}
                max={50}
                value={k}
                onChange={(e) => setK(Number(e.target.value))}
                className="h-8 w-28 accent-primary"
                aria-label="top-k"
              />
              <span className="w-6 font-mono text-xs">{k}</span>
            </div>
            <Button
              type="submit"
              size="sm"
              disabled={queryMutation.isPending || !q.trim()}
            >
              {queryMutation.isPending ? "Querying…" : "Search"}
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Tags
            </label>
            {tagFilter.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => removeTag(t)}
                className="inline-flex items-center gap-1 rounded-md border border-border bg-accent/40 px-2 py-0.5 font-mono text-[10px] text-accent-foreground hover:bg-accent"
                aria-label={`Remove tag ${t}`}
              >
                #{t}
                <span aria-hidden>×</span>
              </button>
            ))}
            <input
              type="text"
              value={tagDraft}
              onChange={(e) => setTagDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addTag(tagDraft);
                } else if (e.key === "Backspace" && !tagDraft && tagFilter.length > 0) {
                  e.preventDefault();
                  setTagFilter(tagFilter.slice(0, -1));
                }
              }}
              placeholder="add tag..."
              className="h-7 rounded-md border border-input bg-transparent px-2 font-mono text-[11px] outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
            <p className="text-[10px] text-muted-foreground">
              (tag filter sent to server once the endpoint supports it)
            </p>
          </div>
        </form>

        {queryError ? (
          <p className="text-sm text-destructive">{queryError}</p>
        ) : null}

        {results ? (
          <div className="space-y-2">
            <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              {results.hits.length} hits · backend={results.backend}
            </div>
            {results.hits.length === 0 ? (
              <p className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
                no hits
              </p>
            ) : (
              <ul className="space-y-2">
                {results.hits.map((h) => (
                  <HitCard key={h.chunk_id} hit={h} maxScore={results.hits[0]!.score} />
                ))}
              </ul>
            )}
          </div>
        ) : null}
      </section>

      <section className="flex items-center justify-between rounded-lg border border-border bg-panel p-4">
        <div className="space-y-0.5">
          <div className="text-sm font-semibold">Rebuild FTS index</div>
          <p className="text-xs text-muted-foreground">
            Rescans `chunks_fts`. Safe but not instant on large corpora.
          </p>
        </div>
        <Button
          variant="destructive"
          onClick={handleRebuild}
          disabled={rebuildMutation.isPending || !stats.data?.ready}
          data-testid="rag-rebuild-btn"
        >
          {rebuildMutation.isPending ? "Rebuilding…" : "Rebuild"}
        </Button>
      </section>
    </>
  );
}

function RagStat({
  label,
  value,
  loading,
  icon,
}: {
  label: string;
  value: number | undefined;
  loading: boolean;
  icon: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border bg-panel p-4">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground">
        <span>{label}</span>
        <span className="text-muted-foreground/70">{icon}</span>
      </div>
      {loading ? (
        <Skeleton className="mt-2 h-7 w-16" />
      ) : (
        <div className="mt-1 font-mono text-2xl font-semibold tracking-tight">
          {value ?? 0}
        </div>
      )}
    </div>
  );
}

function HitCard({ hit, maxScore }: { hit: RagHit; maxScore: number }) {
  const pct = Math.max(4, Math.round((hit.score / Math.max(maxScore, 0.0001)) * 100));
  return (
    <li className="rounded-md border border-border bg-surface p-3">
      <div className="flex items-center justify-between gap-3 text-xs">
        <div className="flex items-center gap-2">
          <code className="font-mono text-muted-foreground">#{hit.chunk_id}</code>
          <span className="font-mono text-muted-foreground">
            {hit.score.toFixed(3)}
          </span>
        </div>
        <div className="h-1 flex-1 max-w-[40%] overflow-hidden rounded-full bg-muted">
          <div
            className={cn("h-full bg-primary")}
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">{hit.content_preview}</p>
    </li>
  );
}
