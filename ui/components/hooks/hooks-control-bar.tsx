"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { GlassPanel } from "@/components/ui/glass-panel";
import { StreamPill, type StreamState } from "@/components/ui/stream-pill";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { StatChip } from "@/components/ui/stat-chip";
import type { HookCategory } from "@/lib/hooks/use-mock-hook-stream";

/**
 * Hooks-page control bar — a single GlassPanel that houses the live/paused
 * StreamPill, four StatChips (events/min, subscribers, p50, p95) and the
 * category FilterChipGroup.
 *
 * Kept as a dumb component — all state lives on the page. Uses the same
 * `<FilterChipGroup>` primitive as the dashboard severity filter so the
 * chip styling stays in lockstep across the admin surface.
 */

export interface HooksControlBarProps {
  /** Live-stream state for the pill. */
  streamState: StreamState;
  /** Optional rate suffix, e.g. "41.2/s" / "0 ev/min". */
  streamRate?: string;
  onToggleStream: (current: StreamState) => void;

  /** Currently selected category filter. */
  category: HookCategory;
  onCategoryChange: (next: HookCategory) => void;
  /** Live per-category counts, driven by the page's ring buffer. */
  categoryCounts: Partial<Record<HookCategory, number>>;

  /** Primary StatChip — events per minute (derived from 60s rolling window). */
  eventsPerMin: number;
  /** Baked SVG sparkline for the events/min chip (same geometry as other pages). */
  eventsSparkPath: string;

  /** Total unique subscribers attending any kind in the buffer. */
  subscribers: number;
  /** Busiest event kind — used as the subscribers StatChip foot. */
  topKindLabel?: string;

  /** p50 dispatch latency across the buffer (ms). */
  p50Ms: number | null;
  /** p95 dispatch latency across the buffer (ms). */
  p95Ms: number | null;
}

/** Same baked geometry used on Approvals / Dashboard so the visual dialect
 * stays consistent. Width 300, height 36. */
const BAKED_SPARKS = {
  subscribers:
    "M0 26 L30 24 L60 22 L90 22 L120 20 L150 20 L180 18 L210 18 L240 16 L270 16 L300 14 L300 36 L0 36 Z",
  p50:
    "M0 22 L30 20 L60 22 L90 18 L120 20 L150 16 L180 18 L210 14 L240 16 L270 12 L300 14 L300 36 L0 36 Z",
  p95:
    "M0 14 L30 16 L60 12 L90 16 L120 10 L150 14 L180 8 L210 12 L240 6 L270 10 L300 4 L300 36 L0 36 Z",
} as const;

/** Canonical category order for the filter chip group. */
export const CATEGORY_ORDER: HookCategory[] = [
  "all",
  "message",
  "session",
  "agent",
  "lifecycle",
  "approval",
  "rate_limit",
  "tool",
  "config",
];

export function HooksControlBar({
  streamState,
  streamRate,
  onToggleStream,
  category,
  onCategoryChange,
  categoryCounts,
  eventsPerMin,
  eventsSparkPath,
  subscribers,
  topKindLabel,
  p50Ms,
  p95Ms,
}: HooksControlBarProps) {
  const { t } = useTranslation();

  const categoryOptions: FilterChipOption[] = React.useMemo(() => {
    return CATEGORY_ORDER.map((cat) => ({
      value: cat,
      label: t(`hooks.tp.cat.${cat}`),
      count: categoryCounts[cat] ?? 0,
      tone:
        cat === "approval" || cat === "rate_limit"
          ? ("warn" as const)
          : cat === "config"
            ? ("warn" as const)
            : cat === "lifecycle"
              ? ("ok" as const)
              : ("neutral" as const),
    }));
  }, [categoryCounts, t]);

  const epmValue =
    eventsPerMin >= 1
      ? eventsPerMin.toFixed(eventsPerMin >= 10 ? 0 : 1)
      : eventsPerMin.toFixed(2);

  return (
    <div className="flex flex-col gap-3.5">
      <GlassPanel
        as="section"
        variant="soft"
        className="flex flex-wrap items-center gap-2.5 p-3"
        aria-label={t("hooks.tp.controlBarAria")}
      >
        <StreamPill
          state={streamState}
          rate={streamRate}
          onToggle={onToggleStream}
        />
        <span
          className="ml-1 font-mono text-[11.5px] text-tp-ink-3"
          data-testid="hooks-stream-hint"
        >
          {t("hooks.tp.streamHint")}
        </span>
      </GlassPanel>

      {/* Stats strip — four glass tiles; first is primary. */}
      <section className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4">
        <StatChip
          variant="primary"
          live={streamState === "live"}
          label={t("hooks.tp.statEventsPerMin")}
          value={<span data-testid="stat-events-per-min">{epmValue}</span>}
          foot={t("hooks.tp.statEventsPerMinFoot")}
          sparkPath={eventsSparkPath}
          sparkTone="amber"
        />
        <StatChip
          label={t("hooks.tp.statSubscribers")}
          value={<span data-testid="stat-subscribers">{subscribers}</span>}
          foot={
            topKindLabel
              ? t("hooks.tp.statSubscribersFoot", { kind: topKindLabel })
              : t("hooks.tp.statSubscribersFootQuiet")
          }
          sparkPath={BAKED_SPARKS.subscribers}
          sparkTone="peach"
        />
        <StatChip
          label={t("hooks.tp.statP50")}
          value={
            <span data-testid="stat-p50">
              {p50Ms === null ? "—" : formatMs(p50Ms)}
            </span>
          }
          foot={t("hooks.tp.statLatencyFoot")}
          sparkPath={BAKED_SPARKS.p50}
          sparkTone="amber"
        />
        <StatChip
          label={t("hooks.tp.statP95")}
          value={
            <span data-testid="stat-p95">
              {p95Ms === null ? "—" : formatMs(p95Ms)}
            </span>
          }
          foot={t("hooks.tp.statLatencyFoot")}
          sparkPath={BAKED_SPARKS.p95}
          sparkTone="ember"
        />
      </section>

      {/* Category filter */}
      <FilterChipGroup
        label={t("hooks.tp.categoryAria")}
        options={categoryOptions}
        value={category}
        onChange={(v) => onCategoryChange(v as HookCategory)}
      />
    </div>
  );
}

function formatMs(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  if (ms < 1) return `${ms.toFixed(1)}ms`;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export default HooksControlBar;
