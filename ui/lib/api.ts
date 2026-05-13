/**
 * corlinman admin API client.
 *
 * Always hits the real gateway at `NEXT_PUBLIC_GATEWAY_URL`. Default
 * is an empty string so request paths resolve relative to the current
 * origin (nginx proxies `/admin/*`, `/health`, `/v1/*`, `/metrics`,
 * and `/plugin-callback` to the gateway in production). `credentials:
 * "include"` forwards the session cookie the gateway sets.
 *
 * For local dev without a reverse proxy, set
 * `NEXT_PUBLIC_GATEWAY_URL=http://localhost:6005` as an opt-in escape
 * hatch.
 */

export const GATEWAY_BASE_URL = process.env.NEXT_PUBLIC_GATEWAY_URL ?? "";

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
}

/** Thin fetch wrapper; throws CorlinmanApiError on non-2xx. */
export async function apiFetch<T>(
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const { body, headers, ...rest } = opts;

  const res = await fetch(`${GATEWAY_BASE_URL}${path}`, {
    credentials: "include",
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

// --- typed admin surfaces ---------------------------------------------------
// All hit live `corlinman-gateway::routes::admin` endpoints.

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
  return apiFetch<PluginSummary[]>("/admin/plugins");
}

export interface AgentSummary {
  name: string;
  file_path: string;
  bytes: number;
  last_modified: string;
}

export async function listAgents(): Promise<AgentSummary[]> {
  return apiFetch<AgentSummary[]>("/admin/agents");
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
  return apiFetch<ApprovalItem[]>("/admin/approvals");
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

// v0.3 — QQ scan-login (NapCat proxy). Four endpoints:
//   POST /admin/channels/qq/qrcode         → { token, image_base64?, qrcode_url?, expires_at }
//   GET  /admin/channels/qq/qrcode/status  → { status, account?, message? }
//   GET  /admin/channels/qq/accounts       → { accounts: QqAccount[] }
//   POST /admin/channels/qq/quick-login    → { status, account?, message? }
export interface QqAccount {
  uin: string;
  nickname?: string;
  avatar_url?: string;
  /** epoch-ms */
  last_login_at: number;
}
export interface QqQrcode {
  token: string;
  /** Base64 PNG (no data: prefix) when NapCat returned an image. */
  image_base64: string | null;
  /** ptqrshow URL when NapCat returned a URL instead of an image. */
  qrcode_url: string | null;
  /** epoch-ms expiry. */
  expires_at: number;
}
export type QqLoginStatus =
  | "waiting"
  | "scanned"
  | "confirmed"
  | "expired"
  | "error";
export interface QqQrcodeStatus {
  status: QqLoginStatus;
  account?: QqAccount;
  message?: string;
}
export function requestQqQrcode(): Promise<QqQrcode> {
  return apiFetch<QqQrcode>("/admin/channels/qq/qrcode", { method: "POST" });
}
export function fetchQqQrcodeStatus(token: string): Promise<QqQrcodeStatus> {
  const qs = new URLSearchParams({ token });
  return apiFetch<QqQrcodeStatus>(
    `/admin/channels/qq/qrcode/status?${qs.toString()}`,
  );
}
export function fetchQqAccounts(): Promise<{ accounts: QqAccount[] }> {
  return apiFetch<{ accounts: QqAccount[] }>("/admin/channels/qq/accounts");
}
export function qqQuickLogin(uin: string): Promise<QqQrcodeStatus> {
  return apiFetch<QqQrcodeStatus>("/admin/channels/qq/quick-login", {
    method: "POST",
    body: { uin },
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
// Channel enable toggle — convenience wrappers over /admin/config that
// mutate only the `enabled` field of `[channels.qq]` / `[channels.telegram]`
// while preserving the rest of the TOML (including comments and ordering).
//
// Regex-scoped to a single `enabled = true|false` line inside the addressed
// section header. Trailing comments on that line are preserved. If the
// section or the `enabled` key is missing, it's appended/inserted rather
// than touching anything else.
// ---------------------------------------------------------------------------

export type ChannelName = "qq" | "telegram";

/** Read the current `enabled` flag for a channel from a TOML string. */
export function readChannelEnabled(toml: string, channel: ChannelName): boolean {
  const headerRe = new RegExp(`^\\[channels\\.${channel}\\]\\s*$`, "m");
  const headerMatch = headerRe.exec(toml);
  if (!headerMatch) return false;
  const body = sectionBody(toml, headerMatch.index + headerMatch[0].length);
  const enabledMatch = /^\s*enabled\s*=\s*(true|false)/m.exec(body);
  return enabledMatch?.[1] === "true";
}

/**
 * Return `toml` with the `enabled` field of `[channels.<channel>]` set to
 * `next`. Creates the section if absent. Preserves the rest of the file.
 * Exported for unit testing — prefer `setChannelEnabled()` in UI code.
 */
export function patchChannelEnabled(
  toml: string,
  channel: ChannelName,
  next: boolean,
): string {
  const header = `[channels.${channel}]`;
  const headerRe = new RegExp(`^\\[channels\\.${channel}\\]\\s*$`, "m");
  const headerMatch = headerRe.exec(toml);

  if (!headerMatch) {
    const sep = toml.endsWith("\n\n") ? "" : toml.endsWith("\n") ? "\n" : "\n\n";
    return `${toml}${sep}${header}\nenabled = ${next}\n`;
  }

  const headerStart = headerMatch.index;
  const headerEnd = headerStart + headerMatch[0].length;
  const body = sectionBody(toml, headerEnd);
  const bodyEnd = headerEnd + body.length;

  const enabledRe = /^(\s*enabled\s*=\s*)(true|false)/m;
  if (enabledRe.test(body)) {
    const newBody = body.replace(enabledRe, `$1${next}`);
    return toml.slice(0, headerEnd) + newBody + toml.slice(bodyEnd);
  }

  // Section exists but lacks an `enabled` line — insert right after header.
  const insertion = `\nenabled = ${next}`;
  return toml.slice(0, headerEnd) + insertion + body + toml.slice(bodyEnd);
}

/** Extract the body of a TOML section starting at `from` up to the next `^\[` header or EOF. */
function sectionBody(toml: string, from: number): string {
  const rest = toml.slice(from);
  const nextHeader = /\n\[/.exec(rest);
  return nextHeader ? rest.slice(0, nextHeader.index) : rest;
}

/**
 * Fetch current config, flip `channels.<channel>.enabled`, POST back.
 * Returns the raw `/admin/config` response — callers should inspect
 * `status === "invalid"` and surface `issues` to the user (e.g. Telegram
 * rejecting enable when `bot_token` is missing).
 */
export async function setChannelEnabled(
  channel: ChannelName,
  enabled: boolean,
): Promise<ConfigPostResponse> {
  const current = await fetchConfig();
  const nextToml = patchChannelEnabled(current.toml, channel, enabled);
  return postConfig(nextToml, false);
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
  /** Normalised boolean (true iff status === "ok"). Populated by fetchHealth. */
  ok: boolean;
  /** Raw gateway status string ("ok" | "warn" | "unhealthy" | ...). */
  status?: string;
  detail?: string;
  checked_at?: string;
}

interface GatewayHealthCheck {
  name: string;
  status: string;
  detail?: string;
  checked_at?: string;
}

export interface HealthStatus {
  status: "ok" | "healthy" | "degraded" | "warn" | "unhealthy" | string;
  checks?: HealthCheck[];
  version?: string;
}

interface GatewayHealthStatus {
  status: string;
  checks?: GatewayHealthCheck[];
  version?: string;
}

/**
 * GET /health — returns aggregated gateway health.
 *
 * The gateway reports each check as `{ name, status, detail }` where
 * `status` is a string ("ok" / "warn" / "unhealthy" / ...). The admin UI
 * wants a boolean, so we normalise here — `ok` is true iff the raw
 * `status` equals "ok".
 */
export async function fetchHealth(): Promise<HealthStatus> {
  const raw = await apiFetch<GatewayHealthStatus>("/health");
  return {
    status: raw.status,
    version: raw.version,
    checks: (raw.checks ?? []).map((c) => ({
      name: c.name,
      status: c.status,
      ok: c.status === "ok",
      detail: c.detail,
      checked_at: c.checked_at,
    })),
  };
}

// ---------------------------------------------------------------------------
// Feature C (v0.2) — custom providers + per-alias params + embedding
//
// Contract: docs/feature-c contract (see Python/Rust counterparts). All
// requests go through admin auth middleware. 503 from any of these
// endpoints means the gateway is v0.1.x and has not been upgraded yet — the
// UI renders a "backend feature pending" empty state (do not toast).
// ---------------------------------------------------------------------------

export type ProviderKind =
  | "anthropic"
  | "openai"
  | "google"
  | "deepseek"
  | "qwen"
  | "glm"
  | "openai_compatible"
  // Free-form-providers refactor: market LLMs surfaced as named kinds even
  // though they all run through the OpenAI-compatible backend today.
  | "mistral"
  | "cohere"
  | "together"
  | "groq"
  | "replicate"
  | "bedrock"
  | "azure";

/** Loose JSON Schema (draft 2020-12) — enough for the subset we render. */
export type JSONSchema = {
  type?: "string" | "number" | "integer" | "boolean" | "object" | "array";
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  format?: string;
  properties?: Record<string, JSONSchema>;
  required?: string[];
  additionalProperties?: boolean | JSONSchema;
  items?: JSONSchema;
  // Tolerate other fields without breaking.
  [key: string]: unknown;
};

export type ProviderCapabilities = {
  embedding?: boolean;
  chat?: boolean;
};

export interface ProviderView {
  name: string;
  kind: ProviderKind;
  enabled: boolean;
  base_url: string | null;
  api_key_source: "env" | "value" | "unset";
  api_key_env_name: string | null;
  params: Record<string, unknown>;
  params_schema: JSONSchema;
  capabilities?: ProviderCapabilities;
}

export interface ProviderUpsert {
  name: string;
  kind: ProviderKind;
  enabled?: boolean;
  base_url?: string;
  api_key?: { env: string } | { value: string } | null;
  params?: Record<string, unknown>;
}

export interface ProvidersResponse {
  providers: ProviderView[];
}

export async function fetchProviders(): Promise<ProviderView[]> {
  const res = await apiFetch<ProvidersResponse>("/admin/providers");
  return res.providers ?? [];
}

export async function upsertProvider(
  body: ProviderUpsert,
): Promise<ProviderView> {
  return apiFetch<ProviderView>("/admin/providers", {
    method: "POST",
    body,
  });
}

export async function deleteProvider(name: string): Promise<void> {
  await apiFetch<void>(`/admin/providers/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

/** Server returns 409 with `{ error, references: string[] }` when a
 * provider is still referenced by an alias or by embedding. Surface the list
 * so the UI can explain why the delete was refused. */
export interface ProviderConflict {
  error: string;
  references: string[];
}

export interface AliasView {
  name: string;
  provider: string;
  model: string;
  params: Record<string, unknown>;
  effective_params_schema: JSONSchema;
}

export interface AliasUpsert {
  name: string;
  provider: string;
  model: string;
  params?: Record<string, unknown>;
}

/** Extended /admin/models response — aliases now carry params + the
 * merged schema the UI should render. The legacy (string-map) shape is
 * still served by v0.1 gateways and handled in ModelsPage. */
export interface ModelsResponseV2 {
  default: string;
  providers: ProviderView[];
  aliases: AliasView[];
}

export async function fetchModelsV2(): Promise<ModelsResponseV2> {
  return apiFetch<ModelsResponseV2>("/admin/models");
}

export async function upsertAlias(body: AliasUpsert): Promise<AliasView> {
  return apiFetch<AliasView>("/admin/models/aliases", {
    method: "POST",
    body,
  });
}

export async function deleteAlias(name: string): Promise<void> {
  await apiFetch<void>(
    `/admin/models/aliases/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
}

export interface EmbeddingView {
  provider: string;
  model: string;
  dimension: number;
  enabled: boolean;
  params: Record<string, unknown>;
  params_schema: JSONSchema;
}

export type EmbeddingUpsert = EmbeddingView;

export async function fetchEmbedding(): Promise<EmbeddingView> {
  return apiFetch<EmbeddingView>("/admin/embedding");
}

export async function upsertEmbedding(
  body: EmbeddingUpsert,
): Promise<EmbeddingView> {
  return apiFetch<EmbeddingView>("/admin/embedding", {
    method: "POST",
    body,
  });
}

export interface BenchmarkView {
  dimension: number;
  latency_ms_p50: number;
  latency_ms_p99: number;
  similarity_matrix: number[][];
  warnings: string[];
}

export async function benchmarkEmbedding(
  samples: string[],
): Promise<BenchmarkView> {
  return apiFetch<BenchmarkView>("/admin/embedding/benchmark", {
    method: "POST",
    body: { samples },
  });
}

// ---------------------------------------------------------------------------
// Wave 1-D — EvolutionLoop proposal queue
//
// Mirrors the gateway routes in
// rust/crates/corlinman-gateway/src/routes/admin/evolution.rs.
// ---------------------------------------------------------------------------

export type EvolutionRisk = "low" | "medium" | "high";

/**
 * MetricSnapshot — mirrors the Rust `corlinman_auto_rollback::metrics::
 * MetricSnapshot`. Written into `evolution_history.metrics_baseline` at
 * apply time and into `evolution_proposals.baseline_metrics_json` by the
 * ShadowTester. Both surfaces feed the UI's `<MetricsDelta />` viz.
 */
export interface MetricSnapshot {
  target: string;
  /** epoch-ms */
  captured_at_ms: number;
  window_secs: number;
  /** event_kind → count over the window. Stable shape across snapshots. */
  counts: Record<string, number>;
}

export interface EvolutionProposal {
  id: string;
  kind: string;
  target: string;
  diff: string;
  reasoning: string;
  risk: EvolutionRisk;
  status: string;
  /** Serialized `ShadowMetrics` (free-form per-kind shape). Populated on
   * `shadow_done` rows — used by `MetricsDelta` for the post-shadow leg. */
  shadow_metrics?: Record<string, unknown>;
  signal_ids: number[];
  trace_ids: string[];
  /** epoch-ms */
  created_at: number;
  decided_at?: number;
  decided_by?: string;
  applied_at?: number;
  /** W1-A: identifier of the eval run that produced `shadow_metrics`. */
  eval_run_id?: string;
  /** W1-A: pre-shadow baseline `MetricSnapshot` JSON. */
  baseline_metrics_json?: MetricSnapshot;
  /** W1-B: epoch-ms the AutoRollback monitor flipped this row. */
  auto_rollback_at?: number;
  /** W1-B: human-readable threshold-breach reason from the monitor. */
  auto_rollback_reason?: string;
}

/**
 * One row in `GET /admin/evolution/history`. Mirrors `HistoryEntryOut`
 * in `rust/crates/corlinman-gateway/src/routes/admin/evolution.rs`.
 *
 * `metrics_baseline` is the `MetricSnapshot` JSON the W1-B applier wrote
 * at apply time. `shadow_metrics` + `baseline_metrics_json` come from
 * the original proposals row so the UI can render the full lineage of
 * baseline → shadow → post-apply on one card.
 */
export interface HistoryEntry {
  proposal_id: string;
  kind: string;
  target: string;
  risk: EvolutionRisk;
  /** "applied" | "rolled_back". */
  status: string;
  /** epoch-ms */
  applied_at: number;
  /** epoch-ms; null while the proposal is still applied. */
  rolled_back_at: number | null;
  /** Manual-rollback reason from the history table. */
  rollback_reason: string | null;
  /** Auto-rollback breach summary from the proposals row. */
  auto_rollback_reason: string | null;
  metrics_baseline: MetricSnapshot;
  shadow_metrics: Record<string, unknown> | null;
  baseline_metrics_json: MetricSnapshot | null;
  before_sha: string;
  after_sha: string;
  eval_run_id: string | null;
  reasoning: string;
}

export function fetchEvolutionPending(): Promise<EvolutionProposal[]> {
  return apiFetch<EvolutionProposal[]>(
    "/admin/evolution?status=pending&limit=50",
  );
}

export function fetchEvolutionApproved(): Promise<EvolutionProposal[]> {
  return apiFetch<EvolutionProposal[]>(
    "/admin/evolution?status=approved&limit=50",
  );
}

export function fetchEvolutionHistory(): Promise<HistoryEntry[]> {
  return apiFetch<HistoryEntry[]>("/admin/evolution/history?limit=50");
}

/** POST /admin/evolution/:id/apply — flips approved→applied and runs the
 * EvolutionApplier. Used by the Approved tab; mirrors the existing
 * approve/deny mutations. */
export interface EvolutionApplyResult {
  id: string;
  status: string;
  history_id?: number;
}

export function applyEvolutionProposal(
  id: string,
): Promise<EvolutionApplyResult> {
  return apiFetch<EvolutionApplyResult>(
    `/admin/evolution/${encodeURIComponent(id)}/apply`,
    { method: "POST" },
  );
}

export interface EvolutionDecideResult {
  id: string;
  status: string;
  decided_at?: number;
  decided_by?: string;
}

export function approveEvolutionProposal(
  id: string,
  decided_by: string,
): Promise<EvolutionDecideResult> {
  return apiFetch<EvolutionDecideResult>(
    `/admin/evolution/${encodeURIComponent(id)}/approve`,
    {
      method: "POST",
      body: { decided_by },
    },
  );
}

export function denyEvolutionProposal(
  id: string,
  decided_by: string,
  reason?: string,
): Promise<EvolutionDecideResult> {
  return apiFetch<EvolutionDecideResult>(
    `/admin/evolution/${encodeURIComponent(id)}/deny`,
    {
      method: "POST",
      body: { decided_by, reason },
    },
  );
}

// ---------------------------------------------------------------------------
// Wave 1-C — weekly EvolutionProposal budget
//
// Mirrors GET /admin/evolution/budget on the gateway. `per_kind` may be empty
// when no kind-level caps are configured; `enabled` is false by default until
// the operator opts the gate in.
// ---------------------------------------------------------------------------

export interface BudgetSlot {
  limit: number;
  used: number;
  remaining: number;
}

export interface BudgetPerKindEntry extends BudgetSlot {
  kind: string;
}

export interface BudgetSnapshot {
  enabled: boolean;
  window_start_ms: number;
  window_end_ms: number;
  weekly_total: BudgetSlot;
  per_kind: BudgetPerKindEntry[];
}

export function fetchBudget(): Promise<BudgetSnapshot> {
  return apiFetch<BudgetSnapshot>("/admin/evolution/budget");
}


// ---------------------------------------------------------------------------
// newapi connector — both /admin/newapi (post-onboard) and the
// /admin/onboard/newapi/* stateless wizard endpoints.
// ---------------------------------------------------------------------------

export interface NewapiChannel {
  id: number;
  name: string;
  type: number;
  status: number;
  models: string;
  group?: string;
  priority?: number;
  used_quota?: number;
  remain_quota?: number;
  test_time?: number;
  response_time?: number;
}

export interface NewapiSummary {
  connection: {
    base_url: string;
    token_masked: string;
    admin_key_present: boolean;
    enabled: boolean;
  };
  status: "ok" | "degraded";
}

export interface NewapiConnectionInput {
  base_url: string;
  token: string;
  admin_token?: string;
}

export interface NewapiProbeResult {
  next?: string;
  base_url: string;
  user: { id: number; username: string; role: number; status: number };
  server_version?: string;
  channels_available?: number;
}

export interface NewapiTestResult {
  status: number;
  latency_ms: number;
  model: string | null;
}

export function fetchNewapiSummary(): Promise<NewapiSummary> {
  return apiFetch<NewapiSummary>("/admin/newapi");
}

export function fetchNewapiChannels(
  type: "llm" | "embedding" | "tts",
): Promise<{ channels: NewapiChannel[] }> {
  return apiFetch(`/admin/newapi/channels?type=${type}`);
}

export function testNewapi(model: string): Promise<NewapiTestResult> {
  return apiFetch<NewapiTestResult>("/admin/newapi/test", {
    method: "POST",
    body: { model },
  });
}

export function patchNewapi(body: Partial<NewapiConnectionInput>): Promise<{
  ok: boolean;
}> {
  return apiFetch("/admin/newapi", { method: "PATCH", body });
}

// onboard wizard
export function probeNewapi(input: NewapiConnectionInput): Promise<NewapiProbeResult> {
  return apiFetch<NewapiProbeResult>("/admin/onboard/newapi/probe", {
    method: "POST",
    body: input,
  });
}

export function listOnboardChannels(
  input: NewapiConnectionInput,
  type: "llm" | "embedding" | "tts",
): Promise<{ channels: NewapiChannel[] }> {
  return apiFetch("/admin/onboard/newapi/channels", {
    method: "POST",
    body: { ...input, type },
  });
}

export interface OnboardFinalizeBody {
  base_url: string;
  token: string;
  admin_token?: string;
  llm: { channel_id?: number; model: string };
  embedding: { channel_id?: number; model: string; dimension?: number };
  tts?: { channel_id?: number; model: string; voice?: string };
}

export function finalizeOnboard(body: OnboardFinalizeBody): Promise<{
  ok: boolean;
  redirect: string;
}> {
  return apiFetch("/admin/onboard/finalize", {
    method: "POST",
    body,
  });
}
