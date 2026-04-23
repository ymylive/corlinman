"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { useVirtualizer } from "@tanstack/react-virtual";

import { cn } from "@/lib/utils";
import { openEventStream } from "@/lib/sse";
import { useMotion } from "@/components/ui/motion-safe";
import { GlassPanel } from "@/components/ui/glass-panel";
import { LogRow } from "@/components/ui/log-row";
import { EmptyState } from "@/components/ui/empty-state";
import {
  LogsControlBar,
  RANGE_MS,
  type TimeRange,
  type SeverityOption,
  type SubsystemOption,
} from "@/components/logs/logs-control-bar";
import { LogStatsStrip } from "@/components/logs/log-stats-strip";
import {
  LogDetailDrawer,
  formatTsShort,
  renderMessageWithCode,
  severityFromLevel,
  type DetailLogEvent,
} from "@/components/logs/log-detail-drawer";
import type { StreamState } from "@/components/ui/stream-pill";

/**
 * Logs — Phase 4 Tidepool cutover.
 *
 * Layout (1440w):
 *   ┌──────────── control bar (glass soft) ────────────┐
 *   │ StreamPill · [15m·1h·24h·7d] · sev ▾ · sub ▾ ·   │
 *   │ search ⌘F · clear · export · settings            │
 *   └──────────────────────────────────────────────────┘
 *   [ stats strip · N events · ok · info · warn · err ]
 *   ┌─── log pane ──────┬─── detail (380px) ──┐
 *   │ grid-head row     │ severity pill · ts · ago │
 *   │ virtualised rows  │ subsystem · h2 · trace   │
 *   │ just-now + select │ Payload · Related · …    │
 *   │ day dividers      │                          │
 *   └───────────────────┴──────────────────────────┘
 *
 * Data flow: SSE `/admin/logs/stream` feeds a ring buffer (RING_MAX).
 * Filters (time range, severity, subsystem set, search) are applied at
 * render time. While paused, incoming events accumulate in a *side*
 * buffer (`pendingRef`) and surface as a "N new · resume" pill so we
 * don't lose events but also don't scroll the table out from under
 * the user.
 */

// ─── types ──────────────────────────────────────────────────────────

type LogEvent = DetailLogEvent;

type Severity = "all" | "ok" | "info" | "warn" | "err";

/** Client-side ring ceiling. Stays identical to the previous (pre-cutover) page
 * so QA comparing backend-facing behaviour can't tell this is a different UI. */
const RING_MAX = 500;

/** Virtualise once we cross this many visible rows. Below the threshold we
 * render flat (no scroll-jitter across tiny lists). */
const VIRTUAL_THRESHOLD = 80;

/** Dense row estimate — matches LogRow variant="dense" (py-2 + 12.5px text). */
const ROW_HEIGHT = 38;
const ROW_OVERSCAN = 8;

/** Retain newest first in the ring. */
function pushRing(buf: LogEvent[], ev: LogEvent): LogEvent[] {
  const next = [ev, ...buf];
  if (next.length > RING_MAX) next.length = RING_MAX;
  return next;
}

/** Compact ISO → `HH:mm` for day-divider labels. */
function minuteKey(iso: string): string {
  return iso.slice(0, 16); // yyyy-MM-ddTHH:mm
}

// ─── page ───────────────────────────────────────────────────────────

export default function LogsPage() {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  // live ring (newest-first) + side buffer of events received while paused
  const [events, setEvents] = React.useState<LogEvent[]>([]);
  const pendingRef = React.useRef<LogEvent[]>([]);
  const [pendingCount, setPendingCount] = React.useState(0);

  // stream state
  const [paused, setPaused] = React.useState(false);
  const pausedRef = React.useRef(paused);
  pausedRef.current = paused;

  // naive per-second arrival-rate readout (clean idle → "0 ev/min")
  const [streamRate, setStreamRate] = React.useState<string>("0 ev/min");
  const rateWindowRef = React.useRef<{ ts: number; n: number }[]>([]);

  // filters
  const [timeRange, setTimeRange] = React.useState<TimeRange>("24h");
  const [severity, setSeverity] = React.useState<Severity>("all");
  const [selectedSubs, setSelectedSubs] = React.useState<string[]>([]);
  const [search, setSearch] = React.useState("");

  // selection (key of a row that owns the drawer)
  const [selectedKey, setSelectedKey] = React.useState<string | null>(null);

  // first-row just-now flag on newly arrived events — one id wide,
  // auto-expires after 2.8s (the same window as the CSS keyframe).
  const [justNowKey, setJustNowKey] = React.useState<string | null>(null);

  // search input ref for ⌘F
  const searchInputRef = React.useRef<HTMLInputElement | null>(null);

  // ─── SSE hookup ─────────────────────────────────────────────
  React.useEffect(() => {
    const close = openEventStream<LogEvent>("/admin/logs/stream", {
      events: ["log", "message"],
      onMessage: ({ data }) => {
        if (!data || typeof data !== "object") return;
        // record arrival for the rate indicator regardless of pause
        const now = Date.now();
        rateWindowRef.current.push({ ts: now, n: 1 });
        if (pausedRef.current) {
          // accumulate; do not touch the table
          pendingRef.current = [data, ...pendingRef.current];
          if (pendingRef.current.length > RING_MAX) {
            pendingRef.current.length = RING_MAX;
          }
          setPendingCount(pendingRef.current.length);
          return;
        }
        const key = rowKey(data);
        setEvents((prev) => pushRing(prev, data));
        if (!reduced) setJustNowKey(key);
      },
    });
    return close;
    // `reduced` is read at subscribe time — re-subscribing on reduced-motion
    // change is unnecessary. Intentionally dep-empty.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Clear just-now after the animation window; keyed on its value so a new
  // event resets the timer cleanly.
  React.useEffect(() => {
    if (!justNowKey) return;
    const id = window.setTimeout(() => setJustNowKey(null), 2800);
    return () => window.clearTimeout(id);
  }, [justNowKey]);

  // ── arrival-rate indicator (updates every 2s, 10s window) ──
  React.useEffect(() => {
    const id = window.setInterval(() => {
      const now = Date.now();
      const WINDOW_MS = 10_000;
      rateWindowRef.current = rateWindowRef.current.filter(
        (s) => now - s.ts <= WINDOW_MS,
      );
      const n = rateWindowRef.current.length;
      if (n === 0) setStreamRate("0 ev/min");
      else if (n / (WINDOW_MS / 1000) >= 1) {
        setStreamRate(`${(n / (WINDOW_MS / 1000)).toFixed(1)}/s`);
      } else {
        setStreamRate(`${Math.round(n * (60_000 / WINDOW_MS))} ev/min`);
      }
    }, 2_000);
    return () => window.clearInterval(id);
  }, []);

  // ⌘F focuses the search input; ⌘K is handled app-level via CommandPalette
  React.useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const meta = ev.metaKey || ev.ctrlKey;
      if (meta && (ev.key === "f" || ev.key === "F")) {
        ev.preventDefault();
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
      }
      if (ev.key === "Escape" && selectedKey !== null) {
        setSelectedKey(null);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedKey]);

  // ─── derived: counts + filtered visible rows ─────────────

  const rangeFloor = React.useMemo(() => {
    if (timeRange === "custom") return 0;
    return Date.now() - RANGE_MS[timeRange];
  }, [timeRange]);

  /** Rows inside the current time-range window + subsystem filter.
   * Severity + search apply on top, but counts need the pre-severity
   * slice so chips report the right numbers. */
  const rangedBySubsystem = React.useMemo(() => {
    const subSet = selectedSubs.length > 0 ? new Set(selectedSubs) : null;
    return events.filter((e) => {
      if (rangeFloor > 0) {
        const ts = Date.parse(e.ts);
        if (Number.isFinite(ts) && ts < rangeFloor) return false;
      }
      if (subSet && !subSet.has(e.subsystem)) return false;
      return true;
    });
  }, [events, rangeFloor, selectedSubs]);

  const counts = React.useMemo(() => {
    const c = { all: 0, ok: 0, info: 0, warn: 0, err: 0 };
    for (const e of rangedBySubsystem) {
      c.all += 1;
      if (e.level === "error") c.err += 1;
      else if (e.level === "warn") c.warn += 1;
      else if (e.level === "info") c.info += 1;
      // debug/trace roll into "info" for our 4-bucket display (rare)
    }
    return c;
  }, [rangedBySubsystem]);

  const { searchMatcher, searchValid } = React.useMemo(
    () => compileSearch(search),
    [search],
  );

  /** Visible rows after severity + search are applied. */
  const visible = React.useMemo(() => {
    return rangedBySubsystem.filter((e) => {
      if (severity === "err" && e.level !== "error") return false;
      if (severity === "warn" && e.level !== "warn") return false;
      if (severity === "info" && e.level !== "info") return false;
      if (severity === "ok" && e.level !== "info") {
        // No "ok" level in the backend vocabulary yet — keep the chip but
        // treat it as info for now; Phase 5 will plumb a dedicated success
        // level. This mirrors the Dashboard's ActivityPane.
        return false;
      }
      if (searchValid && search.trim().length > 0 && !searchMatcher(e)) {
        return false;
      }
      return true;
    });
  }, [rangedBySubsystem, severity, search, searchMatcher, searchValid]);

  // ── metadata for control bar + stats strip ──
  const subsystemOptions: SubsystemOption[] = React.useMemo(() => {
    const acc = new Map<string, number>();
    for (const e of rangedBySubsystem) {
      acc.set(e.subsystem, (acc.get(e.subsystem) ?? 0) + 1);
    }
    return Array.from(acc.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([value, count]) => ({ value, count }));
  }, [rangedBySubsystem]);

  const severityOptions: SeverityOption[] = [
    {
      value: "all",
      label: t("logs.tp.sevAll"),
      count: counts.all,
      tone: "neutral",
    },
    { value: "info", label: t("logs.tp.sevInfo"), count: counts.info, tone: "info" },
    { value: "warn", label: t("logs.tp.sevWarn"), count: counts.warn, tone: "warn" },
    { value: "err", label: t("logs.tp.sevErr"), count: counts.err, tone: "err" },
  ];

  const uniqueTraceIds = React.useMemo(() => {
    const s = new Set<string>();
    for (const e of visible) s.add(e.trace_id);
    return s.size;
  }, [visible]);

  const selectedEvent = React.useMemo(() => {
    if (selectedKey === null) return null;
    return events.find((e) => rowKey(e) === selectedKey) ?? null;
  }, [events, selectedKey]);

  const relatedEvents = React.useMemo(() => {
    if (!selectedEvent) return [];
    return events.filter(
      (e) =>
        e !== selectedEvent &&
        e.trace_id === selectedEvent.trace_id &&
        e.trace_id !== "",
    );
  }, [events, selectedEvent]);

  // ── stream state for the pill ──
  const streamState: StreamState = paused ? "paused" : "live";

  const onToggleStream = React.useCallback(() => {
    setPaused((p) => {
      const next = !p;
      if (!next) {
        // resuming — drain the pending buffer into the ring
        const drain = pendingRef.current;
        pendingRef.current = [];
        setPendingCount(0);
        if (drain.length > 0) {
          setEvents((prev) => {
            // Merge drain (newest-first already) on top of prev, capping RING_MAX
            const merged = [...drain, ...prev];
            if (merged.length > RING_MAX) merged.length = RING_MAX;
            return merged;
          });
        }
      }
      return next;
    });
  }, []);

  const onClear = React.useCallback(() => {
    setEvents([]);
    pendingRef.current = [];
    setPendingCount(0);
    setSelectedKey(null);
  }, []);

  // ── virtualiser: build a flattened row list with dividers upfront ──
  type Item =
    | { kind: "divider"; id: string; label: string }
    | { kind: "row"; id: string; event: LogEvent };

  const items: Item[] = React.useMemo(() => {
    const out: Item[] = [];
    let lastMinute: string | null = null;
    for (const e of visible) {
      const mk = minuteKey(e.ts);
      if (mk !== lastMinute) {
        out.push({
          kind: "divider",
          id: `div-${mk}`,
          label: dividerLabel(e.ts),
        });
        lastMinute = mk;
      }
      out.push({ kind: "row", id: rowKey(e), event: e });
    }
    return out;
  }, [visible]);

  const useVirtual = items.length >= VIRTUAL_THRESHOLD;
  const scrollParentRef = React.useRef<HTMLDivElement | null>(null);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollParentRef.current,
    estimateSize: (i) => (items[i]?.kind === "divider" ? 24 : ROW_HEIGHT),
    overscan: ROW_OVERSCAN,
  });

  const rangeReadout = `${visible.length.toLocaleString()}/${events.length.toLocaleString()}`;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {/* ─── Control bar ────────────────────────────── */}
      <LogsControlBar
        streamState={streamState}
        streamRate={streamRate}
        onToggleStream={onToggleStream}
        timeRange={timeRange}
        onTimeRangeChange={setTimeRange}
        severity={severity}
        severityOptions={severityOptions}
        onSeverityChange={(v) => setSeverity(v as Severity)}
        subsystems={subsystemOptions}
        selectedSubsystems={selectedSubs}
        onSubsystemsChange={setSelectedSubs}
        search={search}
        onSearchChange={setSearch}
        searchInputRef={searchInputRef}
        onClear={onClear}
        canClear={events.length > 0}
        rangeReadout={rangeReadout}
      />

      {/* ─── Stats strip ────────────────────────────── */}
      <LogStatsStrip
        total={counts.all}
        ok={0}
        info={counts.info}
        warn={counts.warn}
        err={counts.err}
        subsystems={subsystemOptions.length}
        traceIds={uniqueTraceIds}
      />

      {/* ─── Resume-on-new banner ───────────────────── */}
      {paused && pendingCount > 0 ? (
        <button
          type="button"
          onClick={onToggleStream}
          className={cn(
            "mx-4 inline-flex w-fit items-center gap-2 self-center rounded-full border px-3 py-1",
            "bg-tp-amber-soft border-tp-amber/25 text-tp-amber",
            "font-mono text-[11.5px]",
            "hover:bg-tp-amber-soft hover:border-tp-amber/40",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
        >
          <span className="tabular-nums">
            {t("logs.tp.pendingResume", { n: pendingCount })}
          </span>
        </button>
      ) : null}

      {/* ─── Main grid: log pane + detail drawer ────── */}
      <div
        className={cn(
          "grid min-h-0 flex-1 gap-3",
          selectedEvent
            ? "grid-cols-[minmax(0,1fr)_380px]"
            : "grid-cols-[minmax(0,1fr)]",
        )}
      >
        {/* Log pane */}
        <GlassPanel variant="soft" className="flex min-h-[560px] flex-col overflow-hidden">
          {/* Column header */}
          <div
            className={cn(
              "grid items-center gap-3 border-b border-tp-glass-edge px-4 py-2.5",
              "grid-cols-[70px_56px_140px_1fr_auto]",
              "font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4",
            )}
            aria-hidden
          >
            <span>{t("logs.tp.colTime")}</span>
            <span>{t("logs.tp.colSev")}</span>
            <span>{t("logs.tp.colSubsystem")}</span>
            <span>{t("logs.tp.colMessage")}</span>
            <span>{t("logs.tp.colDur")}</span>
          </div>

          {/* Rows */}
          <div
            ref={scrollParentRef}
            className="relative min-h-0 flex-1 overflow-y-auto"
            role="log"
            aria-label={t("logs.tp.streamAria")}
            aria-live="polite"
          >
            {items.length === 0 ? (
              <div className="p-8">
                <EmptyState
                  title={
                    paused
                      ? t("logs.paused")
                      : t("logs.waitingForEvents")
                  }
                />
              </div>
            ) : useVirtual ? (
              <div
                style={{
                  height: virtualizer.getTotalSize(),
                  width: "100%",
                  position: "relative",
                }}
              >
                {virtualizer.getVirtualItems().map((v) => {
                  const item = items[v.index];
                  if (!item) return null;
                  return (
                    <div
                      key={item.id}
                      ref={virtualizer.measureElement}
                      data-index={v.index}
                      style={{
                        position: "absolute",
                        top: 0,
                        left: 0,
                        width: "100%",
                        transform: `translateY(${v.start}px)`,
                      }}
                    >
                      {item.kind === "divider" ? (
                        <DayDivider label={item.label} />
                      ) : (
                        <RenderRow
                          item={item}
                          selected={item.id === selectedKey}
                          justNow={item.id === justNowKey}
                          onSelect={() =>
                            setSelectedKey((prev) =>
                              prev === item.id ? null : item.id,
                            )
                          }
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="flex flex-col">
                {items.map((item) =>
                  item.kind === "divider" ? (
                    <DayDivider key={item.id} label={item.label} />
                  ) : (
                    <RenderRow
                      key={item.id}
                      item={item}
                      selected={item.id === selectedKey}
                      justNow={item.id === justNowKey}
                      onSelect={() =>
                        setSelectedKey((prev) =>
                          prev === item.id ? null : item.id,
                        )
                      }
                    />
                  ),
                )}
              </div>
            )}
          </div>
        </GlassPanel>

        {/* Detail drawer */}
        {selectedEvent ? (
          <LogDetailDrawer
            event={selectedEvent}
            related={relatedEvents}
            likelyCause={likelyCauseFor(selectedEvent, t)}
          />
        ) : null}
      </div>
    </div>
  );
}

// ─── sub-components ─────────────────────────────────────────────────

function DayDivider({ label }: { label: string }) {
  return (
    <div
      className={cn(
        "border-y border-tp-glass-edge bg-tp-glass-inner px-4 py-1.5",
        "font-mono text-[10px] uppercase tracking-[0.1em] text-tp-ink-4",
      )}
    >
      {label}
    </div>
  );
}

function RenderRow({
  item,
  selected,
  justNow,
  onSelect,
}: {
  item: { kind: "row"; id: string; event: LogEvent };
  selected: boolean;
  justNow: boolean;
  onSelect: () => void;
}) {
  const e = item.event;
  return (
    <LogRow
      variant="dense"
      ts={formatTsShort(e.ts)}
      severity={severityFromLevel(e.level)}
      subsystem={e.subsystem}
      message={renderMessageWithCode(e.message)}
      duration={inferDuration(e)}
      selected={selected}
      justNow={justNow}
      onClick={onSelect}
      aria-selected={selected}
    />
  );
}

// ─── helpers ────────────────────────────────────────────────────────

function rowKey(e: LogEvent): string {
  // Trace alone collides in bursts; include ts (ms precision) + first 40 chars
  // of the message so adjacent events on the same trace still get distinct keys.
  return `${e.ts}|${e.trace_id}|${e.message.slice(0, 40)}`;
}

/** Extract a displayable duration from known extra fields: `duration_ms`,
 * `duration`, `dur_ms`. Falls back to em-dash when absent. */
function inferDuration(e: LogEvent): string {
  const candidates = ["duration_ms", "duration", "dur_ms", "elapsed_ms"];
  for (const k of candidates) {
    const v = e[k];
    if (typeof v === "number" && Number.isFinite(v)) {
      if (v < 1000) return `${v.toFixed(v < 10 ? 1 : 0)}ms`;
      return `${(v / 1000).toFixed(1)}s`;
    }
    if (typeof v === "string" && /\d/.test(v)) return v;
  }
  return "—";
}

/** `Today · 14:02` / `Apr 22 · 14:02` label based on ISO ts. */
function dividerLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const today = new Date();
  const isToday =
    d.getFullYear() === today.getFullYear() &&
    d.getMonth() === today.getMonth() &&
    d.getDate() === today.getDate();
  if (isToday) return `Today · ${hh}:${mm}`;
  const month = d.toLocaleString(undefined, { month: "short" });
  return `${month} ${d.getDate()} · ${hh}:${mm}`;
}

/** Compile the search box's text into a predicate.
 * `/pattern/flags` is regex; plain text is case-insensitive substring.
 * Returns a safe no-op predicate + `searchValid=false` on malformed regex. */
function compileSearch(raw: string): {
  searchMatcher: (e: LogEvent) => boolean;
  searchValid: boolean;
} {
  const q = raw.trim();
  if (q === "") {
    return { searchMatcher: () => true, searchValid: true };
  }
  const rm = /^\/(.+)\/([gimsuy]*)$/.exec(q);
  if (rm) {
    try {
      const re = new RegExp(rm[1], rm[2]);
      return {
        searchMatcher: (e) =>
          re.test(e.message) ||
          re.test(e.subsystem) ||
          re.test(e.trace_id),
        searchValid: true,
      };
    } catch {
      return { searchMatcher: () => true, searchValid: false };
    }
  }
  const needle = q.toLowerCase();
  return {
    searchMatcher: (e) =>
      e.message.toLowerCase().includes(needle) ||
      e.subsystem.toLowerCase().includes(needle) ||
      e.trace_id.toLowerCase().includes(needle),
    searchValid: true,
  };
}

/** Static "likely cause" hints for well-known error patterns. Phase 5 will
 * swap this for LLM-authored explanations. */
function likelyCauseFor(
  e: LogEvent,
  t: (key: string, opts?: Record<string, unknown>) => string,
): React.ReactNode | undefined {
  if (e.level !== "error") return undefined;
  const msg = e.message.toLowerCase();
  if (msg.includes("403") || msg.includes("forbidden")) {
    return t("logs.tp.cause403");
  }
  if (msg.includes("timeout") || msg.includes("timed out")) {
    return t("logs.tp.causeTimeout");
  }
  if (msg.includes("refused") || msg.includes("econnrefused")) {
    return t("logs.tp.causeRefused");
  }
  return undefined;
}
