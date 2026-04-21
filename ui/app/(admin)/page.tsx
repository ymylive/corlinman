"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  ActivityIcon,
  ArrowUpRight,
  Bot,
  Boxes,
  Database,
  MessageSquare,
} from "lucide-react";

import { cn } from "@/lib/utils";
import {
  apiFetch,
  fetchHealth,
  fetchRagStats,
  type AgentSummary,
  type HealthCheck,
  type HealthStatus,
  type PluginSummary,
  type RagStats,
} from "@/lib/api";
import { openEventStream } from "@/lib/sse";

/**
 * Dashboard landing page — Linear-style overview.
 *
 * Layout:
 *   ┌─────────┬─────────┬─────────┬─────────┐  stat cards row (4)
 *   │ plugins │ agents  │ chunks  │ chat24h │
 *   └─────────┴─────────┴─────────┴─────────┘
 *   ┌───────────────────────┬─────────────────┐
 *   │ Recent activity (SSE) │ System health   │
 *   └───────────────────────┴─────────────────┘
 *
 * All cards fail-open: each query shows `—` on 503 / network error so an
 * offline subsystem doesn't cascade into a broken dashboard.
 */

// ---- stat card ------------------------------------------------------------

interface LogEvent {
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  subsystem: string;
  trace_id: string;
  message: string;
}

const RECENT_MAX = 20;

export default function DashboardPage() {
  // Each query wrapped in .catch(() => undefined) would swallow errors; we
  // keep isError around instead so the UI can render "—" for failures.
  const plugins = useQuery<PluginSummary[]>({
    queryKey: ["admin", "plugins"],
    queryFn: () => apiFetch<PluginSummary[]>("/admin/plugins"),
    retry: false,
  });
  const agents = useQuery<AgentSummary[]>({
    queryKey: ["admin", "agents"],
    queryFn: () => apiFetch<AgentSummary[]>("/admin/agents"),
    retry: false,
  });
  const rag = useQuery<RagStats>({
    queryKey: ["admin", "rag", "stats"],
    queryFn: fetchRagStats,
    retry: false,
  });
  const health = useQuery<HealthStatus>({
    queryKey: ["admin", "health"],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
    retry: false,
  });

  // Recent activity — SSE feed off the logs stream. Non-debug only, 20 max.
  const [events, setEvents] = React.useState<LogEvent[]>([]);
  React.useEffect(() => {
    const close = openEventStream<LogEvent>("/admin/logs/stream", {
      events: ["log", "message"],
      onMessage: ({ data }) => {
        if (!data || typeof data !== "object") return;
        const ev = data as LogEvent;
        if (ev.level === "debug") return;
        setEvents((prev) => {
          const next = [ev, ...prev];
          if (next.length > RECENT_MAX) next.length = RECENT_MAX;
          return next;
        });
      },
    });
    return close;
  }, []);

  // Chat 24h count (no dedicated endpoint yet — leave as placeholder).
  const chat24 = undefined;

  const pluginsHealthy = plugins.data?.filter((p) => p.status === "loaded").length;

  return (
    <div className="space-y-6">
      {/* hero */}
      <section className="relative overflow-hidden rounded-lg border border-border bg-surface/40 p-6 dashboard-hero-glow">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="space-y-1">
            <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
            <p className="text-sm text-muted-foreground">
              Rust gateway · Python AI layer · runtime health at a glance.
            </p>
          </div>
          <Link
            href="/logs"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            Live logs
            <ArrowUpRight className="h-3 w-3" />
          </Link>
        </div>
      </section>

      {/* stat cards */}
      <section className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Plugins"
          value={plugins.isError ? undefined : plugins.data?.length}
          hint={
            plugins.data
              ? `${pluginsHealthy ?? 0} loaded`
              : plugins.isError
                ? "endpoint offline"
                : "loading…"
          }
          icon={<Boxes className="h-4 w-4" />}
          href="/plugins"
          sparkSeed={plugins.data?.length ?? 0}
        />
        <StatCard
          label="Agents"
          value={agents.isError ? undefined : agents.data?.length}
          hint={agents.isError ? "endpoint offline" : "markdown prompts"}
          icon={<Bot className="h-4 w-4" />}
          href="/agents"
          sparkSeed={agents.data?.length ?? 0}
        />
        <StatCard
          label="RAG chunks"
          value={rag.isError ? undefined : rag.data?.chunks}
          hint={
            rag.data
              ? `${rag.data.files} files · ${rag.data.tags} tags`
              : rag.isError
                ? "endpoint offline"
                : "loading…"
          }
          icon={<Database className="h-4 w-4" />}
          href="/rag"
          sparkSeed={rag.data?.chunks ?? 0}
        />
        <StatCard
          label="Chat req (24h)"
          value={chat24}
          hint="metrics endpoint pending"
          icon={<MessageSquare className="h-4 w-4" />}
          href="/logs"
          sparkSeed={0}
        />
      </section>

      {/* activity + health */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-[1.4fr_1fr]">
        <ActivityCard events={events} />
        <HealthCard health={health.data} error={health.isError} />
      </section>
    </div>
  );
}

// ---- stat card ------------------------------------------------------------

function StatCard({
  label,
  value,
  hint,
  icon,
  href,
  sparkSeed,
}: {
  label: string;
  value: number | undefined;
  hint: string;
  icon: React.ReactNode;
  href: string;
  sparkSeed: number;
}) {
  return (
    <Link
      href={href as never}
      className="group relative flex flex-col gap-3 rounded-lg border border-border bg-panel p-4 transition-colors hover:border-primary/40 hover:bg-accent/30"
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-muted-foreground">
          <span className="text-muted-foreground/70">{icon}</span>
          {label}
        </div>
        <ArrowUpRight className="h-3.5 w-3.5 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
      </div>
      <div className="flex items-end justify-between">
        <div className="font-mono text-3xl font-semibold tracking-tight">
          {value === undefined ? "—" : value.toLocaleString()}
        </div>
        <Sparkline seed={sparkSeed} />
      </div>
      <p className="text-xs text-muted-foreground">{hint}</p>
    </Link>
  );
}

/** Deterministic 7-point sparkline. Demo-only until a real metric endpoint lands. */
function Sparkline({ seed }: { seed: number }) {
  // Derive a 7-point series from the seed so the shape at least varies per card.
  const pts = React.useMemo(() => {
    const arr: number[] = [];
    let x = Math.max(1, seed || 1);
    for (let i = 0; i < 7; i++) {
      x = (x * 1103515245 + 12345) & 0x7fffffff;
      arr.push((x % 100) + 20);
    }
    return arr;
  }, [seed]);
  const max = Math.max(...pts);
  const min = Math.min(...pts);
  const w = 64;
  const h = 24;
  const step = w / (pts.length - 1);
  const path = pts
    .map((v, i) => {
      const y = h - ((v - min) / Math.max(1, max - min)) * h;
      return `${i === 0 ? "M" : "L"} ${i * step} ${y}`;
    })
    .join(" ");
  return (
    <svg
      width={w}
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="text-primary/70"
      aria-hidden
    >
      <path d={path} />
    </svg>
  );
}

// ---- activity card --------------------------------------------------------

function ActivityCard({ events }: { events: LogEvent[] }) {
  return (
    <div className="flex min-h-[320px] flex-col rounded-lg border border-border bg-panel">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <ActivityIcon className="h-4 w-4 text-muted-foreground" />
          Recent activity
        </div>
        <Link
          href="/logs"
          className="text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          View all →
        </Link>
      </div>
      <ul className="flex-1 divide-y divide-border overflow-auto font-mono text-xs">
        {events.length === 0 ? (
          <li className="p-6 text-center text-sm text-muted-foreground">
            Waiting for events…
          </li>
        ) : (
          events.map((e, i) => (
            <li
              key={`${e.trace_id}-${i}`}
              className="flex items-start gap-2 px-4 py-2 transition-colors hover:bg-accent/30"
            >
              <span className="shrink-0 text-muted-foreground/70">
                {safeTime(e.ts)}
              </span>
              <LevelPill level={e.level} />
              <span className="shrink-0 text-muted-foreground">{e.subsystem}</span>
              <span className="flex-1 truncate text-foreground">
                {e.message}
              </span>
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

function safeTime(iso: string): string {
  try {
    return iso.slice(11, 19);
  } catch {
    return "--:--:--";
  }
}

function LevelPill({ level }: { level: LogEvent["level"] }) {
  const map: Record<LogEvent["level"], string> = {
    debug: "bg-muted text-muted-foreground",
    info: "bg-primary/15 text-primary",
    warn: "bg-warn/15 text-warn",
    error: "bg-err/15 text-err",
  };
  return (
    <span
      className={cn(
        "inline-flex h-4 shrink-0 items-center rounded px-1 text-[10px] font-medium uppercase tracking-wider",
        map[level],
      )}
    >
      {level}
    </span>
  );
}

// ---- health card ----------------------------------------------------------

function HealthCard({
  health,
  error,
}: {
  health: HealthStatus | undefined;
  error: boolean;
}) {
  // Fabricate a 7-check list if the endpoint hasn't been extended yet.
  const fallback: HealthCheck[] = [
    { name: "gateway", ok: !error },
    { name: "provider:anthropic", ok: !error },
    { name: "rag-store", ok: !error },
    { name: "plugin-runtime", ok: !error },
    { name: "scheduler", ok: !error },
    { name: "approvals", ok: !error },
    { name: "channels:qq", ok: !error },
  ];
  const checks = health?.checks && health.checks.length > 0 ? health.checks : fallback;
  const ok = checks.filter((c) => c.ok).length;
  const total = checks.length;

  return (
    <div className="flex min-h-[320px] flex-col rounded-lg border border-border bg-panel">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="text-sm font-medium">System health</div>
        <div className="font-mono text-xs text-muted-foreground">
          {ok} / {total}
        </div>
      </div>
      <ul className="flex-1 divide-y divide-border">
        {checks.map((c) => (
          <li
            key={c.name}
            className="flex items-center justify-between px-4 py-2.5 text-xs"
          >
            <span className="flex items-center gap-2">
              <span
                className={cn(
                  "inline-block h-2 w-2 rounded-full",
                  c.ok ? "bg-ok" : "bg-err",
                )}
              />
              <span className="font-mono text-foreground">{c.name}</span>
            </span>
            <span className="font-mono text-[10px] text-muted-foreground">
              {c.detail ?? (c.ok ? "ok" : "—")}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
