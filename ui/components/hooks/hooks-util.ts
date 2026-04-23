/**
 * Pure helpers that support the Hooks page but don't render anything.
 * Kept out of `page.tsx` so the page file stays focused on layout + state.
 */

import type { HookCategory, HookEvent } from "@/lib/hooks/use-mock-hook-stream";
import { kindTone } from "./hook-event-row";
import { CATEGORY_ORDER } from "./hooks-control-bar";

export function parseCategory(raw: string | null): HookCategory {
  if (!raw) return "all";
  const allowed: HookCategory[] = CATEGORY_ORDER;
  return (allowed as string[]).includes(raw) ? (raw as HookCategory) : "all";
}

/** Representative subscriber names per kind tone. Illustrative until B5 ships
 * the hook bus introspection endpoint — fanning out the same static list each
 * render keeps the drawer readable and the subscribers-stat honest about
 * fan-out shape. */
export function subscriberPanel(evt: HookEvent): string[] {
  const tone = kindTone(evt.kind, evt.payload);
  switch (tone) {
    case "message":
      return ["session-store", "rag-indexer", "telemetry"];
    case "session":
      return ["session-store", "checkpointer", "ui-bridge"];
    case "agent":
      return ["supervisor", "telemetry", "scheduler"];
    case "lifecycle":
      return [
        "health-probe",
        "admin-ui",
        "metrics",
        "registry",
        "plugin-supervisor",
        "node-bridge",
      ];
    case "config":
      return [
        "router",
        "plugin-supervisor",
        "channels",
        "rag",
        "approvals",
        "telemetry",
        "logging",
        "scheduler",
      ];
    case "approval":
      return ["approvals-ui", "audit-log", "decider-rules"];
    case "rate_limit":
      return ["throttler", "audit-log", "telemetry", "channels"];
    case "tool":
      return ["tool-runner", "audit-log", "telemetry", "scheduler", "ui"];
    case "error":
      return ["error-sink", "telemetry", "audit-log"];
    default:
      return ["telemetry"];
  }
}

export interface BufferAggregates {
  subscribers: number;
  p50: number | null;
  p95: number | null;
  topKind: string | null;
}

/** Summarise the current ring: average subscribers, p50/p95 dispatch latency,
 * busiest kind. All values are `null` when the ring is empty. */
export function aggregateBuffer(
  events: HookEvent[],
  getMetrics: (
    e: HookEvent,
  ) => { subscribers: number; latencyMs: number },
): BufferAggregates {
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
    const idx = Math.min(
      latencies.length - 1,
      Math.max(0, Math.floor(q * (latencies.length - 1))),
    );
    return latencies[idx]!;
  };
  let topKind: string | null = null;
  let topCount = -1;
  for (const [k, n] of kindCounts) {
    if (n > topCount) {
      topKind = k;
      topCount = n;
    }
  }
  return {
    subscribers: Math.round(totalSubs / events.length),
    p50: p(0.5),
    p95: p(0.95),
    topKind,
  };
}
