/**
 * EventSource wrapper for gateway SSE streams (logs, chat completions,
 * pending-approval notifications).
 *
 * Reconnect behaviour (Sprint 5 T4): browsers' native `EventSource` already
 * reconnects on transport error, but the default retry is ~3 s without
 * backoff. For the admin UI we'd rather back off exponentially (2s / 4s /
 * 8s / capped at 30s) so a wedged gateway doesn't hammer itself. We
 * implement that by closing the underlying ES on error, then scheduling a
 * fresh open via setTimeout. All listeners get re-attached on the new ES.
 */

import { GATEWAY_BASE_URL, MOCK_API_URL, MOCK_MODE } from "./api";

export type SseHandler<T = unknown> = (event: {
  event: string;
  data: T;
  id?: string;
}) => void;

export interface OpenEventStreamOptions<T> {
  onMessage: SseHandler<T>;
  onError?: (err: Event) => void;
  /** Named events to subscribe to. Defaults to ["message"]. */
  events?: string[];
  /** Mock emitter used when MOCK_MODE is true. Returns teardown. */
  mock?: (push: SseHandler<T>) => () => void;
}

/** Exponential reconnect schedule (ms). Mirrors corlinman-core::backoff but
 * tuned shorter for interactive SSE channels. */
const BACKOFF_SCHEDULE_MS = [2_000, 4_000, 8_000, 30_000] as const;

/** Opens an EventSource on `${GATEWAY_BASE_URL}${path}`; returns close fn.
 *
 * On transport error the underlying EventSource is torn down and a new one
 * is opened after a backoff delay. The returned close fn cancels any
 * pending reconnect and closes the live connection. */
export function openEventStream<T = unknown>(
  path: string,
  opts: OpenEventStreamOptions<T>,
): () => void {
  // Inline mock emitter wins only when no real mock server is configured.
  if (MOCK_MODE && !MOCK_API_URL && opts.mock) {
    return opts.mock(opts.onMessage);
  }

  const base = MOCK_API_URL || GATEWAY_BASE_URL;
  const url = `${base}${path}`;
  const names = opts.events ?? ["message"];

  let es: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let retryIndex = 0;
  let disposed = false;

  const connect = (): void => {
    if (disposed) return;
    es = new EventSource(url, { withCredentials: !MOCK_API_URL });

    for (const name of names) {
      es.addEventListener(name, (raw: Event) => {
        const me = raw as MessageEvent;
        let parsed: T;
        try {
          parsed = JSON.parse(me.data) as T;
        } catch {
          parsed = me.data as unknown as T;
        }
        opts.onMessage({ event: name, data: parsed, id: me.lastEventId });
        // A successful message means the connection recovered; reset backoff.
        retryIndex = 0;
      });
    }

    es.onerror = (err) => {
      opts.onError?.(err);
      if (disposed) return;
      // EventSource auto-reconnects on its own, but with a short fixed
      // delay. We force-close and schedule our own backed-off retry so a
      // dead gateway doesn't get hammered.
      es?.close();
      es = null;
      const delay =
        BACKOFF_SCHEDULE_MS[Math.min(retryIndex, BACKOFF_SCHEDULE_MS.length - 1)];
      retryIndex += 1;
      retryTimer = setTimeout(connect, delay);
    };
  };

  connect();

  return () => {
    disposed = true;
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
    es?.close();
    es = null;
  };
}
