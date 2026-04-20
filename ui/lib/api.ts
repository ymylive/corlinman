/**
 * corlinman admin API client.
 *
 * Two fetch paths, picked per call:
 *   1. `NEXT_PUBLIC_MOCK_API_URL` set → hit the standalone mock server
 *      (see ui/mock/server.ts; default http://127.0.0.1:7777).
 *   2. Otherwise → real gateway at `NEXT_PUBLIC_GATEWAY_URL`
 *      (default http://localhost:6005), with `credentials: "include"` so
 *      the browser forwards any auth cookie the gateway sets.
 *
 * M6 note: admin endpoints are HTTP Basic right now — either hit them from a
 * browser after a Basic-auth prompt or set the `Authorization` header on the
 * fetch from a server-side helper. Cookie/session auth lands in M7.
 *
 * The inline `opts.mock` escape hatch is kept for local dev without a
 * gateway running: set `NEXT_PUBLIC_MOCK_MODE=1` to enable it.
 */

export const GATEWAY_BASE_URL =
  process.env.NEXT_PUBLIC_GATEWAY_URL ?? "http://localhost:6005";

/** Empty string means "no mock server"; any non-empty value routes all calls there. */
export const MOCK_API_URL = process.env.NEXT_PUBLIC_MOCK_API_URL ?? "";

/** Opt-in inline mock for offline dev. Off by default now that the gateway is wired. */
export const MOCK_MODE = process.env.NEXT_PUBLIC_MOCK_MODE === "1";

export interface ApiError extends Error {
  status?: number;
  traceId?: string;
}

export class CorlinmanApiError extends Error implements ApiError {
  status?: number;
  traceId?: string;
  constructor(message: string, status?: number, traceId?: string) {
    super(message);
    this.name = "CorlinmanApiError";
    this.status = status;
    this.traceId = traceId;
  }
}

export interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  /** Inline mock payload returned when MOCK_MODE is true and no mock server URL is set. */
  mock?: unknown;
}

function resolveBaseUrl(): string {
  if (MOCK_API_URL) return MOCK_API_URL;
  return GATEWAY_BASE_URL;
}

/** Thin fetch wrapper; throws CorlinmanApiError on non-2xx. */
export async function apiFetch<T>(
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const useInlineMock = MOCK_MODE && !MOCK_API_URL && opts.mock !== undefined;
  if (useInlineMock) {
    // Simulate a short network roundtrip so loading states render in dev.
    await new Promise((r) => setTimeout(r, 120));
    return opts.mock as T;
  }

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { body, mock: _mock, headers, ...rest } = opts;
  const base = resolveBaseUrl();
  // Mock server does not require credentials; real gateway does.
  const credentials: RequestCredentials = MOCK_API_URL ? "omit" : "include";

  const res = await fetch(`${base}${path}`, {
    credentials,
    headers: {
      "content-type": "application/json",
      ...(headers ?? {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    ...rest,
  });

  const traceId = res.headers.get("x-request-id") ?? undefined;
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new CorlinmanApiError(
      text || `Request failed: ${res.status}`,
      res.status,
      traceId,
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// --- typed admin surfaces (stubs) -------------------------------------------
// Each one returns mock data today; M6 replaces `mock:` with real payloads
// served by corlinman-gateway::routes::admin.

export type PluginStatus = "loaded" | "disabled" | "error";

export interface PluginSummary {
  name: string;
  version: string;
  status: PluginStatus;
  manifest_path: string;
  origin: "Bundled" | "Global" | "Workspace" | "Config";
  plugin_type: "synchronous" | "asynchronous" | "messagePreprocessor";
  capabilities: string[];
  description: string;
  last_touched_at: string;
  error?: string;
}

export async function listPlugins(): Promise<PluginSummary[]> {
  return apiFetch<PluginSummary[]>("/admin/plugins", {
    mock: [
      {
        name: "DailyNote",
        version: "2.0.0",
        status: "loaded",
        manifest_path: "Plugin/DailyNote/plugin-manifest.json",
        origin: "Workspace",
        plugin_type: "synchronous",
        capabilities: ["create", "update"],
        description: "日记系统 (创建与更新)",
        last_touched_at: "2026-04-19T14:22:10Z",
      },
    ] satisfies PluginSummary[],
  });
}

export interface AgentSummary {
  name: string;
  file_path: string;
  bytes: number;
  last_modified: string;
}

export async function listAgents(): Promise<AgentSummary[]> {
  return apiFetch<AgentSummary[]>("/admin/agents", {
    mock: [
      {
        name: "Aemeath",
        file_path: "Agent/Aemeath.txt",
        bytes: 18234,
        last_modified: "2026-04-20T09:32:11Z",
      },
    ] satisfies AgentSummary[],
  });
}

export interface ApprovalItem {
  id: string;
  plugin: string;
  tool: string;
  sessionKey: string;
  requestedAt: string;
  argsPreview: string;
}

export async function listPendingApprovals(): Promise<ApprovalItem[]> {
  return apiFetch<ApprovalItem[]>("/admin/approvals", {
    mock: [
      {
        id: "apv_01HXYZ",
        plugin: "FileOperator",
        tool: "write",
        sessionKey: "qq:group:123456",
        requestedAt: "2026-04-20T06:11:02Z",
        argsPreview: '{ "path": "./notes.md", "content": "..." }',
      },
    ],
  });
}

// --- Approvals (S2 T3 wired, S5 T4 expanded with batch helper) -------------
// Matches the Rust `ApprovalOut` shape in
// rust/crates/corlinman-gateway/src/routes/admin/approvals.rs.
export interface Approval {
  id: string;
  plugin: string;
  tool: string;
  session_key: string;
  args_json: string;
  requested_at: string;
  decided_at: string | null;
  decision: string | null;
}

export function fetchApprovals(includeDecided: boolean): Promise<Approval[]> {
  const qs = includeDecided ? "?include_decided=true" : "";
  return apiFetch<Approval[]>(`/admin/approvals${qs}`);
}

export interface DecideResult {
  id: string;
  decision: string;
}

export function decideApproval(
  id: string,
  approve: boolean,
  reason?: string,
): Promise<DecideResult> {
  return apiFetch<DecideResult>(`/admin/approvals/${id}/decide`, {
    method: "POST",
    body: { approve, reason },
  });
}

/** Outcome of a single decide call inside a batch. */
export interface BatchDecideOutcome {
  id: string;
  ok: boolean;
  error?: string;
}

/** Fires every decide in parallel with `Promise.allSettled` and reports per-id
 * outcomes so the caller can revert optimistic updates for the failed ones. */
export async function decideApprovalsBatch(
  ids: string[],
  approve: boolean,
  reason?: string,
): Promise<BatchDecideOutcome[]> {
  const results = await Promise.allSettled(
    ids.map((id) => decideApproval(id, approve, reason)),
  );
  return results.map((r, i) => {
    const id = ids[i]!;
    if (r.status === "fulfilled") return { id, ok: true };
    const msg = r.reason instanceof Error ? r.reason.message : String(r.reason);
    return { id, ok: false, error: msg };
  });
}

/** Convenience re-export for callers that want the SSE helper. */
export { openEventStream } from "./sse";
