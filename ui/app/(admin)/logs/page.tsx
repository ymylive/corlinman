"use client";

import * as React from "react";
import { Pause, Play, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { openEventStream } from "@/lib/sse";

/**
 * Live log viewer — SSE /admin/logs/stream.
 *
 * Events are kept in a ring buffer (RING_MAX) and filtered client-side by
 * level / subsystem / substring. The stream pauses when `paused` is true.
 * Expanding a row reveals the structured fields as a tree.
 */

interface LogEvent {
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  subsystem: string;
  trace_id: string;
  message: string;
  [extra: string]: unknown;
}

const RING_MAX = 500;

function levelTone(level: LogEvent["level"]) {
  switch (level) {
    case "error":
      return "text-err bg-err/10 border-err/30";
    case "warn":
      return "text-warn bg-warn/10 border-warn/30";
    case "info":
      return "text-primary bg-primary/10 border-primary/30";
    case "debug":
    default:
      return "text-muted-foreground bg-muted border-border";
  }
}

export default function LogsPage() {
  const [events, setEvents] = React.useState<LogEvent[]>([]);
  const [levelFilter, setLevelFilter] = React.useState<string>("all");
  const [subsystemFilter, setSubsystemFilter] = React.useState<string>("");
  const [search, setSearch] = React.useState<string>("");
  const [paused, setPaused] = React.useState(false);
  const [expanded, setExpanded] = React.useState<Set<string>>(() => new Set());
  const [copiedId, setCopiedId] = React.useState<string | null>(null);
  const pausedRef = React.useRef(paused);
  pausedRef.current = paused;

  React.useEffect(() => {
    const close = openEventStream<LogEvent>("/admin/logs/stream", {
      events: ["log", "message"],
      onMessage: ({ data }) => {
        if (pausedRef.current) return;
        setEvents((prev) => {
          const next = [data, ...prev];
          if (next.length > RING_MAX) next.length = RING_MAX;
          return next;
        });
      },
      mock: (push) => {
        const id = setInterval(() => {
          push({
            event: "log",
            data: {
              ts: new Date().toISOString(),
              level: "info",
              subsystem: "gateway",
              trace_id: Math.random().toString(16).slice(2, 18),
              message: "inline mock tick",
            },
          });
        }, 1000);
        return () => clearInterval(id);
      },
    });
    return () => close();
  }, []);

  const subsystems = React.useMemo(() => {
    const s = new Set<string>();
    for (const e of events) s.add(e.subsystem);
    return Array.from(s).sort();
  }, [events]);

  const visible = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return events.filter((e) => {
      if (levelFilter !== "all" && e.level !== levelFilter) return false;
      if (subsystemFilter && !e.subsystem.includes(subsystemFilter))
        return false;
      if (q && !e.message.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [events, levelFilter, subsystemFilter, search]);

  const toggleExpand = (key: string) => {
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });
  };

  async function copyTrace(traceId: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(traceId);
      setCopiedId(traceId);
      setTimeout(() => setCopiedId((c) => (c === traceId ? null : c)), 1200);
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <div className="flex flex-1 flex-col space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Logs</h1>
        <p className="text-sm text-muted-foreground">
          SSE structured log stream · ring buffer {RING_MAX} · filter by
          level, subsystem, substring.
        </p>
      </header>

      <section className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-panel p-3">
        <select
          className="h-8 rounded-md border border-input bg-background px-2 font-mono text-xs outline-none focus-visible:ring-1 focus-visible:ring-ring"
          value={levelFilter}
          onChange={(e) => setLevelFilter(e.target.value)}
          aria-label="Level filter"
        >
          <option value="all">level: all</option>
          <option value="debug">debug</option>
          <option value="info">info</option>
          <option value="warn">warn</option>
          <option value="error">error</option>
        </select>
        <select
          className="h-8 rounded-md border border-input bg-background px-2 font-mono text-xs outline-none focus-visible:ring-1 focus-visible:ring-ring"
          value={subsystemFilter}
          onChange={(e) => setSubsystemFilter(e.target.value)}
          aria-label="Subsystem filter"
        >
          <option value="">subsystem: all</option>
          {subsystems.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <Input
          placeholder="search message..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="h-8 max-w-[320px] font-mono text-xs"
        />
        <div className="ml-auto flex items-center gap-2">
          <Button
            variant={paused ? "default" : "outline"}
            size="sm"
            onClick={() => setPaused((p) => !p)}
          >
            {paused ? (
              <Play className="h-3 w-3" />
            ) : (
              <Pause className="h-3 w-3" />
            )}
            {paused ? "Resume" : "Pause"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setEvents([])}
            disabled={events.length === 0}
          >
            <Trash2 className="h-3 w-3" />
            Clear
          </Button>
          <span className="font-mono text-[11px] text-muted-foreground">
            {visible.length} / {events.length}
          </span>
        </div>
      </section>

      <section className="flex-1 overflow-hidden rounded-lg border border-border bg-panel">
        <ul className="max-h-[70vh] divide-y divide-border overflow-auto font-mono text-xs">
          {visible.length === 0 ? (
            <li className="p-6 text-center text-sm text-muted-foreground">
              {paused ? "Stream paused." : "Waiting for events…"}
            </li>
          ) : (
            visible.map((e, i) => {
              const key = `${e.trace_id}-${i}`;
              const isExpanded = expanded.has(key);
              const extras = Object.entries(e).filter(
                ([k]) =>
                  !["ts", "level", "subsystem", "trace_id", "message"].includes(
                    k,
                  ),
              );
              return (
                <li
                  key={key}
                  className="transition-colors hover:bg-accent/20"
                >
                  <div
                    className="flex items-start gap-2 px-3 py-2 cursor-pointer"
                    role="button"
                    tabIndex={0}
                    onClick={() => extras.length > 0 && toggleExpand(key)}
                    onKeyDown={(ev) => {
                      if ((ev.key === "Enter" || ev.key === " ") && extras.length > 0) {
                        ev.preventDefault();
                        toggleExpand(key);
                      }
                    }}
                  >
                    <span className="shrink-0 text-muted-foreground">
                      {e.ts.slice(11, 23)}
                    </span>
                    <span
                      className={cn(
                        "shrink-0 rounded border px-1 text-[10px] font-semibold uppercase tracking-wider",
                        levelTone(e.level),
                      )}
                    >
                      {e.level}
                    </span>
                    <span className="shrink-0 text-muted-foreground">
                      {e.subsystem}
                    </span>
                    <button
                      type="button"
                      onClick={(ev) => {
                        ev.stopPropagation();
                        copyTrace(e.trace_id);
                      }}
                      className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                      title="Copy trace id"
                    >
                      {copiedId === e.trace_id ? "copied" : e.trace_id.slice(0, 8)}
                    </button>
                    <span className="flex-1 whitespace-pre-wrap break-all text-foreground">
                      {e.message}
                    </span>
                    {extras.length > 0 ? (
                      <span className="shrink-0 text-[10px] text-muted-foreground">
                        {isExpanded ? "▾" : "▸"} {extras.length}
                      </span>
                    ) : null}
                  </div>
                  {isExpanded && extras.length > 0 ? (
                    <pre className="overflow-auto bg-surface/60 px-10 py-2 text-[10px] text-muted-foreground">
                      {JSON.stringify(Object.fromEntries(extras), null, 2)}
                    </pre>
                  ) : null}
                </li>
              );
            })
          )}
        </ul>
      </section>
    </div>
  );
}
