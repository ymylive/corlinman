"use client";

import * as React from "react";
import { Pause, Play } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Live / paused indicator pill with breathing dot and optional rate readout.
 *
 * States:
 *   - `live`     — green dot with `.tp-breathe`, optional `rate` suffix
 *     (e.g. "41.2/s"). Pause button icon shown when `onToggle` is passed.
 *   - `paused`   — muted dot, no breathing, Play button icon.
 *   - `throttled`— amber dot with `.tp-breathe-amber`; indicates backpressure
 *     upstream, e.g. when the hook bus is rate-limiting a subscriber.
 *
 * Accessibility:
 *   - `role="status"` + `aria-live="polite"` so screen readers announce
 *     state changes without stealing focus.
 *   - The toggle button (if present) carries an `aria-label` reflecting
 *     the action that *will* happen on click ("Pause" when live, etc).
 */

export type StreamState = "live" | "paused" | "throttled";

export interface StreamPillProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "onToggle"> {
  state: StreamState;
  /** Optional rate suffix — free text, e.g. "41.2/s" or "0 ev/min". */
  rate?: string;
  /** Show Pause/Play button. Fires on click with the *current* state. */
  onToggle?: (current: StreamState) => void;
}

const config: Record<
  StreamState,
  { label: string; dotTone: "ok" | "warn" | "muted"; breatheClass: string }
> = {
  live: { label: "Live", dotTone: "ok", breatheClass: "tp-breathe" },
  paused: { label: "Paused", dotTone: "muted", breatheClass: "" },
  throttled: {
    label: "Throttled",
    dotTone: "warn",
    breatheClass: "tp-breathe-amber",
  },
};

const dotColor: Record<"ok" | "warn" | "muted", string> = {
  ok: "bg-tp-ok",
  warn: "bg-tp-warn",
  muted: "bg-tp-ink-4",
};

const containerTone: Record<StreamState, string> = {
  live: "bg-tp-ok-soft text-tp-ok border-tp-ok/25",
  paused: "bg-tp-glass-inner text-tp-ink-3 border-tp-glass-edge",
  throttled: "bg-tp-warn-soft text-tp-warn border-tp-warn/25",
};

export const StreamPill = React.forwardRef<HTMLDivElement, StreamPillProps>(
  function StreamPill(
    { state, rate, onToggle, className, ...rest },
    ref,
  ) {
    const cfg = config[state];
    const actionLabel =
      state === "live" ? "Pause stream" : state === "paused" ? "Resume stream" : "Resume stream";
    const ActionIcon = state === "live" ? Pause : Play;

    return (
      <div
        ref={ref}
        role="status"
        aria-live="polite"
        className={cn(
          "inline-flex items-center gap-2 rounded-full border",
          "py-[5px] pl-[10px] pr-3 font-mono text-[11.5px]",
          containerTone[state],
          className,
        )}
        data-state={state}
        {...rest}
      >
        <span
          aria-hidden="true"
          className={cn("h-[7px] w-[7px] rounded-full", dotColor[cfg.dotTone], cfg.breatheClass)}
        />
        <span>{cfg.label}</span>
        {rate ? (
          <span className="text-current/80">· {rate}</span>
        ) : null}
        {onToggle ? (
          <button
            type="button"
            aria-label={actionLabel}
            onClick={() => onToggle(state)}
            className={cn(
              "-mr-1 ml-1 inline-flex h-4 w-4 items-center justify-center rounded-sm",
              "text-current/70 hover:text-current focus-visible:outline-none",
              "focus-visible:ring-2 focus-visible:ring-current/40",
            )}
          >
            <ActionIcon className="h-3 w-3" />
          </button>
        ) : null}
      </div>
    );
  },
);

export default StreamPill;
