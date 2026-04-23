"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Search } from "lucide-react";

import { cn } from "@/lib/utils";
import { apiFetch, type PluginSummary } from "@/lib/api";
import { useMotionVariants } from "@/lib/motion";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { PluginsHeader } from "@/components/plugins/plugins-header";
import { PluginCard } from "@/components/plugins/plugin-card";

/**
 * Plugins admin page — Tidepool cutover.
 *
 * Layout (1440 reference):
 *   ┌─────────── glass-strong hero ───────────────┐
 *   │ lead pill · title · prose · refresh · ⌘K    │
 *   └──────────────────────────────────────────────┘
 *   [ StatChip × 4 — total · loaded · sandboxed · errored ]
 *   [ SearchInput ]  [ FilterChipGroup — all|loaded|sandboxed|error ]
 *   ┌ card grid — minmax(280px,1fr) ──────────────┐
 *   │ <PluginCard> × N (GlassPanel soft)          │
 *   └──────────────────────────────────────────────┘
 *
 * The gateway is usually offline in dev; every read-only query uses
 * `retry: false` so failure paints the offline variant immediately.
 *
 * The "sandboxed" count/filter is derived via `isSandboxed()` — today it
 * proxies through `plugin_type === "asynchronous"` since plugin summaries
 * don't carry an explicit sandbox flag. The detail page can still show
 * the real sandbox manifest block.
 */

const SPARK_TOTAL =
  "M0 28 L30 24 L60 26 L90 20 L120 22 L150 16 L180 18 L210 12 L240 14 L270 8 L300 10 L300 36 L0 36 Z";
const SPARK_LOADED =
  "M0 22 L30 22 L60 20 L90 22 L120 18 L150 20 L180 18 L210 20 L240 16 L270 18 L300 16 L300 36 L0 36 Z";
const SPARK_SANDBOXED =
  "M0 28 L30 26 L60 24 L90 22 L120 20 L150 16 L180 14 L210 10 L240 8 L270 6 L300 4 L300 36 L0 36 Z";
const SPARK_ERRORED =
  "M0 10 L30 14 L60 16 L90 20 L120 22 L150 24 L180 26 L210 28 L240 30 L270 30 L300 32 L300 36 L0 36 Z";

type FilterValue = "all" | "loaded" | "sandboxed" | "error";

function isSandboxed(p: PluginSummary): boolean {
  // Without an explicit sandbox flag on PluginSummary today, proxy via
  // plugin_type — async plugins are the docker-sandboxed runtime variants.
  return p.plugin_type === "asynchronous";
}

export default function PluginsPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const [search, setSearch] = React.useState("");
  const [filter, setFilter] = React.useState<FilterValue>("all");

  const query = useQuery<PluginSummary[]>({
    queryKey: ["admin", "plugins"],
    queryFn: () => apiFetch<PluginSummary[]>("/admin/plugins"),
    retry: false,
  });

  const plugins = query.data ?? [];
  const offline = query.isError;

  const counts = React.useMemo(() => {
    const c = { total: plugins.length, loaded: 0, sandboxed: 0, errored: 0 };
    for (const p of plugins) {
      if (p.status === "loaded") c.loaded += 1;
      if (p.status === "error") c.errored += 1;
      if (isSandboxed(p)) c.sandboxed += 1;
    }
    return c;
  }, [plugins]);

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return plugins.filter((p) => {
      if (filter === "loaded" && p.status !== "loaded") return false;
      if (filter === "error" && p.status !== "error") return false;
      if (filter === "sandboxed" && !isSandboxed(p)) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) ||
        p.description?.toLowerCase().includes(q) ||
        p.capabilities.some((c) => c.toLowerCase().includes(q))
      );
    });
  }, [plugins, search, filter]);

  const updatedLabel = React.useMemo(() => {
    const dataUpdatedAt = query.dataUpdatedAt;
    if (!dataUpdatedAt) return undefined;
    return formatRelative(new Date(dataUpdatedAt).toISOString(), t);
  }, [query.dataUpdatedAt, t]);

  const filterOptions: FilterChipOption[] = [
    { value: "all", label: t("plugins.tp.filterAll"), count: counts.total },
    {
      value: "loaded",
      label: t("plugins.tp.filterLoaded"),
      count: counts.loaded,
      tone: "ok",
    },
    {
      value: "sandboxed",
      label: t("plugins.tp.filterSandboxed"),
      count: counts.sandboxed,
      tone: "info",
    },
    {
      value: "error",
      label: t("plugins.tp.filterError"),
      count: counts.errored,
      tone: "err",
    },
  ];

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <PluginsHeader
        counts={offline ? undefined : counts}
        updatedLabel={updatedLabel}
        offline={offline}
        fetching={query.isFetching}
        onRefresh={() => query.refetch()}
      />

      {/* Stat chips row */}
      <motion.section
        className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4"
        variants={variants.stagger}
        initial="hidden"
        animate="visible"
      >
        <StatChip
          variant="primary"
          live={!offline}
          label={t("plugins.tp.statTotal")}
          value={offline ? "—" : counts.total}
          foot={offline ? t("dashboard.endpointOffline") : t("plugins.tp.statFootTotal")}
          sparkPath={SPARK_TOTAL}
          sparkTone="amber"
        />
        <StatChip
          label={t("plugins.tp.statLoaded")}
          value={offline ? "—" : counts.loaded}
          delta={
            !offline && counts.total > 0
              ? {
                  label: `${counts.loaded} / ${counts.total}`,
                  tone: counts.loaded === counts.total ? "up" : "flat",
                }
              : undefined
          }
          foot={offline ? t("dashboard.endpointOffline") : t("plugins.tp.statFootLoaded")}
          sparkPath={SPARK_LOADED}
          sparkTone="ember"
        />
        <StatChip
          label={t("plugins.tp.statSandboxed")}
          value={offline ? "—" : counts.sandboxed}
          foot={offline ? t("dashboard.endpointOffline") : t("plugins.tp.statFootSandboxed")}
          sparkPath={SPARK_SANDBOXED}
          sparkTone="peach"
        />
        <StatChip
          label={t("plugins.tp.statErrored")}
          value={offline ? "—" : counts.errored}
          delta={
            !offline
              ? counts.errored === 0
                ? { label: t("dashboard.tp.caughtUp"), tone: "up" }
                : { label: t("dashboard.tp.awaiting"), tone: "down" }
              : undefined
          }
          foot={offline ? t("dashboard.endpointOffline") : t("plugins.tp.statFootErrored")}
          sparkPath={SPARK_ERRORED}
          sparkTone="ember"
        />
      </motion.section>

      {/* Search + filter chips */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <label className="relative flex min-w-[220px] flex-1 items-center sm:max-w-[360px]">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tp-ink-4"
            aria-hidden
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("plugins.tp.searchPlaceholder")}
            aria-label={t("plugins.filterPlaceholder")}
            className="h-9 w-full rounded-lg border border-tp-glass-edge bg-tp-glass-inner pl-8 pr-3 text-[13px] text-tp-ink placeholder:text-tp-ink-4 transition-colors hover:bg-tp-glass-inner-hover focus:outline-none focus:ring-2 focus:ring-tp-amber/40"
          />
        </label>
        <FilterChipGroup
          options={filterOptions}
          value={filter}
          onChange={(next) => setFilter(next as FilterValue)}
          label={t("plugins.tp.filterLabel")}
        />
      </section>

      {/* Card grid / empty / loading / error */}
      {query.isPending ? (
        <CardGridSkeleton />
      ) : offline ? (
        <OfflineBlock message={(query.error as Error | undefined)?.message} />
      ) : filtered.length === 0 ? (
        <EmptyBlock hasAnyPlugins={plugins.length > 0} />
      ) : (
        <section
          aria-label={t("plugins.title")}
          className={cn(
            "grid gap-3",
            "grid-cols-[repeat(auto-fill,minmax(280px,1fr))]",
          )}
          data-testid="plugins-grid"
        >
          {filtered.map((p) => (
            <PluginCard
              key={p.name}
              plugin={p}
              lastTouchedLabel={formatRelative(p.last_touched_at, t)}
            />
          ))}
        </section>
      )}
    </motion.div>
  );
}

// ─── Sub-components ────────────────────────────────────────────────────

function CardGridSkeleton() {
  return (
    <section
      aria-hidden
      className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]"
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <GlassPanel
          key={i}
          variant="soft"
          className="flex h-[132px] flex-col gap-3 p-4"
        >
          <div className="h-4 w-2/3 rounded bg-tp-glass-inner-strong" />
          <div className="h-3 w-1/3 rounded bg-tp-glass-inner" />
          <div className="mt-auto flex gap-2">
            <div className="h-5 w-14 rounded-full bg-tp-glass-inner" />
            <div className="h-5 w-16 rounded-full bg-tp-glass-inner" />
          </div>
        </GlassPanel>
      ))}
    </section>
  );
}

function OfflineBlock({ message }: { message?: string }) {
  const { t } = useTranslation();
  // Raw HTML dumps (a 404 page body) add no signal and look broken. Only
  // show plain-text diagnostics; suppress anything that starts with `<`.
  const firstLine = message?.split(/\r?\n/).find((ln) => ln.trim().length > 0)?.trim();
  const isHtmlDump = firstLine?.startsWith("<");
  const short = isHtmlDump
    ? undefined
    : firstLine && firstLine.length > 180
      ? firstLine.slice(0, 180) + "…"
      : firstLine;
  return (
    <GlassPanel variant="soft" className="flex flex-col items-center gap-2 p-8 text-center">
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
        {t("plugins.tp.offlineTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t("plugins.tp.offlineHint")}
      </p>
      {short ? (
        <p
          className="max-w-full truncate font-mono text-[11px] text-tp-ink-4"
          title={message}
        >
          {short}
        </p>
      ) : null}
    </GlassPanel>
  );
}

function EmptyBlock({ hasAnyPlugins }: { hasAnyPlugins: boolean }) {
  const { t } = useTranslation();
  return (
    <GlassPanel variant="soft" className="flex flex-col items-center gap-2 p-8 text-center">
      <div className="text-[14px] font-medium text-tp-ink">
        {hasAnyPlugins ? t("plugins.tp.emptyTitle") : t("plugins.noneRegistered")}
      </div>
      {hasAnyPlugins ? (
        <p className="text-[13px] text-tp-ink-3">{t("plugins.tp.emptyHint")}</p>
      ) : null}
    </GlassPanel>
  );
}

// ─── Helpers ────────────────────────────────────────────────────────────

function formatRelative(
  iso: string,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  try {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const s = Math.round((now - then) / 1000);
    if (s < 60) return t("common.secondsAgo", { n: Math.max(s, 0) });
    if (s < 3600) return t("common.minutesAgo", { n: Math.round(s / 60) });
    if (s < 86400) return t("common.hoursAgo", { n: Math.round(s / 3600) });
    return t("common.daysAgo", { n: Math.round(s / 86400) });
  } catch {
    return iso;
  }
}
