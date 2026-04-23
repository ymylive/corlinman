import type { QqStatus } from "@/lib/api";
import type { StreamState } from "@/components/ui/stream-pill";

/**
 * Derived state bag for the QQ admin page. The REST surface only exposes
 * `runtime` + a handful of booleans; this collapses them to a small handful
 * of view-layer concepts (StreamPill state, high-level status label key)
 * that every downstream component can lean on.
 */

export type QqConnection = "connected" | "disconnected" | "unknown" | "offline" | "disabled";

export function deriveConnection(status: QqStatus | undefined): QqConnection {
  if (!status) return "offline";
  if (!status.configured) return "offline";
  if (!status.enabled) return "disabled";
  return status.runtime;
}

/** StreamPill state for the hero strip. `throttled` doubles as "reconnecting". */
export function streamStateFor(
  conn: QqConnection,
  reconnecting: boolean,
): StreamState {
  if (reconnecting) return "throttled";
  if (conn === "connected") return "live";
  return "paused";
}

export function i18nConnectionKey(conn: QqConnection): string {
  switch (conn) {
    case "connected":
      return "channels.connectionConnected";
    case "disconnected":
      return "channels.connectionDisconnected";
    case "offline":
      return "channels.connectionNotConfigured";
    case "disabled":
      return "channels.connectionDisabled";
    default:
      return "channels.connectionUnknown";
  }
}

// -- Recent message row shape ------------------------------------------------

export interface QqRecentMessage {
  ts?: string;
  chatId?: string;
  sender?: string;
  preview: string;
  raw: Record<string, unknown>;
}

function toMono(v: unknown): string | undefined {
  if (v === undefined || v === null) return undefined;
  return String(v);
}

/** Normalise a raw NapCat/OneBot message blob to the fields we display. */
export function normaliseRecent(
  raw: Record<string, unknown>,
): QqRecentMessage {
  const preview =
    (raw.text as string | undefined) ??
    (raw.content as string | undefined) ??
    (raw.message as string | undefined) ??
    JSON.stringify(raw);
  const ts = toMono(raw.ts ?? raw.time ?? raw.at);
  const sender = toMono(raw.from ?? raw.user_id ?? raw.sender);
  const chatId = toMono(
    raw.chat_id ?? raw.group_id ?? raw.room ?? raw.channel,
  );
  return { ts, chatId, sender, preview, raw };
}

/** Compact HH:MM:SS slice from an ISO-ish string; `—` otherwise. */
export function formatTsShort(ts: string | undefined): string {
  if (!ts) return "—";
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(ts)) return ts.slice(11, 19);
  // numeric epoch (seconds or ms)
  const n = Number(ts);
  if (Number.isFinite(n)) {
    const d = new Date(n < 1e12 ? n * 1_000 : n);
    return d.toISOString().slice(11, 19);
  }
  return ts.slice(0, 8);
}

/** "12s ago", "4m ago", "1h ago" — very coarse. */
export function formatRelativeAgo(ts: string | undefined, now: number): string | null {
  if (!ts) return null;
  let then: number;
  if (/^\d{4}-\d{2}-\d{2}T/.test(ts)) {
    then = new Date(ts).getTime();
  } else {
    const n = Number(ts);
    if (!Number.isFinite(n)) return null;
    then = n < 1e12 ? n * 1_000 : n;
  }
  if (!Number.isFinite(then)) return null;
  const delta = Math.max(0, Math.floor((now - then) / 1_000));
  if (delta < 60) return `${delta}s`;
  if (delta < 3_600) return `${Math.floor(delta / 60)}m`;
  return `${Math.floor(delta / 3_600)}h`;
}
