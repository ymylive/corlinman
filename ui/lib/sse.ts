/**
 * EventSource wrapper for gateway SSE streams (logs, chat completions,
 * pending-approval notifications).
 *
 * TODO(M6): add automatic reconnect-with-backoff using
 *           corlinman-core::backoff::DEFAULT_SCHEDULE ([5,10,30,60]s).
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

/** Opens an EventSource on `${GATEWAY_BASE_URL}${path}`; returns close fn. */
export function openEventStream<T = unknown>(
  path: string,
  opts: OpenEventStreamOptions<T>,
): () => void {
  // Inline mock emitter wins only when no real mock server is configured.
  if (MOCK_MODE && !MOCK_API_URL && opts.mock) {
    return opts.mock(opts.onMessage);
  }

  const base = MOCK_API_URL || GATEWAY_BASE_URL;
  // Mock server is same-origin-less; skip credentials there.
  const es = new EventSource(`${base}${path}`, {
    withCredentials: !MOCK_API_URL,
  });
  const names = opts.events ?? ["message"];
  const listeners: Array<[string, EventListener]> = [];

  for (const name of names) {
    const listener: EventListener = (raw: Event) => {
      const me = raw as MessageEvent;
      let parsed: T;
      try {
        parsed = JSON.parse(me.data) as T;
      } catch {
        parsed = me.data as unknown as T;
      }
      opts.onMessage({ event: name, data: parsed, id: me.lastEventId });
    };
    es.addEventListener(name, listener);
    listeners.push([name, listener]);
  }

  if (opts.onError) es.onerror = opts.onError;

  return () => {
    for (const [name, listener] of listeners) es.removeEventListener(name, listener);
    es.close();
  };
}
