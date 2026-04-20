/**
 * Shared approvals UI types. Re-exports the transport shape from `lib/api`
 * and adds the SSE event discriminated union the page subscribes to.
 */

export type { Approval, BatchDecideOutcome, DecideResult } from "@/lib/api";
import type { Approval } from "@/lib/api";

/** Events the gateway emits on `/admin/approvals/stream`.
 *
 * - `pending`: a new prompt-mode tool call is awaiting approval.
 * - `decided`: an approval row was resolved (either via this admin UI or
 *   because the gate-side timeout expired).
 * - `lag`: broadcast receiver dropped one or more frames; UI shows a
 *   banner and the next React-Query poll will resync ground truth.
 *
 * The Rust side (see `broadcast_to_sse` in `approvals.rs`) emits `pending`
 * and `decided` with SSE's default `"message"` event name, and `lag` with
 * a named `"lag"` event.
 */
export type StreamEvent =
  | { kind: "pending"; approval: Approval }
  | { kind: "decided"; id: string; decision: string; reason: string | null };

export type Tab = "pending" | "history";
