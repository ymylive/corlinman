"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Wifi, WifiOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  fetchQqStatus,
  reconnectQq,
  updateQqKeywords,
  type QqStatus,
} from "@/lib/api";

/**
 * QQ channel admin. /admin/channels/qq/status · /keywords · /reconnect.
 *
 * Top: status card with the big connection light + last-seen.
 * Middle: per-group keyword chip editor (Enter adds, × removes).
 * Bottom: recent messages transcript.
 */
export default function QqChannelPage() {
  const qc = useQueryClient();
  const status = useQuery<QqStatus>({
    queryKey: ["admin", "channels", "qq"],
    queryFn: fetchQqStatus,
    refetchInterval: 10_000,
  });

  const [draft, setDraft] = React.useState<Record<string, string[]>>({});
  const [draftInit, setDraftInit] = React.useState(false);
  React.useEffect(() => {
    if (status.data && !draftInit) {
      setDraft(status.data.group_keywords ?? {});
      setDraftInit(true);
    }
  }, [status.data, draftInit]);

  const saveMutation = useMutation({
    mutationFn: (next: Record<string, string[]>) => updateQqKeywords(next),
    onSuccess: () => {
      toast.success("Keywords saved");
      qc.invalidateQueries({ queryKey: ["admin", "channels", "qq"] });
    },
    onError: (err) =>
      toast.error(`Save failed: ${err instanceof Error ? err.message : String(err)}`),
  });

  const reconnectMutation = useMutation({
    mutationFn: reconnectQq,
    onSuccess: () => toast.success("Reconnect requested"),
    onError: (err) =>
      toast.warning(err instanceof Error ? err.message : String(err)),
  });

  const addGroup = () => {
    const id = window.prompt("Enter QQ group id:");
    if (!id) return;
    if (draft[id]) return;
    setDraft({ ...draft, [id]: [] });
  };
  const removeGroup = (id: string) => {
    const next = { ...draft };
    delete next[id];
    setDraft(next);
  };

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">QQ Channel</h1>
        <p className="text-sm text-muted-foreground">
          `/admin/channels/qq/status` · `/keywords` · `/reconnect`. Runtime
          state depends on corlinman-channels exposure.
        </p>
      </header>

      {status.isPending ? (
        <Skeleton className="h-28 w-full" />
      ) : status.isError ? (
        <p className="text-sm text-destructive">
          load failed: {(status.error as Error).message}
        </p>
      ) : status.data ? (
        <ConnectionCard
          status={status.data}
          onReconnect={() => reconnectMutation.mutate()}
          reconnecting={reconnectMutation.isPending}
        />
      ) : null}

      {/* keyword editor */}
      <section className="space-y-3 rounded-lg border border-border bg-panel p-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold">Group keywords</h2>
            <p className="text-xs text-muted-foreground">
              Press Enter to add a keyword chip; × to remove.
            </p>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={addGroup}>
              + Group
            </Button>
            <Button
              size="sm"
              onClick={() => saveMutation.mutate(draft)}
              disabled={saveMutation.isPending}
              data-testid="qq-save-keywords-btn"
            >
              {saveMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </div>
        </div>
        {Object.keys(draft).length === 0 ? (
          <p className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
            No per-group overrides. Add one to bind the bot to a set of
            keywords per group.
          </p>
        ) : (
          <ul className="space-y-2">
            {Object.entries(draft)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([id, kws]) => (
                <GroupRow
                  key={id}
                  gid={id}
                  keywords={kws}
                  onChange={(next) => setDraft({ ...draft, [id]: next })}
                  onRemove={() => removeGroup(id)}
                />
              ))}
          </ul>
        )}
      </section>

      {/* recent messages */}
      <section className="rounded-lg border border-border bg-panel">
        <div className="border-b border-border px-4 py-3 text-sm font-semibold">
          Recent messages
        </div>
        <div className="max-h-[360px] overflow-auto p-4">
          {!status.data || status.data.recent_messages.length === 0 ? (
            <p className="text-center text-sm text-muted-foreground">
              No messages yet.
            </p>
          ) : (
            <ul className="space-y-2">
              {(status.data.recent_messages as Array<Record<string, unknown>>)
                .slice(0, 10)
                .map((m, i) => (
                  <MessageBubble key={i} msg={m} />
                ))}
            </ul>
          )}
        </div>
      </section>
    </>
  );
}

function ConnectionCard({
  status,
  onReconnect,
  reconnecting,
}: {
  status: QqStatus;
  onReconnect: () => void;
  reconnecting: boolean;
}) {
  const tone = !status.configured
    ? "muted"
    : !status.enabled
      ? "muted"
      : status.runtime === "connected"
        ? "ok"
        : status.runtime === "disconnected"
          ? "err"
          : "warn";
  const label = !status.configured
    ? "Not configured"
    : !status.enabled
      ? "Disabled"
      : status.runtime === "connected"
        ? "Connected"
        : status.runtime === "disconnected"
          ? "Disconnected"
          : "Unknown";
  return (
    <section className="grid grid-cols-1 gap-4 rounded-lg border border-border bg-panel p-4 md:grid-cols-[auto_1fr_auto]">
      <div className="flex items-center gap-3">
        <div
          className={cn(
            "relative flex h-12 w-12 items-center justify-center rounded-full",
            tone === "ok" && "bg-ok/15 text-ok",
            tone === "warn" && "bg-warn/15 text-warn",
            tone === "err" && "bg-err/15 text-err",
            tone === "muted" && "bg-muted text-muted-foreground",
          )}
        >
          {tone === "ok" ? (
            <>
              <Wifi className="h-5 w-5" />
              <span className="absolute right-0.5 top-0.5 h-2 w-2 animate-pulse rounded-full bg-ok" />
            </>
          ) : (
            <WifiOff className="h-5 w-5" />
          )}
        </div>
      </div>
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">{label}</span>
          <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            runtime={status.runtime}
          </span>
        </div>
        <div className="space-y-0.5 text-xs">
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground">ws_url</span>
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
              {status.ws_url ?? "(none)"}
            </code>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground">self_ids</span>
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
              [{status.self_ids.join(", ")}]
            </code>
          </div>
        </div>
      </div>
      <Button
        size="sm"
        variant="outline"
        onClick={onReconnect}
        disabled={!status.configured || reconnecting}
        data-testid="qq-reconnect-btn"
      >
        {reconnecting ? "Reconnecting..." : "Reconnect"}
      </Button>
    </section>
  );
}

function GroupRow({
  gid,
  keywords,
  onChange,
  onRemove,
}: {
  gid: string;
  keywords: string[];
  onChange: (next: string[]) => void;
  onRemove: () => void;
}) {
  const [draft, setDraft] = React.useState("");
  const add = (raw: string) => {
    const t = raw.trim();
    if (!t) return;
    if (keywords.includes(t)) return;
    onChange([...keywords, t]);
    setDraft("");
  };
  const remove = (t: string) => onChange(keywords.filter((x) => x !== t));
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface p-2">
      <code className="rounded bg-muted px-2 py-1 font-mono text-xs">{gid}</code>
      <div className="flex min-h-[28px] flex-1 flex-wrap items-center gap-1.5">
        {keywords.map((kw) => (
          <button
            key={kw}
            type="button"
            onClick={() => remove(kw)}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-accent/40 px-2 py-0.5 font-mono text-[10px] text-accent-foreground hover:bg-accent"
            aria-label={`Remove ${kw}`}
          >
            {kw} <span aria-hidden>×</span>
          </button>
        ))}
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add(draft);
            } else if (e.key === "Backspace" && !draft && keywords.length > 0) {
              e.preventDefault();
              remove(keywords[keywords.length - 1]!);
            }
          }}
          placeholder="add keyword..."
          className="h-7 max-w-[180px] border-0 bg-transparent px-1 text-xs shadow-none focus-visible:ring-0"
        />
      </div>
      <Button size="sm" variant="ghost" onClick={onRemove}>
        Remove
      </Button>
    </li>
  );
}

function MessageBubble({ msg }: { msg: Record<string, unknown> }) {
  const text =
    (msg.text as string | undefined) ??
    (msg.content as string | undefined) ??
    JSON.stringify(msg);
  const from =
    (msg.from as string | undefined) ??
    (msg.user_id as string | undefined) ??
    "unknown";
  const ts = (msg.ts as string | undefined) ?? (msg.time as string | undefined);
  return (
    <li className="flex items-start gap-3 rounded-md border border-border bg-surface p-3">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/15 font-mono text-[10px] font-semibold text-primary">
        {from.slice(0, 2).toUpperCase()}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-xs">
          <span className="font-mono">{from}</span>
          {ts ? <span className="text-muted-foreground">{ts}</span> : null}
        </div>
        <p className="mt-1 whitespace-pre-wrap break-words text-xs">{text}</p>
      </div>
    </li>
  );
}
