"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { useRouter, useSearchParams } from "next/navigation";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { EmptyState } from "@/components/ui/empty-state";
import { useMotion } from "@/components/ui/motion-safe";
import {
  useMockHookStream,
  kindCategory,
  type HookCategory,
  type HookEvent,
} from "@/lib/hooks/use-mock-hook-stream";
import type { StreamState } from "@/components/ui/stream-pill";
import { HooksControlBar } from "@/components/hooks/hooks-control-bar";
import {
  HookEventRow,
  deriveHookMetrics,
} from "@/components/hooks/hook-event-row";
import { HookDetailDrawer } from "@/components/hooks/hook-detail-drawer";
import {
  parseCategory,
  subscriberPanel,
} from "@/components/hooks/hooks-util";

/**
 * Hooks — Phase 5c Tidepool cutover.
 *
 * Layout:
 *   [ hero prose (glass strong) ]
 *   [ StreamPill · rate ]
 *   [ StatChip × 4: events/min · subscribers · p50 · p95 ]
 *   [ FilterChipGroup: all · message · session · agent · lifecycle · … ]
 *   [ event stream (glass soft)   │ HookDetailDrawer (when selected) ]
 *
 * Data flow: the page consumes `useMockHookStream()` (the existing mock
 * emitter) until Batch 5 ships `/admin/hooks/stream`. While *paused*,
 * incoming events are buffered into a side ring and surfaced as a
 * resume-to-view pill — we never drop events that arrive during a pause.
 *
 * Newest row gets `justNow` for 2.8s (the tp-just-now keyframe window).
 * Clicking a row selects it; clicking the same row again closes the drawer.
 * Esc also closes the drawer.
 */

type SelectedKey = string | null;

// Ring cap — the hook stream already caps at MAX_EVENTS=200 upstream, but we
// keep a local ceiling mirroring it so the paused-side buffer doesn't grow
// unbounded during long pauses.
const RING_MAX = 200;

// Single baked spark for the primary (events/min) chip so we don't recompute
// on every render. Same geometry as Approvals/Dashboard pending sparks.
const EVENTS_SPARK_PATH =
  "M0 30 L30 26 L60 22 L90 24 L120 20 L150 22 L180 14 L210 18 L240 12 L270 16 L300 10 L300 36 L0 36 Z";

export default function HooksPage() {
  const { t } = useTranslation();
  const { reduced } = useMotion();
  const router = useRouter();
  const searchParams = useSearchParams();

  // Live mock firehose — stays the source of truth until B5.
  const { events: liveEvents, connected, eps } = useMockHookStream();

  // ─── pause-aware buffer ────────────────────────────────
  const [paused, setPaused] = React.useState(false);
  const pausedRef = React.useRef(paused);
  pausedRef.current = paused;

  // Snapshot of events the user *has* seen. When paused we stop updating
  // this — but we *do* record any newly arrived events in a side ring so
  // we can drain them on resume.
  const [frozen, setFrozen] = React.useState<HookEvent[] | null>(null);
  const sideBufferRef = React.useRef<HookEvent[]>([]);
  const [pendingCount, setPendingCount] = React.useState(0);

  // Snapshot just once when the user pauses; unfreeze and drain on resume.
  React.useEffect(() => {
    if (paused) {
      setFrozen(liveEvents);
      sideBufferRef.current = [];
      setPendingCount(0);
    } else if (frozen !== null) {
      setFrozen(null);
      sideBufferRef.current = [];
      setPendingCount(0);
    }
    // `frozen` is intentionally dep-omitted — we want the snapshot to
    // freeze *at* the toggle edge, not any time `frozen` changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paused]);

  // While paused, accumulate newly-arrived events into the side ring so the
  // "N new · resume" pill has an accurate count.
  React.useEffect(() => {
    if (!paused || frozen === null) return;
    const frozenIds = new Set(frozen.map((e) => e.id));
    const fresh = liveEvents.filter((e) => !frozenIds.has(e.id));
    if (fresh.length === 0) return;
    sideBufferRef.current = fresh.slice(0, RING_MAX);
    setPendingCount(Math.min(fresh.length, RING_MAX));
    // liveEvents updates frequently; keep the effect cheap.
  }, [paused, frozen, liveEvents]);

  /** The list the user sees — frozen snapshot while paused, live otherwise. */
  const events = paused && frozen ? frozen : liveEvents;

  // ─── category (URL-persisted) ───────────────────────────
  const category = React.useMemo(
    () => parseCategory(searchParams?.get("cat") ?? null),
    [searchParams],
  );
  const setCategory = React.useCallback(
    (next: HookCategory) => {
      const params = new URLSearchParams(searchParams?.toString() ?? "");
      if (next === "all") params.delete("cat");
      else params.set("cat", next);
      const query = params.toString();
      router.replace(`/hooks${query ? `?${query}` : ""}`, { scroll: false });
    },
    [router, searchParams],
  );

  // ─── selection ──────────────────────────────────────────
  const [selectedId, setSelectedId] = React.useState<SelectedKey>(null);

  // Close drawer on Esc.
  React.useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape" && selectedId !== null) setSelectedId(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId]);

  // ─── just-now tracking ──────────────────────────────────
  // Mark the newest id seen each time the ring grows. The CSS keyframe
  // (2.8s) handles the visual fade — we only need to know "which id is the
  // new one" on each tick.
  const prevTopIdRef = React.useRef<string | null>(null);
  const [justNowId, setJustNowId] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (paused) return; // during pause we don't surface new highlights
    const top = events[0]?.id ?? null;
    if (top && top !== prevTopIdRef.current) {
      prevTopIdRef.current = top;
      if (!reduced) setJustNowId(top);
    }
  }, [events, paused, reduced]);
  React.useEffect(() => {
    if (!justNowId) return;
    const id = window.setTimeout(() => setJustNowId(null), 2800);
    return () => window.clearTimeout(id);
  }, [justNowId]);

  // ─── per-event metrics (derived, memoised by event id) ──
  // `deriveHookMetrics` is deterministic per id; memoising an id→metrics
  // map means we only recompute for ids we haven't seen yet.
  const metricsCacheRef = React.useRef<
    Map<string, { subscribers: number; latencyMs: number }>
  >(new Map());
  const getMetrics = React.useCallback((evt: HookEvent) => {
    const cache = metricsCacheRef.current;
    const hit = cache.get(evt.id);
    if (hit) return hit;
    const fresh = deriveHookMetrics(evt);
    cache.set(evt.id, fresh);
    return fresh;
  }, []);

  // ─── visible (post-filter) ──────────────────────────────
  const visibleEvents = React.useMemo(() => {
    if (category === "all") return events;
    return events.filter((e) => kindCategory(e.kind) === category);
  }, [events, category]);

  // ─── aggregates for the control bar ─────────────────────
  const categoryCounts = React.useMemo(() => {
    const out: Partial<Record<HookCategory, number>> = {};
    out.all = events.length;
    for (const e of events) {
      const c = kindCategory(e.kind);
      out[c] = (out[c] ?? 0) + 1;
    }
    return out;
  }, [events]);

  /** Events-per-minute across the ring's 60s window. Falls back to EPS when
   * the buffer doesn't cover a full minute. */
  const eventsPerMin = React.useMemo(() => {
    if (events.length === 0) return 0;
    const now = Date.now();
    const cutoff = now - 60_000;
    const n = events.filter((e) => e.ts >= cutoff).length;
    if (n > 0) return n;
    // Fallback — eps*60 so the chip still reflects the firehose rate when
    // the buffer is young.
    return eps * 60;
  }, [events, eps]);

  /** Aggregate subscribers + latencies across the visible ring. */
  const aggregates = React.useMemo(() => {
    if (events.length === 0) {
      return { subscribers: 0, p50: null, p95: null, topKind: null };
    }
    let totalSubs = 0;
    const latencies: number[] = [];
    const kindCounts = new Map<string, number>();
    for (const e of events) {
      const m = getMetrics(e);
      totalSubs += m.subscribers;
      latencies.push(m.latencyMs);
      kindCounts.set(e.kind, (kindCounts.get(e.kind) ?? 0) + 1);
    }
    latencies.sort((a, b) => a - b);
    const p = (q: number): number => {
      if (latencies.length === 0) return 0;
      const idx = Math.min(
        latencies.length - 1,
        Math.max(0, Math.floor(q * (latencies.length - 1))),
      );
      return latencies[idx]!;
    };
    // Average subscribers across the buffer is the honest summary — the raw
    // sum conflates time with fan-out.
    const avgSubs = totalSubs / events.length;
    let topKind: string | null = null;
    let topCount = -1;
    for (const [k, n] of kindCounts) {
      if (n > topCount) {
        topKind = k;
        topCount = n;
      }
    }
    return {
      subscribers: Math.round(avgSubs),
      p50: p(0.5),
      p95: p(0.95),
      topKind,
    };
  }, [events, getMetrics]);

  // ─── stream state + rate ────────────────────────────────
  const streamState: StreamState = !connected
    ? "throttled"
    : paused
      ? "paused"
      : "live";
  const streamRate = eps >= 1 ? `${eps.toFixed(1)}/s` : `${Math.round(eps * 60)} ev/min`;
  const onToggleStream = React.useCallback((_current?: StreamState) => {
    setPaused((p) => !p);
  }, []);

  // ─── selection helpers ──────────────────────────────────
  const selectedEvent = React.useMemo(() => {
    if (selectedId === null) return null;
    return events.find((e) => e.id === selectedId) ?? null;
  }, [events, selectedId]);

  const selectedMetrics = selectedEvent ? getMetrics(selectedEvent) : null;
  const subscriberNames = React.useMemo(
    () => (selectedEvent ? subscriberPanel(selectedEvent) : []),
    [selectedEvent],
  );

  // ─── hero prose ─────────────────────────────────────────
  const heroSubCount = categoryCounts.config ?? 0;
  const heroEventsPerMin = Math.round(eventsPerMin);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {/* Hero — glass strong, prose-quiet (mirrors Approvals hero pattern). */}
      <GlassPanel
        as="header"
        variant="strong"
        className="flex flex-col gap-3 p-5 sm:p-6"
      >
        <h1 className="font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-tp-ink sm:text-[32px]">
          {t("hooks.tp.heroTitle")}
        </h1>
        <p className="max-w-[62ch] text-[14px] leading-[1.6] text-tp-ink-2">
          <InlineMetric tone="amber">
            {t("hooks.tp.heroLeadN", { n: heroEventsPerMin })}
          </InlineMetric>
          <span className="ml-1">
            {t("hooks.tp.heroLeadAttend", { n: heroSubCount })}
          </span>
          <span className="ml-1 text-tp-ink-3">
            {t("hooks.tp.heroTier")}
          </span>
        </p>
      </GlassPanel>

      {/* Control bar (stream pill · stats · category filter). */}
      <HooksControlBar
        streamState={streamState}
        streamRate={streamRate}
        onToggleStream={onToggleStream}
        category={category}
        onCategoryChange={setCategory}
        categoryCounts={categoryCounts}
        eventsPerMin={eventsPerMin}
        eventsSparkPath={EVENTS_SPARK_PATH}
        subscribers={aggregates.subscribers}
        topKindLabel={aggregates.topKind ?? undefined}
        p50Ms={aggregates.p50}
        p95Ms={aggregates.p95}
      />

      {/* Paused + pending banner */}
      {paused && pendingCount > 0 ? (
        <button
          type="button"
          onClick={() => onToggleStream()}
          className={cn(
            "inline-flex w-fit items-center gap-2 self-center rounded-full border px-3 py-1",
            "bg-tp-amber-soft border-tp-amber/25 text-tp-amber",
            "font-mono text-[11.5px]",
            "hover:border-tp-amber/40",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
          data-testid="resume-pending-pill"
        >
          <span className="tabular-nums">
            {t("hooks.tp.pendingResume", { n: pendingCount })}
          </span>
        </button>
      ) : null}

      {/* Main grid — stream list + optional detail drawer */}
      <div
        className={cn(
          "grid min-h-0 flex-1 gap-3.5",
          selectedEvent
            ? "lg:grid-cols-[minmax(0,1fr)_380px]"
            : "lg:grid-cols-[minmax(0,1fr)]",
        )}
      >
        <GlassPanel
          variant="soft"
          className="flex min-h-[480px] flex-col overflow-hidden"
        >
          {/* column header (aria-hidden, decorative labels) */}
          <div
            aria-hidden
            className={cn(
              "grid items-center gap-3 border-b border-tp-glass-edge px-4 py-2.5",
              "grid-cols-[82px_170px_64px_minmax(0,1fr)_auto]",
              "font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4",
            )}
          >
            <span>{t("hooks.tp.colTime")}</span>
            <span>{t("hooks.tp.colKind")}</span>
            <span>{t("hooks.tp.colSubs")}</span>
            <span>{t("hooks.tp.colMessage")}</span>
            <span>{t("hooks.tp.colLatency")}</span>
          </div>

          <div
            role="log"
            aria-label={t("hooks.tp.streamAria")}
            aria-live="polite"
            className="relative min-h-0 flex-1 overflow-y-auto"
          >
            {visibleEvents.length === 0 ? (
              <div className="p-8">
                <EmptyState
                  title={
                    events.length === 0
                      ? t("hooks.tp.emptyTitle")
                      : t("hooks.tp.emptyFilterTitle")
                  }
                  description={
                    events.length === 0
                      ? t("hooks.tp.emptyBody")
                      : t("hooks.tp.emptyFilterBody")
                  }
                />
              </div>
            ) : (
              <ol className="flex flex-col">
                {visibleEvents.map((evt) => {
                  const metrics = getMetrics(evt);
                  return (
                    <li key={evt.id} className="contents">
                      <HookEventRow
                        event={evt}
                        subscribers={metrics.subscribers}
                        latencyMs={metrics.latencyMs}
                        justNow={evt.id === justNowId}
                        selected={evt.id === selectedId}
                        onClick={() =>
                          setSelectedId((prev) =>
                            prev === evt.id ? null : evt.id,
                          )
                        }
                      />
                    </li>
                  );
                })}
              </ol>
            )}
          </div>
        </GlassPanel>

        {selectedEvent && selectedMetrics ? (
          <HookDetailDrawer
            event={selectedEvent}
            subscribers={selectedMetrics.subscribers}
            latencyMs={selectedMetrics.latencyMs}
            subscriberNames={subscriberNames}
          />
        ) : null}
      </div>
    </div>
  );
}

// ─── local helpers ────────────────────────────────────────

function InlineMetric({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "amber" | "neutral";
}) {
  return (
    <span
      className={cn(
        "whitespace-nowrap rounded-md border px-1.5 py-px font-mono text-[12.5px] font-medium tabular-nums",
        tone === "amber"
          ? "border-tp-amber/30 bg-tp-amber-soft text-tp-amber"
          : "border-tp-glass-edge bg-tp-glass-inner-strong text-tp-ink",
      )}
    >
      {children}
    </span>
  );
}
