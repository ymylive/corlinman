"use client";

import * as React from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ArrowUpRight, Search } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  apiFetch,
  fetchHealth,
  fetchRagStats,
  listPendingApprovals,
  type AgentSummary,
  type HealthCheck,
  type HealthStatus,
  type PluginSummary,
  type RagStats,
} from "@/lib/api";
import { openEventStream } from "@/lib/sse";
import { useMotionVariants } from "@/lib/motion";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { LogRow, type LogSeverity } from "@/components/ui/log-row";
import { MiniSparkline, type SparkBar } from "@/components/ui/mini-sparkline";
import { UptimeStreak, type DayBar } from "@/components/admin/uptime-streak";
import { useCommandPalette } from "@/components/cmdk-palette";

/**
 * Dashboard — Tidepool cutover.
 *
 * Layout (1440w reference):
 *   ┌──────────────────────── hero (glass strong) ─────────────┐
 *   │ lead pill · greeting · prose summary with inline metrics │
 *   │ [⌘K]  [Review N approvals]      ┤  uptime streak │     │
 *   └───────────────────────────────────────────────────────────┘
 *   ┌ plugins ─┬ agents ─┬ rag chunks ─┬ pending appr ─┐
 *   │  primary │          │              │              │
 *   └──────────┴──────────┴──────────────┴──────────────┘
 *   ┌──── activity (glass soft, 1.4fr) ────┬── health (1fr) ──┐
 *   │ filter chips + LogRow list           │ UptimeStreak +   │
 *   │                                      │ per-service mini │
 *   │                                      │ sparkline rows   │
 *   └──────────────────────────────────────┴──────────────────┘
 *
 * All panes read-through the existing queries + SSE stream so a live
 * gateway paints real data; an offline gateway shows an empty-state
 * across the board (every query has `retry: false` so it fails fast).
 */

// ─── activity row shape (matches /admin/logs/stream SSE payload) ─────
interface LogEvent {
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  subsystem: string;
  trace_id: string;
  message: string;
}

type ActivityFilter = "all" | "ok" | "info" | "warn" | "err";

const RECENT_MAX = 40; // keep more in memory than we render, so filtering doesn't empty the feed

// ─── mock series — real metrics endpoint lands in a future phase ─────
// Each spark is a 10-segment path baked into the same `0 0 300 36` geometry
// as the Tidepool prototype. Shapes are deterministic so the SSR render
// matches the CSR hydration (no flash).
const PRIMARY_SPARK =
  "M0 28 L30 24 L60 26 L90 20 L120 22 L150 16 L180 18 L210 12 L240 14 L270 8 L300 10 L300 36 L0 36 Z";
const FLAT_SPARK =
  "M0 22 L30 22 L60 20 L90 22 L120 18 L150 20 L180 18 L210 20 L240 16 L270 18 L300 16 L300 36 L0 36 Z";
const ASCENDING_SPARK =
  "M0 28 L30 26 L60 24 L90 22 L120 20 L150 16 L180 14 L210 10 L240 8 L270 6 L300 4 L300 36 L0 36 Z";
const DESCENDING_SPARK =
  "M0 10 L30 14 L60 16 L90 20 L120 22 L150 24 L180 26 L210 28 L240 30 L270 30 L300 32 L300 36 L0 36 Z";

// ─── Page ────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const palette = useCommandPalette();

  // Core queries. Every one wraps with `.catch` via retry:false + isError.
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
  const approvals = useQuery({
    queryKey: ["admin", "approvals", "pending"],
    queryFn: () => listPendingApprovals(),
    refetchInterval: 30_000,
    retry: false,
  });

  // Recent activity — kept in a ring buffer. Filter is applied at render time.
  const [events, setEvents] = React.useState<LogEvent[]>([]);
  const [filter, setFilter] = React.useState<ActivityFilter>("all");

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

  // ── filter-chip counts ──────────────────────────────────────
  const counts = React.useMemo(() => {
    const c = { all: events.length, ok: 0, info: 0, warn: 0, err: 0 };
    for (const e of events) {
      if (e.level === "warn") c.warn += 1;
      else if (e.level === "error") c.err += 1;
      else if (e.level === "info") c.info += 1;
    }
    return c;
  }, [events]);

  const visibleEvents = React.useMemo(() => {
    if (filter === "all") return events.slice(0, 14);
    const want = filter === "err" ? "error" : filter;
    return events
      .filter((e) => {
        if (want === "ok") return false; // no ok-level in stream today
        return e.level === want;
      })
      .slice(0, 14);
  }, [events, filter]);

  // ── derived metrics for the hero summary ────────────────────
  const pluginsTotal = plugins.data?.length;
  const pluginsLoaded = plugins.data?.filter((p) => p.status === "loaded").length;
  const agentsCount = agents.data?.length;
  const ragChunks = rag.data?.chunks;
  const pendingApprovals = approvals.data?.length ?? 0;

  // "running clear" vs "attending" vs "busy" — derived from health + approvals
  const okChecks = health.data?.checks?.filter((c) => c.ok).length ?? 0;
  const totalChecks = health.data?.checks?.length ?? 0;
  const allNominal = totalChecks > 0 && okChecks === totalChecks;
  const statusWord = !allNominal
    ? t("dashboard.tp.attending")
    : pendingApprovals > 0
      ? t("dashboard.tp.waiting")
      : t("dashboard.tp.quiet");

  const onRunPalette = () => palette.setOpen(true);

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      {/* ─── HERO ──────────────────────────────────────────── */}
      <GlassPanel variant="strong" as="section" className="relative overflow-hidden p-8">
        {/* aurora glow blobs behind hero copy */}
        <div
          aria-hidden
          className="pointer-events-none absolute bottom-[-80px] right-[-40px] h-[280px] w-[420px] rounded-full opacity-70 blur-3xl"
          style={{
            background: "radial-gradient(closest-side, var(--tp-amber-glow), transparent 70%)",
          }}
        />
        <div
          aria-hidden
          className="pointer-events-none absolute top-[-60px] left-[-60px] h-[220px] w-[320px] rounded-full opacity-50 blur-[50px]"
          style={{
            background:
              "radial-gradient(closest-side, color-mix(in oklch, var(--tp-ember) 40%, transparent), transparent 70%)",
          }}
        />

        <div className="relative grid items-end gap-9 md:grid-cols-[1fr_auto]">
          <div className="flex min-w-0 flex-col gap-4">
            <HeroLead systemsOk={`${okChecks}/${totalChecks || 7}`} />
            <h1 className="text-balance font-sans text-[34px] font-semibold leading-[1.12] tracking-[-0.028em] text-tp-ink sm:text-[38px]">
              {t("dashboard.tp.greeting")}
              <br />
              {t("dashboard.tp.agentsAre")}{" "}
              <span className="bg-tp-grad-text bg-clip-text font-semibold text-transparent">
                {statusWord}
              </span>
              .
            </h1>
            <HeroSummary
              pluginsLoaded={pluginsLoaded}
              pluginsTotal={pluginsTotal}
              agentsCount={agentsCount}
              ragChunks={ragChunks}
              pendingApprovals={pendingApprovals}
              health={health.data}
              healthError={health.isError}
            />
            <div className="mt-1 flex flex-wrap items-center gap-2.5">
              <button
                type="button"
                onClick={onRunPalette}
                className="inline-flex items-center gap-2 rounded-lg bg-white/95 px-3 py-2 text-[13px] font-medium text-[#1a120d] shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_8px_16px_-8px_rgba(0,0,0,0.4)] transition-transform duration-200 hover:-translate-y-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40 data-[dark=false]:bg-tp-ink data-[dark=false]:text-white"
              >
                <Search className="h-3.5 w-3.5" />
                {t("dashboard.tp.ctaPalette")}
                <span className="ml-1 rounded bg-black/5 px-1.5 py-0.5 font-mono text-[10px] text-black/60">
                  ⌘K
                </span>
              </button>
              {pendingApprovals > 0 ? (
                <Link
                  href="/approvals"
                  className="inline-flex items-center gap-2 rounded-lg border border-tp-amber/35 bg-tp-amber-soft px-3 py-2 text-[13px] font-medium text-tp-amber hover:bg-[color-mix(in_oklch,var(--tp-amber)_22%,transparent)]"
                >
                  {t("dashboard.tp.ctaReview", { n: pendingApprovals })}
                  <ArrowUpRight className="h-3.5 w-3.5 opacity-70" />
                </Link>
              ) : (
                <Link
                  href="/logs"
                  className="inline-flex items-center gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[13px] font-medium text-tp-ink-2 hover:bg-tp-glass-inner-hover hover:text-tp-ink"
                >
                  {t("dashboard.tp.ctaLogs")}
                  <ArrowUpRight className="h-3.5 w-3.5 opacity-70" />
                </Link>
              )}
            </div>
          </div>

          <HeroUptime health={health.data} />
        </div>
      </GlassPanel>

      {/* ─── STAT CHIPS ─────────────────────────────────────── */}
      <motion.section
        className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4"
        variants={variants.stagger}
        initial="hidden"
        animate="visible"
      >
        <StatChip
          variant="primary"
          live
          label={t("dashboard.plugins")}
          value={
            plugins.isError || pluginsTotal === undefined ? "—" : pluginsTotal
          }
          delta={
            typeof pluginsLoaded === "number" && typeof pluginsTotal === "number"
              ? {
                  label: `${pluginsLoaded} / ${pluginsTotal}`,
                  tone: pluginsLoaded === pluginsTotal ? "up" : "flat",
                }
              : undefined
          }
          foot={
            plugins.isError
              ? t("dashboard.endpointOffline")
              : t("dashboard.pluginsLoaded", { n: pluginsLoaded ?? 0 })
          }
          sparkPath={PRIMARY_SPARK}
          sparkTone="amber"
        />
        <StatChip
          label={t("dashboard.agents")}
          value={agents.isError || agentsCount === undefined ? "—" : agentsCount}
          foot={
            agents.isError
              ? t("dashboard.endpointOffline")
              : t("dashboard.agentsHint")
          }
          sparkPath={FLAT_SPARK}
          sparkTone="ember"
        />
        <StatChip
          label={t("dashboard.ragChunks")}
          value={
            rag.isError || ragChunks === undefined
              ? "—"
              : formatNumber(ragChunks)
          }
          foot={
            rag.data
              ? t("dashboard.ragFilesTags", {
                  files: rag.data.files,
                  tags: rag.data.tags,
                })
              : rag.isError
                ? t("dashboard.endpointOffline")
                : t("dashboard.loadingHint")
          }
          sparkPath={ASCENDING_SPARK}
          sparkTone="peach"
        />
        <StatChip
          label={t("dashboard.tp.approvalsLabel")}
          value={approvals.isError ? "—" : pendingApprovals}
          delta={
            pendingApprovals > 0
              ? { label: t("dashboard.tp.awaiting"), tone: "flat" }
              : { label: t("dashboard.tp.caughtUp"), tone: "up" }
          }
          foot={
            approvals.isError
              ? t("dashboard.endpointOffline")
              : t("dashboard.tp.approvalsHint")
          }
          sparkPath={DESCENDING_SPARK}
          sparkTone="ember"
        />
      </motion.section>

      {/* ─── ACTIVITY + HEALTH ─────────────────────────────── */}
      <section className="grid grid-cols-1 gap-3.5 lg:grid-cols-[1.4fr_1fr]">
        <ActivityPane
          events={visibleEvents}
          counts={counts}
          filter={filter}
          onFilter={setFilter}
        />
        <HealthPane health={health.data} error={health.isError} />
      </section>
    </motion.div>
  );
}

// ─── Hero sub-components ─────────────────────────────────────────────

function HeroLead({ systemsOk }: { systemsOk: string }) {
  const { t } = useTranslation();
  return (
    <div className="inline-flex w-fit items-center gap-2.5 rounded-full border border-tp-glass-edge bg-tp-glass-inner-strong py-1 pl-2 pr-3 font-mono text-[11px] text-tp-ink-2">
      <span className="h-1.5 w-1.5 rounded-full bg-tp-amber tp-breathe-amber" />
      {t("dashboard.tp.leadPill", { systems: systemsOk })}
    </div>
  );
}

function HeroSummary({
  pluginsLoaded,
  pluginsTotal,
  agentsCount,
  ragChunks,
  pendingApprovals,
}: {
  pluginsLoaded: number | undefined;
  pluginsTotal: number | undefined;
  agentsCount: number | undefined;
  ragChunks: number | undefined;
  pendingApprovals: number;
  health: HealthStatus | undefined;
  healthError: boolean;
}) {
  const { t } = useTranslation();
  const pluginsPhrase =
    typeof pluginsLoaded === "number" && typeof pluginsTotal === "number"
      ? `${pluginsLoaded}/${pluginsTotal}`
      : "—";
  return (
    <p className="max-w-[64ch] text-[15px] leading-[1.6] text-tp-ink-2">
      {t("dashboard.tp.summaryLead", { agents: agentsCount ?? "—" })}{" "}
      <InlineMetric>{pluginsPhrase}</InlineMetric>{" "}
      {t("dashboard.tp.summaryPluginsSuffix")}{" "}
      <InlineMetric>
        {ragChunks === undefined ? "—" : formatNumber(ragChunks)}
      </InlineMetric>{" "}
      {t("dashboard.tp.summaryChunksSuffix")}
      {pendingApprovals > 0 ? (
        <>
          {" "}
          <InlineMetric tone="warn">
            {t("dashboard.tp.pendingApprovals", { n: pendingApprovals })}
          </InlineMetric>{" "}
          {t("dashboard.tp.summaryApprovalsSuffix")}
        </>
      ) : null}
    </p>
  );
}

function InlineMetric({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "warn";
}) {
  return (
    <span
      className={cn(
        "whitespace-nowrap rounded-md border px-1.5 py-px font-mono text-[13px] font-medium tabular-nums",
        tone === "warn"
          ? "border-tp-warn/30 bg-tp-warn-soft text-tp-warn"
          : "border-tp-glass-edge bg-tp-glass-inner-strong text-tp-ink",
      )}
    >
      {children}
    </span>
  );
}

function HeroUptime({ health }: { health: HealthStatus | undefined }) {
  const { t } = useTranslation();
  const ok = health?.checks?.filter((c) => c.ok).length ?? 0;
  const total = health?.checks?.length ?? 7;
  const pct = total === 0 ? "—" : ((ok / total) * 100).toFixed(0);
  return (
    <div className="min-w-[220px] rounded-2xl border border-tp-glass-edge bg-tp-glass-inner p-4">
      <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-tp-ink-4">
        {t("dashboard.tp.uptimeLabel")}
      </div>
      <div className="mt-2 font-serif text-[38px] font-normal leading-[1.05] tracking-[-0.02em] text-tp-ink">
        {pct}
        <span className="ml-0.5 font-sans text-[18px] font-normal text-tp-ink-3">
          %
        </span>
      </div>
      <div className="mt-2 flex items-center gap-2.5 text-[12px] text-tp-ink-3">
        <span>
          {ok}/{total} {t("dashboard.tp.uptimeNominal")}
        </span>
        <span className="relative h-1 flex-1 overflow-hidden rounded-full bg-tp-glass-inner-strong">
          <span
            className="absolute inset-y-0 left-0 rounded-full"
            style={{
              width: `${total === 0 ? 0 : (ok / total) * 100}%`,
              background: "linear-gradient(90deg, var(--tp-amber), var(--tp-ember))",
            }}
          />
        </span>
      </div>
    </div>
  );
}

// ─── Activity pane ───────────────────────────────────────────────────

function ActivityPane({
  events,
  counts,
  filter,
  onFilter,
}: {
  events: LogEvent[];
  counts: { all: number; ok: number; info: number; warn: number; err: number };
  filter: ActivityFilter;
  onFilter: (f: ActivityFilter) => void;
}) {
  const { t } = useTranslation();
  const options: FilterChipOption[] = [
    { value: "all", label: t("dashboard.tp.filterAll"), count: counts.all },
    { value: "info", label: "info", count: counts.info, tone: "info" },
    { value: "warn", label: "warn", count: counts.warn, tone: "warn" },
    { value: "err", label: "err", count: counts.err, tone: "err" },
  ];
  return (
    <GlassPanel variant="soft" className="flex min-h-[360px] flex-col p-5">
      <div className="flex items-center justify-between border-b border-tp-glass-edge pb-3">
        <div className="inline-flex items-center gap-2.5 text-[14px] font-semibold text-tp-ink">
          <span className="h-1.5 w-1.5 rounded-full bg-tp-ok tp-breathe" />
          {t("dashboard.recentActivity")}
        </div>
        <Link
          href="/logs"
          className="text-[12.5px] text-tp-ink-3 transition-colors hover:text-tp-ink"
        >
          {t("dashboard.viewAll")}
        </Link>
      </div>

      <div className="py-2.5">
        <FilterChipGroup
          options={options}
          value={filter}
          onChange={(next) => onFilter(next as ActivityFilter)}
          label={t("dashboard.tp.filterSeverity")}
        />
      </div>

      {events.length === 0 ? (
        <div className="flex flex-1 items-center justify-center p-8 text-center text-[13px] text-tp-ink-3">
          {t("dashboard.waitingForEvents")}
        </div>
      ) : (
        <div className="flex flex-1 flex-col">
          {events.map((e, i) => (
            <LogRow
              key={`${e.trace_id}-${e.ts}-${i}`}
              variant="comfortable"
              ts={safeTime(e.ts)}
              severity={mapSeverity(e.level)}
              subsystem={e.subsystem}
              message={e.message}
              justNow={i === 0}
            />
          ))}
        </div>
      )}
    </GlassPanel>
  );
}

function mapSeverity(level: LogEvent["level"]): LogSeverity {
  if (level === "error") return "err";
  if (level === "warn") return "warn";
  return "info";
}

function safeTime(iso: string): string {
  try {
    return iso.slice(11, 19);
  } catch {
    return "--:--:--";
  }
}

// ─── Health pane ─────────────────────────────────────────────────────

function HealthPane({
  health,
  error,
}: {
  health: HealthStatus | undefined;
  error: boolean;
}) {
  const { t } = useTranslation();
  const fallback: HealthCheck[] = [
    { name: "gateway", ok: !error },
    { name: "provider:anthropic", ok: !error },
    { name: "rag-store", ok: !error },
    { name: "plugin-runtime", ok: !error },
    { name: "scheduler", ok: !error },
    { name: "approvals", ok: !error },
    { name: "channels:qq", ok: !error },
  ];
  const checks =
    health?.checks && health.checks.length > 0 ? health.checks : fallback;
  const ok = checks.filter((c) => c.ok).length;
  const pct = checks.length === 0 ? "—" : ((ok / checks.length) * 100).toFixed(2);

  const uptimeBars: DayBar[] = React.useMemo(() => {
    // Synthetic 30-day bars until a real /admin/uptime endpoint exists. Shape
    // mirrors the ticker in HeroUptime so both panels read consistent.
    const arr: DayBar[] = [];
    for (let i = 0; i < 30; i++) {
      const tone: DayBar["tone"] =
        i === 16 && error ? "err" : i === 4 && error ? "warn" : "ok";
      arr.push({ height: tone === "ok" ? 90 + ((i * 7) % 10) : 50, tone });
    }
    return arr;
  }, [error]);

  return (
    <GlassPanel variant="soft" className="flex min-h-[360px] flex-col p-5">
      <div className="flex items-center justify-between border-b border-tp-glass-edge pb-3">
        <div className="text-[14px] font-semibold text-tp-ink">
          {t("dashboard.systemHealth")}
        </div>
        <div className="font-mono text-[11.5px] text-tp-ink-3">
          {ok} / {checks.length}
        </div>
      </div>

      <div className="py-4">
        <UptimeStreak
          pct={pct}
          bars={uptimeBars}
          incidentsText={
            error
              ? t("dashboard.tp.incidentsNow")
              : t("dashboard.tp.noIncidents")
          }
          label={t("dashboard.tp.availability90d")}
        />
      </div>

      <div className="flex flex-col">
        {checks.map((c) => {
          const bars = fabricateSpark(c.name, !!c.ok);
          return (
            <div
              key={c.name}
              className={cn(
                "grid grid-cols-[1fr_56px_auto] items-center gap-3 border-b border-tp-glass-edge py-2",
                "last:border-b-0 text-[12.5px]",
              )}
            >
              <span className="flex items-center gap-2 text-tp-ink-2">
                <span
                  aria-hidden
                  className={cn(
                    "h-1.5 w-1.5 rounded-full",
                    c.ok
                      ? "bg-tp-ok shadow-[0_0_4px_color-mix(in_oklch,var(--tp-ok)_30%,transparent)]"
                      : "bg-tp-err shadow-[0_0_4px_color-mix(in_oklch,var(--tp-err)_30%,transparent)]",
                  )}
                />
                <span className="truncate font-medium text-tp-ink">
                  {c.name}
                </span>
              </span>
              <MiniSparkline bars={bars} />
              <span className="text-right font-mono text-[10.5px] text-tp-ink-3">
                {c.detail ?? (c.ok ? "ok" : "—")}
              </span>
            </div>
          );
        })}
      </div>
    </GlassPanel>
  );
}

/** Deterministic 6-bar availability series seeded by service name. */
function fabricateSpark(name: string, ok: boolean): SparkBar[] {
  let x = 0;
  for (let i = 0; i < name.length; i++) x = (x * 31 + name.charCodeAt(i)) & 0xffff;
  const bars: SparkBar[] = [];
  for (let i = 0; i < 6; i++) {
    x = (x * 1103515245 + 12345) & 0x7fffffff;
    const base = 78 + (x % 22); // 78-100
    bars.push({ height: ok ? base : Math.max(40, base - 35), tone: ok ? "ok" : "err" });
  }
  return bars;
}

function formatNumber(n: number): string {
  if (n < 1000) return String(n);
  return n.toLocaleString();
}
