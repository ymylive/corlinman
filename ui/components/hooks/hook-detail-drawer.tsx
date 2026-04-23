"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { DetailDrawer } from "@/components/ui/detail-drawer";
import { JsonView } from "@/components/ui/json-view";
import type { HookEvent } from "@/lib/hooks/use-mock-hook-stream";
import { kindTone, formatLatency } from "./hook-event-row";

/**
 * Right-rail detail drawer for the Hooks stream.
 *
 * Layout inside the <DetailDrawer>:
 *   - meta row  : kind pill · full timestamp · relative "Ns ago"
 *   - subtitle  : session_key or "n/a" (amber mono — same as Logs)
 *   - title     : event summary
 *   - Section 1 : Dispatch — subscribers list + latency
 *   - Section 2 : Payload — JsonView of `event.payload`
 *
 * The drawer is inline / glass; closing delegates to the parent ("click the
 * selected row again" pattern), matching Logs + Approvals.
 */

export interface HookDetailDrawerProps {
  event: HookEvent;
  subscribers: number;
  latencyMs: number | null;
  /** Representative subscriber names for the Dispatch section. */
  subscriberNames: string[];
  className?: string;
}

const tonePillClass: Record<ReturnType<typeof kindTone>, string> = {
  message: "bg-tp-glass-inner-strong text-tp-ink-2 border-tp-glass-edge",
  session: "bg-tp-glass-inner-strong text-tp-ink-2 border-tp-glass-edge",
  agent: "bg-tp-glass-inner-strong text-tp-ink-2 border-tp-glass-edge",
  lifecycle: "bg-tp-ok-soft text-tp-ok border-tp-ok/25",
  config: "bg-tp-amber-soft text-tp-amber border-tp-amber/25",
  approval: "bg-tp-amber-soft text-tp-amber border-tp-amber/25",
  rate_limit: "bg-tp-warn-soft text-tp-warn border-tp-warn/25",
  tool: "bg-tp-glass-inner-strong text-tp-ink-2 border-tp-glass-edge",
  error: "bg-tp-err-soft text-tp-err border-tp-err/25",
  neutral: "bg-tp-glass-inner-strong text-tp-ink-3 border-tp-glass-edge",
};

export function HookDetailDrawer({
  event,
  subscribers,
  latencyMs,
  subscriberNames,
  className,
}: HookDetailDrawerProps) {
  const { t } = useTranslation();
  const tone = kindTone(event.kind, event.payload);
  const relative = useRelativeAgo(event.ts);
  const fullTs = formatFullTs(event.ts);

  const meta = (
    <>
      <span
        className={cn(
          "rounded-md border px-2 py-[2px] font-mono text-[10px] font-medium tracking-[0.04em]",
          tonePillClass[tone],
        )}
        data-testid="drawer-kind-pill"
      >
        {event.kind}
      </span>
      <span className="font-mono text-[13px] tabular-nums text-tp-ink">
        {fullTs}
      </span>
      {relative ? (
        <span className="font-mono text-[11px] text-tp-ink-4">{relative}</span>
      ) : null}
    </>
  );

  const subsystemLine = event.session_key ?? t("hooks.tp.drawerNoSession");

  // Phase 1 parity: derive a plausible id we can expose as trace_id for the
  // DetailDrawer's TraceRow. Uses the event's stable id.
  const traceId = (event.payload?.id as string | undefined) ?? event.id;

  return (
    <DetailDrawer
      title={<span data-testid="drawer-summary">{event.summary}</span>}
      subsystem={subsystemLine}
      meta={meta}
      trace={{ id: traceId, label: t("hooks.tp.drawerTrace") }}
      className={className}
    >
      <DetailDrawer.Section label={t("hooks.tp.sectionDispatch")}>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-[13px]">
          <div className="flex items-baseline gap-1.5">
            <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
              {t("hooks.tp.drawerSubscribersLabel")}
            </span>
            <span className="font-medium tabular-nums text-tp-ink">
              {subscribers}
            </span>
          </div>
          <div className="flex items-baseline gap-1.5">
            <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
              {t("hooks.tp.drawerLatencyLabel")}
            </span>
            <span className="font-medium tabular-nums text-tp-ink">
              {formatLatency(latencyMs)}
            </span>
          </div>
        </div>

        {subscriberNames.length > 0 ? (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {subscriberNames.map((name) => (
              <span
                key={name}
                className={cn(
                  "inline-flex items-center rounded-full border px-2 py-[2px]",
                  "bg-tp-glass-inner border-tp-glass-edge",
                  "font-mono text-[10.5px] text-tp-ink-3",
                )}
              >
                {name}
              </span>
            ))}
          </div>
        ) : (
          <div className="mt-3 font-mono text-[11.5px] text-tp-ink-4">
            {t("hooks.tp.drawerSubscribersEmpty")}
          </div>
        )}
      </DetailDrawer.Section>

      <DetailDrawer.Section label={t("hooks.tp.sectionPayload")}>
        {event.payload && Object.keys(event.payload).length > 0 ? (
          <JsonView value={event.payload} />
        ) : (
          <div
            className={cn(
              "rounded-lg border border-dashed border-tp-glass-edge",
              "bg-tp-glass-inner p-4 text-center",
              "font-mono text-[11.5px] text-tp-ink-4",
            )}
          >
            {t("hooks.tp.drawerPayloadEmpty")}
          </div>
        )}
      </DetailDrawer.Section>
    </DetailDrawer>
  );
}

/** Full `yyyy-MM-dd HH:mm:ss.SSS` display for the drawer header. */
function formatFullTs(ts: number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  const yyyy = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${yyyy}-${mo}-${dd} ${hh}:${mm}:${ss}.${ms}`;
}

function useRelativeAgo(ts: number): string | null {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);
  if (!Number.isFinite(ts)) return null;
  const diff = Math.max(0, now - ts);
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export default HookDetailDrawer;
