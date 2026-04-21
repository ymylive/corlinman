/**
 * corlinman admin API client.
 *
 * Two fetch paths, picked per call:
 *   1. `NEXT_PUBLIC_MOCK_API_URL` set → hit the standalone mock server
 *      (see ui/mock/server.ts; default http://127.0.0.1:7777).
 *   2. Otherwise → real gateway at `NEXT_PUBLIC_GATEWAY_URL`. Default is an
 *      empty string so request paths resolve relative to the current origin
 *      (nginx proxies `/admin/*`, `/health`, `/v1/*`, `/metrics`, and
 *      `/plugin-callback` to the gateway in production). `credentials:
 *      "include"` forwards the session cookie the gateway sets.
 *
 * For local dev without a reverse proxy, set `NEXT_PUBLIC_GATEWAY_URL=
 * http://localhost:6005` as an opt-in escape hatch.
 *
 * M6 note: admin endpoints are HTTP Basic right now — either hit them from a
 * browser after a Basic-auth prompt or set the `Authorization` header on the
 * fetch from a server-side helper. Cookie/session auth lands in M7.
 *
 * The inline `opts.mock` escape hatch is kept for local dev without a
 * gateway running: set `NEXT_PUBLIC_MOCK_MODE=1` to enable it.
 */

export const GATEWAY_BASE_URL = process.env.NEXT_PUBLIC_GATEWAY_URL ?? "";

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

// ---------------------------------------------------------------------------
// S6 T1 — RAG admin surface
// ---------------------------------------------------------------------------

export interface RagStats {
  ready: boolean;
  files: number;
  chunks: number;
  tags: number;
}
export function fetchRagStats(): Promise<RagStats> {
  return apiFetch<RagStats>("/admin/rag/stats");
}

export interface RagHit {
  chunk_id: number;
  score: number;
  content_preview: string;
}
export interface RagQueryResponse {
  backend: string;
  q: string;
  k: number;
  hits: RagHit[];
}
export function queryRag(q: string, k = 10): Promise<RagQueryResponse> {
  const qs = new URLSearchParams({ q, k: String(k) }).toString();
  return apiFetch<RagQueryResponse>(`/admin/rag/query?${qs}`);
}
export function rebuildRag(): Promise<{ status: string; target: string }> {
  return apiFetch<{ status: string; target: string }>("/admin/rag/rebuild", {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// S6 T2 — QQ channel admin surface
// ---------------------------------------------------------------------------

export interface QqStatus {
  configured: boolean;
  enabled: boolean;
  ws_url: string | null;
  self_ids: number[];
  group_keywords: Record<string, string[]>;
  runtime: "unknown" | "connected" | "disconnected";
  recent_messages: unknown[];
}
export function fetchQqStatus(): Promise<QqStatus> {
  return apiFetch<QqStatus>("/admin/channels/qq/status");
}
export function reconnectQq(): Promise<unknown> {
  return apiFetch("/admin/channels/qq/reconnect", { method: "POST" });
}
export function updateQqKeywords(
  groupKeywords: Record<string, string[]>,
): Promise<{ status: string; group_keywords: Record<string, string[]> }> {
  return apiFetch("/admin/channels/qq/keywords", {
    method: "POST",
    body: { group_keywords: groupKeywords },
  });
}

// ---------------------------------------------------------------------------
// S6 T3 — Scheduler admin surface
// ---------------------------------------------------------------------------

export interface SchedulerJob {
  name: string;
  cron: string;
  timezone: string | null;
  action_kind: "run_agent" | "run_tool";
  next_fire_at: string | null;
  last_status: string | null;
}
export function fetchSchedulerJobs(): Promise<SchedulerJob[]> {
  return apiFetch<SchedulerJob[]>("/admin/scheduler/jobs");
}
export interface SchedulerHistory {
  job: string;
  at: string;
  source: string;
  status: string;
  message: string;
}
export function fetchSchedulerHistory(): Promise<SchedulerHistory[]> {
  return apiFetch<SchedulerHistory[]>("/admin/scheduler/history");
}
export function triggerSchedulerJob(name: string): Promise<unknown> {
  return apiFetch(`/admin/scheduler/jobs/${encodeURIComponent(name)}/trigger`, {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// S6 T4 — Config admin surface
// ---------------------------------------------------------------------------

export interface ConfigGetResponse {
  toml: string;
  version: string;
  meta: Record<string, unknown>;
}
export function fetchConfig(): Promise<ConfigGetResponse> {
  return apiFetch<ConfigGetResponse>("/admin/config");
}
export interface ConfigIssue {
  path: string;
  code: string;
  message: string;
  level: "error" | "warn";
}
export interface ConfigPostResponse {
  status: "ok" | "invalid";
  issues: ConfigIssue[];
  requires_restart: string[];
  version?: string;
}
export function postConfig(
  toml: string,
  dryRun: boolean,
): Promise<ConfigPostResponse> {
  return apiFetch<ConfigPostResponse>("/admin/config", {
    method: "POST",
    body: { toml, dry_run: dryRun },
  });
}
export function fetchConfigSchema(): Promise<unknown> {
  return apiFetch("/admin/config/schema");
}

// ---------------------------------------------------------------------------
// S6 T5 — Models admin surface
// ---------------------------------------------------------------------------

export interface ProviderRow {
  name: string;
  enabled: boolean;
  has_api_key: boolean;
  api_key_kind: "env" | "literal" | null;
  base_url: string | null;
}
export interface ModelsResponse {
  default: string;
  aliases: Record<string, string>;
  providers: ProviderRow[];
}
export function fetchModels(): Promise<ModelsResponse> {
  return apiFetch<ModelsResponse>("/admin/models");
}
export function updateAliases(
  aliases: Record<string, string>,
  defaultModel?: string,
): Promise<{ status: string; default: string; aliases: Record<string, string> }> {
  return apiFetch("/admin/models/aliases", {
    method: "POST",
    body: { aliases, default: defaultModel },
  });
}

// ---------------------------------------------------------------------------
// S6 T6 — Plugin invoke + Agent editor
// ---------------------------------------------------------------------------

export interface PluginInvokeResponse {
  status: "success" | "error" | "accepted";
  duration_ms: number;
  result?: unknown;
  result_raw?: string | null;
  code?: number;
  message?: string;
  task_id?: string;
}
export function invokePlugin(
  name: string,
  tool: string,
  args: unknown,
): Promise<PluginInvokeResponse> {
  return apiFetch<PluginInvokeResponse>(
    `/admin/plugins/${encodeURIComponent(name)}/invoke`,
    { method: "POST", body: { tool, arguments: args } },
  );
}

export interface PluginDetail {
  summary: PluginSummary;
  manifest: Record<string, unknown>;
  diagnostics: unknown[];
}
export function fetchPluginDetail(name: string): Promise<PluginDetail> {
  return apiFetch<PluginDetail>(`/admin/plugins/${encodeURIComponent(name)}`);
}

export interface AgentContent {
  name: string;
  file_path: string;
  bytes: number;
  last_modified: string | null;
  content: string;
}
export function fetchAgent(name: string): Promise<AgentContent> {
  return apiFetch<AgentContent>(`/admin/agents/${encodeURIComponent(name)}`);
}
export function saveAgent(
  name: string,
  content: string,
): Promise<{ status: string; name: string; file_path: string; bytes: number }> {
  return apiFetch(`/admin/agents/${encodeURIComponent(name)}`, {
    method: "POST",
    body: { content },
  });
}

// ---------------------------------------------------------------------------
// UI redesign — health + dashboard metrics
// ---------------------------------------------------------------------------

export interface HealthCheck {
  name: string;
  ok: boolean;
  detail?: string;
  checked_at?: string;
}

export interface HealthStatus {
  status: "ok" | "healthy" | "degraded" | "warn" | "unhealthy" | string;
  checks?: HealthCheck[];
  version?: string;
}

/**
 * GET /health — returns aggregated gateway health. The gateway exposes a
 * simple JSON shape; we accept the loosest superset so older 200/OK-string
 * responses still parse.
 */
export async function fetchHealth(): Promise<HealthStatus> {
  return apiFetch<HealthStatus>("/health");
}

