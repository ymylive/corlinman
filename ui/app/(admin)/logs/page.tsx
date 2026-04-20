"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/components/providers";
import { openEventStream } from "@/lib/sse";

/**
 * Live log viewer — subscribes to SSE /admin/logs/stream served by
 * ui/mock/server.ts in dev; at M7 this will come from the gateway
 * with structured tracing events (plan §9).
 */

interface LogEvent {
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  subsystem: string;
  trace_id: string;
  message: string;
}

const RING_MAX = 500;

function levelVariant(level: LogEvent["level"]): {
  label: string;
  cls: string;
} {
  switch (level) {
    case "error":
      return { label: "ERROR", cls: "bg-destructive text-destructive-foreground" };
    case "warn":
      return {
        label: "WARN",
        cls: "bg-amber-500/20 text-amber-300 border-transparent",
      };
    case "info":
      return {
        label: "INFO",
        cls: "bg-sky-500/20 text-sky-300 border-transparent",
      };
    case "debug":
    default:
      return {
        label: "DEBUG",
        cls: "bg-muted text-muted-foreground border-transparent",
      };
  }
}

export default function LogsPage() {
  const { t } = useI18n();
  const [events, setEvents] = React.useState<LogEvent[]>([]);
  const [levelFilter, setLevelFilter] = React.useState<string>("all");
  const [subsystemFilter, setSubsystemFilter] = React.useState<string>("");
  const [paused, setPaused] = React.useState(false);
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
        // Inline fallback if NEXT_PUBLIC_MOCK_API_URL is unset. Keeps the page
        // useful even without the standalone mock server running.
        const id = setInterval(() => {
          push({
            event: "log",
            data: {
              ts: new Date().toISOString(),
              level: "info",
              subsystem: "gateway",
              trace_id: Math.random().toString(16).slice(2, 18),
              message: "inline mock tick (start ui/mock/server.ts for variety)",
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
    return events.filter((e) => {
      if (levelFilter !== "all" && e.level !== levelFilter) return false;
      if (subsystemFilter && !e.subsystem.includes(subsystemFilter)) return false;
      return true;
    });
  }, [events, levelFilter, subsystemFilter]);

  async function copyTrace(traceId: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(traceId);
      setCopiedId(traceId);
      setTimeout(() => setCopiedId((c) => (c === traceId ? null : c)), 1200);
    } catch {
      // clipboard may be unavailable (insecure origin); silently ignore
    }
  }

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">{t("nav.logs")}</h1>
        <p className="text-sm text-muted-foreground">
          SSE 流式结构化日志。ring buffer {RING_MAX} 行，支持 level / subsystem 过滤。
        </p>
      </header>

      <section className="flex flex-wrap items-end gap-3 rounded-lg border border-border p-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="level-filter">{t("logs.filter.level")}</Label>
          <select
            id="level-filter"
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={levelFilter}
            onChange={(e) => setLevelFilter(e.target.value)}
          >
            <option value="all">{t("logs.filter.all")}</option>
            <option value="debug">debug</option>
            <option value="info">info</option>
            <option value="warn">warn</option>
            <option value="error">error</option>
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="subsystem-filter">{t("logs.filter.subsystem")}</Label>
          <Input
            id="subsystem-filter"
            className="w-48"
            placeholder={subsystems[0] ?? "gateway"}
            value={subsystemFilter}
            onChange={(e) => setSubsystemFilter(e.target.value)}
          />
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button
            variant={paused ? "default" : "outline"}
            size="sm"
            onClick={() => setPaused((p) => !p)}
          >
            {paused ? t("logs.action.resume") : t("logs.action.pause")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setEvents([])}
            disabled={events.length === 0}
          >
            {t("logs.action.clear")}
          </Button>
          <span className="text-xs text-muted-foreground">
            {visible.length} / {events.length}
          </span>
        </div>
      </section>

      <section className="rounded-lg border border-border">
        <ul className="divide-y divide-border font-mono text-xs">
          {visible.length === 0 ? (
            <li className="p-4 text-center text-sm text-muted-foreground">
              {paused ? t("logs.state.paused") : t("state.loading")}
            </li>
          ) : (
            visible.map((e, i) => {
              const v = levelVariant(e.level);
              return (
                <li
                  key={`${e.trace_id}-${i}`}
                  className="flex items-start gap-3 px-3 py-2 hover:bg-muted/40"
                >
                  <span className="shrink-0 text-muted-foreground">
                    {e.ts.slice(11, 23)}
                  </span>
                  <Badge className={`shrink-0 ${v.cls}`}>{v.label}</Badge>
                  <span className="shrink-0 text-muted-foreground">
                    {e.subsystem}
                  </span>
                  <button
                    type="button"
                    onClick={() => copyTrace(e.trace_id)}
                    className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
                    title={t("logs.trace.copy")}
                  >
                    {copiedId === e.trace_id ? "✓" : e.trace_id.slice(0, 8)}
                  </button>
                  <span className="flex-1 whitespace-pre-wrap break-all text-foreground">
                    {e.message}
                  </span>
                </li>
              );
            })
          )}
        </ul>
      </section>
    </>
  );
}
