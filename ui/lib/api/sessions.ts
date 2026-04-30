/**
 * Sessions admin API client (Phase 4 W2 4-2D — Trajectory replay).
 *
 * Mirrors the Rust HTTP routes being built in parallel by Agent A under
 * `rust/crates/corlinman-gateway/src/routes/admin/sessions.rs`. Until that
 * commit lands the dev workflow points `NEXT_PUBLIC_MOCK_API_URL` at
 * `mock/server.ts`, which serves the same byte-for-byte payload contract.
 *
 *   GET  /admin/sessions
 *     → 200 { sessions: SessionSummary[] }
 *     → 503 { error: "sessions_disabled" }
 *
 *   POST /admin/sessions/:key/replay
 *     body (optional): { mode?: "transcript" | "rerun" }
 *     → 200 ReplayResponse
 *     → 404 { error: "not_found", session_key: <key> }
 *     → 503 { error: "sessions_disabled" }
 *
 * `last_message_at` is **unix milliseconds** (the SQLite column is i64) — the
 * UI formats it via `new Date(ms).toLocaleString()`. Transcript message `ts`
 * is RFC-3339 / ISO-8601 (matches the `tenants.created_at` convention from
 * Phase 4 W1 4-1B once that surface ships).
 *
 * The `rerun` mode is stubbed by Agent A in v1: the response carries
 * `summary.rerun_diff = "not_implemented_yet"` and the same transcript shape.
 * The page renders a placeholder explaining the deferral when it sees that
 * sentinel; the dialog UI keeps `rerun` disabled with a "coming in Wave 2.5"
 * tooltip until W2.5 ships the full diff renderer.
 */

import { CorlinmanApiError, apiFetch } from "@/lib/api";

/* ------------------------------------------------------------------ */
/*                           Public types                             */
/* ------------------------------------------------------------------ */

/** One row in `GET /admin/sessions`. */
export interface SessionSummary {
  /** Composite key — typically `<channel>:<scope>:<id>`, e.g. `qq:1234`. */
  session_key: string;
  /** Unix milliseconds of the most-recent message in the session. */
  last_message_at: number;
  /** Total message count across both roles. */
  message_count: number;
}

export type ReplayMode = "transcript" | "rerun";

export type TranscriptRole = "user" | "assistant" | "system";

/** One message in a replay's `transcript` array. */
export interface TranscriptMessage {
  role: TranscriptRole;
  content: string;
  /** RFC-3339 / ISO-8601 string. */
  ts: string;
}

/** Summary block on a replay response. */
export interface ReplaySummary {
  message_count: number;
  tenant_id: string;
  /**
   * Sentinel string `"not_implemented_yet"` when `mode === "rerun"` in v1.
   * Absent otherwise. The UI keys off this to render the deferral notice.
   */
  rerun_diff?: string;
}

export interface ReplayResponse {
  session_key: string;
  mode: ReplayMode;
  transcript: TranscriptMessage[];
  summary: ReplaySummary;
}

/** Tagged result so callers can branch on `503 sessions_disabled` without
 *  having to pattern-match on `CorlinmanApiError.message`. */
export type SessionsListResult =
  | { kind: "ok"; sessions: SessionSummary[] }
  | { kind: "disabled" };

export type ReplayResult =
  | { kind: "ok"; replay: ReplayResponse }
  | { kind: "not_found"; session_key: string }
  | { kind: "disabled" };

/* ------------------------------------------------------------------ */
/*                          URL builders                              */
/* ------------------------------------------------------------------ */

/** GET path for the list endpoint. */
export const SESSIONS_LIST_PATH = "/admin/sessions";

/**
 * POST path for the replay endpoint. Encodes `session_key` so colons and
 * other punctuation in keys like `qq:group:123` round-trip through the URL.
 */
export function sessionsReplayPath(sessionKey: string): string {
  return `/admin/sessions/${encodeURIComponent(sessionKey)}/replay`;
}

/* ------------------------------------------------------------------ */
/*                          Error helpers                             */
/* ------------------------------------------------------------------ */

function is503(err: unknown): boolean {
  return err instanceof CorlinmanApiError && err.status === 503;
}

function is404(err: unknown): boolean {
  return err instanceof CorlinmanApiError && err.status === 404;
}

/* ------------------------------------------------------------------ */
/*                           Public fetches                           */
/* ------------------------------------------------------------------ */

/**
 * GET /admin/sessions → list of session keys with last_message_at + count.
 *
 * Returns a tagged `SessionsListResult` so the page can paint the
 * "session storage is off" banner on 503 without inspecting exception
 * messages itself.
 */
export async function fetchSessions(): Promise<SessionsListResult> {
  try {
    const res = await apiFetch<{ sessions: SessionSummary[] }>(
      SESSIONS_LIST_PATH,
    );
    return { kind: "ok", sessions: res.sessions ?? [] };
  } catch (err) {
    if (is503(err)) return { kind: "disabled" };
    throw err;
  }
}

/**
 * POST /admin/sessions/:key/replay → deterministic transcript dump.
 *
 * Defaults `mode` to `"transcript"` when omitted (matches Agent A's Rust
 * route default). Returns a tagged result so the dialog can render an inline
 * "session not found" message on 404 instead of a global error toast.
 */
export async function replaySession(
  sessionKey: string,
  opts?: { mode?: ReplayMode },
): Promise<ReplayResult> {
  const mode: ReplayMode = opts?.mode ?? "transcript";
  try {
    const replay = await apiFetch<ReplayResponse>(
      sessionsReplayPath(sessionKey),
      {
        method: "POST",
        body: { mode },
      },
    );
    return { kind: "ok", replay };
  } catch (err) {
    if (is404(err)) return { kind: "not_found", session_key: sessionKey };
    if (is503(err)) return { kind: "disabled" };
    throw err;
  }
}

/** Sentinel value emitted by Agent A's stub for `mode === "rerun"`. */
export const RERUN_NOT_IMPLEMENTED = "not_implemented_yet" as const;
