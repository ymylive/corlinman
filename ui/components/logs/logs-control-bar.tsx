"use client";

import * as React from "react";
import { Calendar, Download, Search, Settings2, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StreamPill, type StreamState } from "@/components/ui/stream-pill";

/**
 * Logs control bar — pairs the live-stream pill with time-range chips,
 * severity + subsystem select triggers, the free-text search input, and
 * two icon actions on the right.
 *
 * This is a *dumb* bar: it delegates all state to the page. Popover
 * bodies for severity/subsystem selects render inline via disclosure
 * blocks — we skip Radix popover to keep the DOM light on pages that
 * already stack many glass surfaces.
 */

export type TimeRange = "15m" | "1h" | "24h" | "7d" | "custom";

export const TIME_RANGES: TimeRange[] = ["15m", "1h", "24h", "7d"];

/** Window durations in ms per time-range key. `custom` opts out — no client filter. */
export const RANGE_MS: Record<Exclude<TimeRange, "custom">, number> = {
  "15m": 15 * 60_000,
  "1h": 60 * 60_000,
  "24h": 24 * 60 * 60_000,
  "7d": 7 * 24 * 60 * 60_000,
};

export interface SeverityOption {
  value: string;
  label: string;
  count: number;
  tone: "ok" | "warn" | "err" | "info" | "neutral";
}

export interface SubsystemOption {
  value: string;
  count: number;
}

export interface LogsControlBarProps {
  streamState: StreamState;
  streamRate?: string;
  onToggleStream: (current: StreamState) => void;

  timeRange: TimeRange;
  onTimeRangeChange: (r: TimeRange) => void;

  severity: string;
  severityOptions: SeverityOption[];
  onSeverityChange: (v: string) => void;

  subsystems: SubsystemOption[];
  selectedSubsystems: string[];
  onSubsystemsChange: (next: string[]) => void;

  search: string;
  onSearchChange: (v: string) => void;
  /** Ref exposed so parent can wire ⌘F to focus(). */
  searchInputRef: React.RefObject<HTMLInputElement | null>;

  onClear: () => void;
  canClear: boolean;
  /** Counts visible/total, e.g. "32/500". */
  rangeReadout?: string;
}

const toneDot: Record<SeverityOption["tone"], string> = {
  ok: "bg-tp-ok",
  warn: "bg-tp-warn",
  err: "bg-tp-err",
  info: "bg-tp-ink-4",
  neutral: "",
};

export function LogsControlBar({
  streamState,
  streamRate,
  onToggleStream,
  timeRange,
  onTimeRangeChange,
  severity,
  severityOptions,
  onSeverityChange,
  subsystems,
  selectedSubsystems,
  onSubsystemsChange,
  search,
  onSearchChange,
  searchInputRef,
  onClear,
  canClear,
  rangeReadout,
}: LogsControlBarProps) {
  const { t } = useTranslation();
  const [severityOpen, setSeverityOpen] = React.useState(false);
  const [subsysOpen, setSubsysOpen] = React.useState(false);
  const severityRef = React.useRef<HTMLDivElement>(null);
  const subsysRef = React.useRef<HTMLDivElement>(null);

  // Close popovers on outside click — keep DOM simple, no Radix portal.
  React.useEffect(() => {
    if (!severityOpen && !subsysOpen) return;
    function onClick(ev: MouseEvent) {
      const tgt = ev.target as Node;
      if (severityRef.current && !severityRef.current.contains(tgt)) {
        setSeverityOpen(false);
      }
      if (subsysRef.current && !subsysRef.current.contains(tgt)) {
        setSubsysOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [severityOpen, subsysOpen]);

  const allSubsSelected =
    subsystems.length > 0 &&
    selectedSubsystems.length === 0; // empty = all (default)
  const subsysSummary = allSubsSelected
    ? t("logs.tp.subAll")
    : t("logs.tp.subSelected", { n: selectedSubsystems.length });

  const activeSeverity = severityOptions.find((o) => o.value === severity);

  return (
    <GlassPanel
      as="section"
      variant="soft"
      className="flex flex-wrap items-center gap-2.5 p-3"
      aria-label={t("logs.tp.controlBarAria")}
    >
      <StreamPill
        state={streamState}
        rate={streamRate}
        onToggle={onToggleStream}
      />

      {/* Time-range toggle */}
      <div
        role="tablist"
        aria-label={t("logs.tp.timeRangeAria")}
        className="inline-flex gap-0.5 rounded-lg border border-tp-glass-edge bg-tp-glass-inner p-0.5"
      >
        {TIME_RANGES.map((r) => (
          <button
            key={r}
            role="tab"
            aria-selected={timeRange === r}
            data-active={timeRange === r || undefined}
            onClick={() => onTimeRangeChange(r)}
            className={cn(
              "rounded-md px-2.5 py-1 font-mono text-[11.5px] transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
              timeRange === r
                ? "bg-tp-glass-inner-hover text-tp-ink"
                : "text-tp-ink-3 hover:text-tp-ink-2 hover:bg-tp-glass-inner",
            )}
          >
            {r}
          </button>
        ))}
        <button
          role="tab"
          aria-selected={timeRange === "custom"}
          data-active={timeRange === "custom" || undefined}
          onClick={() => onTimeRangeChange("custom")}
          className={cn(
            "inline-flex items-center gap-1 rounded-md px-2.5 py-1 font-mono text-[11.5px] transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
            timeRange === "custom"
              ? "bg-tp-glass-inner-hover text-tp-ink"
              : "text-tp-ink-3 hover:text-tp-ink-2 hover:bg-tp-glass-inner",
          )}
        >
          <Calendar className="h-3 w-3" />
          {t("logs.tp.timeCustom")}
        </button>
      </div>

      {/* Severity select */}
      <div ref={severityRef} className="relative">
        <button
          type="button"
          aria-haspopup="listbox"
          aria-expanded={severityOpen}
          onClick={() => setSeverityOpen((v) => !v)}
          className={cn(
            "inline-flex items-center gap-2 rounded-lg border px-3 py-[5px]",
            "bg-tp-glass-inner border-tp-glass-edge text-tp-ink-2 text-[12.5px]",
            "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
        >
          <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
            {t("logs.tp.severityLabel")}
          </span>
          <span className="font-medium text-tp-ink">
            {activeSeverity?.label ?? severity}
          </span>
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            className="h-2.5 w-2.5 text-tp-ink-4"
            aria-hidden
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>

        {severityOpen ? (
          <div
            role="listbox"
            aria-label={t("logs.tp.severityAria")}
            className={cn(
              "absolute left-0 top-[calc(100%+6px)] z-20 min-w-[180px]",
              "rounded-lg border border-tp-glass-edge bg-tp-glass-2 p-1 shadow-tp-panel",
              "backdrop-blur-glass backdrop-saturate-glass",
            )}
          >
            {severityOptions.map((opt) => (
              <button
                key={opt.value}
                role="option"
                aria-selected={severity === opt.value}
                onClick={() => {
                  onSeverityChange(opt.value);
                  setSeverityOpen(false);
                }}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left",
                  "text-[12.5px] transition-colors",
                  severity === opt.value
                    ? "bg-tp-glass-inner-hover text-tp-ink"
                    : "text-tp-ink-2 hover:bg-tp-glass-inner hover:text-tp-ink",
                )}
              >
                {opt.tone !== "neutral" ? (
                  <span
                    aria-hidden
                    className={cn(
                      "h-[6px] w-[6px] rounded-full",
                      toneDot[opt.tone],
                    )}
                  />
                ) : (
                  <span aria-hidden className="h-[6px] w-[6px]" />
                )}
                <span className="flex-1 font-mono">{opt.label}</span>
                <span className="font-mono text-[10.5px] tabular-nums text-tp-ink-4">
                  {opt.count}
                </span>
              </button>
            ))}
          </div>
        ) : null}
      </div>

      {/* Subsystem multi-select */}
      <div ref={subsysRef} className="relative">
        <button
          type="button"
          aria-haspopup="listbox"
          aria-expanded={subsysOpen}
          onClick={() => setSubsysOpen((v) => !v)}
          className={cn(
            "inline-flex items-center gap-2 rounded-lg border px-3 py-[5px]",
            "bg-tp-glass-inner border-tp-glass-edge text-tp-ink-2 text-[12.5px]",
            "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
        >
          <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
            {t("logs.tp.subsystemLabel")}
          </span>
          <span className="font-medium text-tp-ink">{subsysSummary}</span>
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            className="h-2.5 w-2.5 text-tp-ink-4"
            aria-hidden
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>

        {subsysOpen ? (
          <div
            role="listbox"
            aria-label={t("logs.tp.subsystemAria")}
            className={cn(
              "absolute left-0 top-[calc(100%+6px)] z-20 min-w-[240px] max-h-[260px] overflow-y-auto",
              "rounded-lg border border-tp-glass-edge bg-tp-glass-2 p-1 shadow-tp-panel",
              "backdrop-blur-glass backdrop-saturate-glass",
            )}
          >
            {subsystems.length === 0 ? (
              <div className="px-2.5 py-2 font-mono text-[11.5px] text-tp-ink-4">
                {t("logs.tp.subsystemEmpty")}
              </div>
            ) : (
              <>
                <button
                  type="button"
                  onClick={() => onSubsystemsChange([])}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left",
                    "text-[12px] transition-colors",
                    selectedSubsystems.length === 0
                      ? "bg-tp-glass-inner-hover text-tp-ink"
                      : "text-tp-ink-2 hover:bg-tp-glass-inner hover:text-tp-ink",
                  )}
                >
                  <span className="flex-1 font-mono">
                    {t("logs.tp.subAll")}
                  </span>
                </button>
                <div className="my-1 h-px bg-tp-glass-edge" />
                {subsystems.map((s) => {
                  const picked = selectedSubsystems.includes(s.value);
                  return (
                    <button
                      key={s.value}
                      type="button"
                      role="option"
                      aria-selected={picked}
                      onClick={() => {
                        const set = new Set(selectedSubsystems);
                        if (set.has(s.value)) set.delete(s.value);
                        else set.add(s.value);
                        onSubsystemsChange(Array.from(set));
                      }}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left",
                        "text-[12px] transition-colors",
                        picked
                          ? "bg-tp-glass-inner-hover text-tp-ink"
                          : "text-tp-ink-2 hover:bg-tp-glass-inner hover:text-tp-ink",
                      )}
                    >
                      <span
                        aria-hidden
                        className={cn(
                          "h-3 w-3 rounded border",
                          picked
                            ? "bg-tp-amber border-tp-amber"
                            : "bg-tp-glass-inner border-tp-glass-edge",
                        )}
                      />
                      <span className="flex-1 truncate font-mono">{s.value}</span>
                      <span className="font-mono text-[10.5px] tabular-nums text-tp-ink-4">
                        {s.count}
                      </span>
                    </button>
                  );
                })}
              </>
            )}
          </div>
        ) : null}
      </div>

      {/* Search input */}
      <div
        className={cn(
          "inline-flex flex-1 min-w-[220px] items-center gap-2 rounded-lg border px-2.5 py-[5px]",
          "bg-tp-glass-inner border-tp-glass-edge",
          "focus-within:border-tp-amber/40",
        )}
      >
        <Search className="h-3 w-3 text-tp-ink-4" aria-hidden />
        <input
          ref={searchInputRef}
          type="text"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder={t("logs.tp.searchPlaceholder")}
          aria-label={t("logs.tp.searchAria")}
          className={cn(
            "flex-1 border-0 bg-transparent text-[12.5px] text-tp-ink outline-none",
            "placeholder:text-tp-ink-4",
          )}
        />
        <kbd
          className={cn(
            "rounded border border-tp-glass-edge bg-tp-glass-inner-strong",
            "px-1.5 py-px font-mono text-[10px] text-tp-ink-3",
          )}
        >
          ⌘F
        </kbd>
      </div>

      {/* Right actions */}
      <div className="ml-auto flex items-center gap-1.5">
        {rangeReadout ? (
          <span className="font-mono text-[11px] tabular-nums text-tp-ink-4">
            {rangeReadout}
          </span>
        ) : null}
        <IconButton
          title={t("logs.tp.clearTitle")}
          disabled={!canClear}
          onClick={onClear}
          aria-label={t("logs.tp.clearTitle")}
        >
          <Trash2 className="h-3 w-3" />
        </IconButton>
        <IconButton
          title={t("logs.tp.exportTitle")}
          onClick={() => {
            /* export is a Phase 5 follow-up — visual affordance only */
          }}
          aria-label={t("logs.tp.exportTitle")}
        >
          <Download className="h-3 w-3" />
        </IconButton>
        <IconButton
          title={t("logs.tp.settingsTitle")}
          onClick={() => {
            /* settings is a Phase 5 follow-up — visual affordance only */
          }}
          aria-label={t("logs.tp.settingsTitle")}
        >
          <Settings2 className="h-3 w-3" />
        </IconButton>
      </div>
    </GlassPanel>
  );
}

function IconButton({
  children,
  className,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex h-[30px] w-[30px] items-center justify-center rounded-lg border",
        "bg-tp-glass-inner border-tp-glass-edge text-tp-ink-3",
        "hover:bg-tp-glass-inner-hover hover:text-tp-ink-2",
        "disabled:cursor-not-allowed disabled:opacity-40",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

export default LogsControlBar;
