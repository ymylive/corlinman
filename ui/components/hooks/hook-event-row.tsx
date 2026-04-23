"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import type {
  HookEvent,
  HookEventKind,
} from "@/lib/hooks/use-mock-hook-stream";

/**
 * Row primitive for the Hooks live stream. Shares its dense-grid language with
 * `<LogRow variant="dense">` but swaps the severity column for an
 * **event-kind pill** (the taxonomy discriminator) and the duration column for
 * a **latency** readout that doubles as the subscribers count.
 *
 * Columns (grid): `ts · kind · subscribers · message · latency`.
 *
 * Behavioural mirrors to `<LogRow>`:
 *   - `justNow` lights up the 2px amber left-edge bar for ~2.8s (tp-just-now).
 *   - `selected` wins over `justNow` (amber soft fill + 2px inset bar).
 *
 * Accessibility: rendered as a `<button>` so keyboard users can select the
 * row with Enter / Space; `aria-selected` mirrors the `selected` prop.
 */

/** Palette tone assigned per category of hook kind. Warm amber + ember on the
 * lifecycle / config / approval / rate-limit kinds; cooler on the
 * session/agent/message "hot path" kinds so they don't scream for attention
 * during the firehose. */
export type HookKindTone =
  | "message"
  | "session"
  | "agent"
  | "config"
  | "lifecycle"
  | "approval"
  | "rate_limit"
  | "tool"
  | "error"
  | "neutral";

/**
 * Deterministic kind-to-tone mapping. Exposed + pure so the page can build the
 * FilterChipGroup tones off the same vocabulary without duplicating branch
 * logic. For compound kinds (`approval.decided` with `deny`, `tool.called`
 * with `ok=false`) the caller can optionally pass the payload — failures
 * surface as `error`.
 */
export function kindTone(
  kind: string,
  payload?: Record<string, unknown>,
): HookKindTone {
  if (kind.startsWith("message.")) return "message";
  if (kind === "session.patch") return "session";
  if (kind.startsWith("agent.")) return "agent";
  if (kind === "gateway.startup") return "lifecycle";
  if (kind === "config.changed") return "config";
  if (kind === "approval.requested") return "approval";
  if (kind === "approval.decided") {
    const decision = payload?.decision;
    if (decision === "deny" || decision === "timeout") return "error";
    return "approval";
  }
  if (kind === "rate_limit.triggered") return "rate_limit";
  if (kind === "tool.called") {
    if (payload?.ok === false) return "error";
    return "tool";
  }
  return "neutral";
}

const kindPillClass: Record<HookKindTone, string> = {
  // warm-orange on lifecycle + config — they're "loud" events
  lifecycle: "bg-tp-ok-soft text-tp-ok border-tp-ok/30",
  config: "bg-tp-amber-soft text-tp-amber border-tp-amber/30",
  approval: "bg-tp-amber-soft text-tp-amber border-tp-amber/30",
  rate_limit: "bg-tp-warn-soft text-tp-warn border-tp-warn/30",
  error: "bg-tp-err-soft text-tp-err border-tp-err/30",
  // ink-range on routine hot-path events
  message: "bg-tp-glass-inner-strong text-tp-ink-2 border-tp-glass-edge",
  session: "bg-tp-glass-inner-strong text-tp-ink-2 border-tp-glass-edge",
  agent: "bg-tp-glass-inner-strong text-tp-ink-2 border-tp-glass-edge",
  tool: "bg-tp-glass-inner-strong text-tp-ink-2 border-tp-glass-edge",
  neutral: "bg-tp-glass-inner text-tp-ink-3 border-tp-glass-edge",
};

/** `HH:mm:ss.SSS` — mirrors the log-row time column shape. */
export function formatRowTs(ts: number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--.---";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}

/** Latency → "Nms" | "N.Ns" | "—". Null/undefined collapse to the em dash. */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1) return `${ms.toFixed(1)}ms`;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export interface HookEventRowProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onSelect"> {
  event: HookEvent;
  /** Number of in-process subscribers that observed this event. */
  subscribers: number;
  /** Dispatch latency in milliseconds (p50 for a typical subscriber). */
  latencyMs: number | null;
  /** Row arrived within the last ~2.8s — triggers the tp-just-now bar. */
  justNow?: boolean;
  /** Row is the detail-drawer target (amber soft fill + inset bar). */
  selected?: boolean;
}

export const HookEventRow = React.forwardRef<
  HTMLButtonElement,
  HookEventRowProps
>(function HookEventRow(
  {
    event,
    subscribers,
    latencyMs,
    justNow,
    selected,
    className,
    ...rest
  },
  ref,
) {
  const tone = kindTone(event.kind, event.payload);
  const iso = new Date(event.ts).toISOString();

  return (
    <button
      ref={ref}
      type="button"
      aria-pressed={selected || undefined}
      data-selected={selected || undefined}
      data-just-now={justNow || undefined}
      data-kind={event.kind}
      data-testid="hook-event-row"
      className={cn(
        "relative grid w-full items-center gap-3 text-left",
        "border-b border-tp-glass-edge transition-colors",
        "grid-cols-[82px_170px_64px_minmax(0,1fr)_auto] px-4 py-2 text-[12.5px]",
        "hover:bg-tp-glass-inner-hover",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
        selected && "bg-tp-amber-soft",
        selected &&
          "shadow-[inset_2px_0_0_var(--tp-amber),inset_0_0_0_1px_color-mix(in_oklch,var(--tp-amber)_20%,transparent)]",
        className,
      )}
      {...rest}
    >
      {/* Just-now left-edge bar — the selected amber bar takes precedence. */}
      {justNow && !selected ? (
        <span
          aria-hidden
          className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-sm bg-tp-amber shadow-[0_0_8px_var(--tp-amber-glow)] tp-just-now"
        />
      ) : null}

      {/* time */}
      <time
        dateTime={iso}
        className="font-mono text-[11px] tabular-nums text-tp-ink-4"
      >
        {formatRowTs(event.ts)}
      </time>

      {/* kind pill */}
      <span
        className={cn(
          "inline-flex w-fit items-center rounded border px-1.5 py-[1px]",
          "font-mono text-[10px] font-medium tracking-wider",
          kindPillClass[tone],
        )}
        data-testid="hook-kind-pill"
      >
        {event.kind}
      </span>

      {/* subscribers count */}
      <span
        className="font-mono text-[10.5px] tabular-nums text-tp-ink-3"
        aria-label={`${subscribers} subscribers`}
      >
        <span aria-hidden>◎ </span>
        {subscribers}
      </span>

      {/* summary + optional session chip */}
      <span className="flex min-w-0 items-center gap-2">
        <span className="truncate text-tp-ink-2">{event.summary}</span>
        {event.session_key ? (
          <span
            className={cn(
              "shrink-0 rounded border px-1.5 py-[1px]",
              "bg-tp-glass-inner border-tp-glass-edge",
              "font-mono text-[10px] text-tp-ink-3",
            )}
          >
            {event.session_key}
          </span>
        ) : null}
      </span>

      {/* latency */}
      <span
        className={cn(
          "font-mono text-[10.5px] tabular-nums text-tp-ink-4",
          tone === "error" && "text-tp-err",
        )}
      >
        {formatLatency(latencyMs)}
      </span>
    </button>
  );
});

/** Pure derivation of per-event metrics so tests / sparklines can call the same.
 *
 * Subscribers: derived from the event kind's audience profile. In the real
 * HookBus an event like `config.changed` fans out to every subsystem, while
 * something like `message.transcribed` is observed by a narrower set. The
 * specific numbers are illustrative — when B5 ships `/admin/hooks/stream` this
 * helper becomes a proper lookup into the subscriber registry.
 *
 * Latency: synthesised from the kind's typical dispatch shape with a bit of
 * jitter seeded by the event id so re-renders stay stable. */
export function deriveHookMetrics(event: HookEvent): {
  subscribers: number;
  latencyMs: number;
} {
  const baseSubscribers: Partial<Record<HookEventKind, number>> = {
    "config.changed": 8,
    "gateway.startup": 6,
    "agent.bootstrap": 5,
    "session.patch": 4,
    "message.received": 3,
    "message.sent": 3,
    "message.transcribed": 2,
    "message.preprocessed": 2,
    "approval.requested": 3,
    "approval.decided": 3,
    "rate_limit.triggered": 4,
    "tool.called": 5,
  };
  const baseLatency: Partial<Record<HookEventKind, number>> = {
    "config.changed": 12,
    "gateway.startup": 18,
    "agent.bootstrap": 9,
    "session.patch": 3,
    "message.received": 2,
    "message.sent": 2,
    "message.transcribed": 14,
    "message.preprocessed": 5,
    "approval.requested": 6,
    "approval.decided": 4,
    "rate_limit.triggered": 2,
    "tool.called": 22,
  };

  // Cheap deterministic jitter: hash the id to a 0..1 float.
  let hash = 0;
  for (let i = 0; i < event.id.length; i += 1) {
    hash = (hash * 31 + event.id.charCodeAt(i)) | 0;
  }
  const jitter = ((hash >>> 0) % 1000) / 1000; // 0..1

  const subs = baseSubscribers[event.kind] ?? 2;
  const lat = baseLatency[event.kind] ?? 6;
  return {
    subscribers: subs,
    // ±40% jitter, clamped to [0.5, 5x]
    latencyMs: Math.max(0.5, lat * (0.6 + jitter * 0.8)),
  };
}

export default HookEventRow;
