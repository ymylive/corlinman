/**
 * Standalone mock admin API server for corlinman UI dev.
 *
 * Why standalone: the Rust gateway admin routes don't land until M6
 * (plan §7-§9). Until then the Next.js UI needs somewhere to hit so
 * plugins / agents / logs pages can render real-ish data. Zero
 * external dependencies (node:http only) so it doesn't bloat the
 * lockfile and cannot accidentally ship in the production image.
 *
 * Endpoints:
 *   GET  /admin/plugins        → MockPlugin[]
 *   GET  /admin/agents         → MockAgent[]
 *   GET  /admin/logs/stream    → SSE stream of MockLogEvent
 *   GET  /admin/tenants        → { tenants, allowed }   (Phase 4 W1 4-1B)
 *   POST /admin/tenants        → 201 { tenant_id }       (Phase 4 W1 4-1B)
 *   GET  /healthz              → { ok: true }
 *
 * TODO(M6): retire in favour of the real corlinman-gateway admin
 * surface (see plan §7). Kept as a devDep-only tool.
 */

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import {
  MOCK_AGENTS,
  MOCK_PLUGINS,
  LOG_TEMPLATES,
  MOCK_HISTORY_APPROVALS,
  MOCK_PENDING_APPROVALS,
  MOCK_TENANTS,
  MOCK_TENANTS_ENABLED,
  MOCK_SESSIONS,
  makeMockReplay,
  genTraceId,
  type MockApproval,
  type MockTenant,
} from "./seed";

// Slug regex must mirror the Rust validator in corlinman-tenant
// (`^[a-z][a-z0-9-]{0,62}$`). Diverging from the Rust side defeats the
// point of mocking — the UI's only client-side validation is "non-empty";
// everything else flows through the server's 400 response.
const SLUG_RE = /^[a-z][a-z0-9-]{0,62}$/;

const PORT = Number(process.env.MOCK_PORT ?? 7777);
const HOST = process.env.MOCK_HOST ?? "127.0.0.1";

// Default to permissive CORS so Next.js dev server on :3000 can hit us.
const CORS_HEADERS: Record<string, string> = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,OPTIONS",
  "access-control-allow-headers": "content-type,x-request-id",
  "access-control-expose-headers": "x-request-id",
};

// In-memory approvals state for local dev. Mutated by POST decide + served
// via GET list. Reset when the process restarts.
const pendingState: MockApproval[] = MOCK_PENDING_APPROVALS.map((r) => ({
  ...r,
}));
const historyState: MockApproval[] = MOCK_HISTORY_APPROVALS.map((r) => ({
  ...r,
}));

// In-memory tenant registry. POST /admin/tenants pushes here so the page
// can refresh after a successful create and see the new row.
const tenantState: MockTenant[] = MOCK_TENANTS.map((r) => ({ ...r }));

function json(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "x-request-id": genTraceId(),
    ...CORS_HEADERS,
  });
  res.end(payload);
}

function notFound(res: ServerResponse, path: string): void {
  json(res, 404, { error: "not_found", path });
}

function openSse(req: IncomingMessage, res: ServerResponse): () => void {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    "x-accel-buffering": "no",
    ...CORS_HEADERS,
  });
  // Initial comment so proxies keep the connection open.
  res.write(": mock-log-stream online\n\n");

  let closed = false;
  const ticker = setInterval(() => {
    if (closed) return;
    const template =
      LOG_TEMPLATES[Math.floor(Math.random() * LOG_TEMPLATES.length)];
    const event = {
      ts: new Date().toISOString(),
      trace_id: genTraceId(),
      ...template,
    };
    res.write(`event: log\n`);
    res.write(`id: ${event.trace_id}\n`);
    res.write(`data: ${JSON.stringify(event)}\n\n`);
  }, 500);

  const cleanup = (): void => {
    if (closed) return;
    closed = true;
    clearInterval(ticker);
    try {
      res.end();
    } catch {
      // already closed
    }
  };

  req.on("close", cleanup);
  req.on("error", cleanup);
  return cleanup;
}

function readJsonBody(req: IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

function openApprovalsSse(req: IncomingMessage, res: ServerResponse): () => void {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    "x-accel-buffering": "no",
    ...CORS_HEADERS,
  });
  res.write(": mock-approvals-stream online\n\n");
  // Keep the connection warm without inventing fake approvals — real
  // traffic only fires when a new pending lands in the gateway; for the
  // mock server we just hold the channel open so reconnect logic can be
  // exercised manually (kill the process to trigger retry).
  const ka = setInterval(() => {
    res.write(": keep-alive\n\n");
  }, 15_000);
  const cleanup = () => {
    clearInterval(ka);
    try {
      res.end();
    } catch {
      // already closed
    }
  };
  req.on("close", cleanup);
  req.on("error", cleanup);
  return cleanup;
}

const server = createServer(async (req, res) => {
  const method = req.method ?? "GET";
  const url = req.url ?? "/";

  if (method === "OPTIONS") {
    res.writeHead(204, CORS_HEADERS);
    res.end();
    return;
  }

  // Strip query string before matching most routes; approvals list honours
  // `?include_decided=true` so we keep the query string there.
  const path = url.split("?")[0];
  const qs = url.includes("?") ? url.slice(url.indexOf("?") + 1) : "";

  if (method === "GET") {
    switch (path) {
      case "/healthz":
        json(res, 200, { ok: true, service: "corlinman-mock-api" });
        return;
      case "/admin/plugins":
        json(res, 200, MOCK_PLUGINS);
        return;
      case "/admin/agents":
        json(res, 200, MOCK_AGENTS);
        return;
      case "/admin/logs/stream":
        openSse(req, res);
        return;
      case "/admin/approvals": {
        const includeDecided = qs.includes("include_decided=true");
        json(
          res,
          200,
          includeDecided ? [...pendingState, ...historyState] : pendingState,
        );
        return;
      }
      case "/admin/approvals/stream":
        openApprovalsSse(req, res);
        return;
      case "/admin/sessions":
        // Phase 4 W2 4-2D — trajectory replay list. Mirrors the Rust
        // route's `{ sessions: SessionSummary[] }` shape.
        json(res, 200, { sessions: MOCK_SESSIONS });
        return;
      case "/admin/tenants": {
        if (!MOCK_TENANTS_ENABLED) {
          json(res, 403, { error: "tenants_disabled" });
          return;
        }
        json(res, 200, {
          tenants: tenantState,
          allowed: tenantState.map((t) => t.tenant_id),
        });
        return;
      }
      default:
        notFound(res, path);
        return;
    }
  }

  if (method === "POST") {
    if (path === "/admin/tenants") {
      if (!MOCK_TENANTS_ENABLED) {
        json(res, 403, { error: "tenants_disabled" });
        return;
      }
      let body: {
        slug?: unknown;
        display_name?: unknown;
        admin_username?: unknown;
        admin_password?: unknown;
      };
      try {
        body = (await readJsonBody(req)) as typeof body;
      } catch {
        json(res, 400, { error: "invalid_json" });
        return;
      }
      const slug = typeof body.slug === "string" ? body.slug : "";
      const displayName =
        typeof body.display_name === "string" && body.display_name.length > 0
          ? body.display_name
          : slug;
      const adminUsername =
        typeof body.admin_username === "string" ? body.admin_username : "";
      const adminPassword =
        typeof body.admin_password === "string" ? body.admin_password : "";
      if (!SLUG_RE.test(slug)) {
        json(res, 400, {
          error: "invalid_tenant_slug",
          reason: "slug must match ^[a-z][a-z0-9-]{0,62}$",
        });
        return;
      }
      if (!adminUsername || !adminPassword) {
        json(res, 400, {
          error: "invalid_tenant_slug",
          reason: "admin_username and admin_password are required",
        });
        return;
      }
      if (tenantState.find((t) => t.tenant_id === slug)) {
        json(res, 409, { error: "tenant_exists" });
        return;
      }
      const created: MockTenant = {
        tenant_id: slug,
        display_name: displayName,
        created_at: new Date().toISOString(),
      };
      tenantState.push(created);
      json(res, 201, { tenant_id: created.tenant_id });
      return;
    }

    const decideMatch = path.match(/^\/admin\/approvals\/([^/]+)\/decide$/);
    if (decideMatch) {
      const id = decideMatch[1]!;
      const idx = pendingState.findIndex((r) => r.id === id);
      if (idx === -1) {
        json(res, 404, { error: "not_found", resource: "approval", id });
        return;
      }
      let body: { approve?: boolean; reason?: string };
      try {
        body = (await readJsonBody(req)) as typeof body;
      } catch {
        json(res, 400, { error: "invalid_json" });
        return;
      }
      const approve = body.approve === true;
      const resolved: MockApproval = {
        ...pendingState[idx]!,
        decided_at: new Date().toISOString(),
        decision: approve ? "approved" : "denied",
      };
      pendingState.splice(idx, 1);
      historyState.unshift(resolved);
      json(res, 200, { id, decision: resolved.decision });
      return;
    }

    // Phase 4 W2 4-2D — replay endpoint:
    //   POST /admin/sessions/:key/replay
    // The :key segment is URL-encoded by the client (colons, slashes), so
    // decode it before lookup. Body is optional; defaults to mode=transcript.
    const replayMatch = path.match(/^\/admin\/sessions\/([^/]+)\/replay$/);
    if (replayMatch) {
      const sessionKey = decodeURIComponent(replayMatch[1]!);
      let body: { mode?: string };
      try {
        body = (await readJsonBody(req)) as typeof body;
      } catch {
        json(res, 400, { error: "invalid_json" });
        return;
      }
      const requestedMode = body?.mode === "rerun" ? "rerun" : "transcript";
      const replay = makeMockReplay(sessionKey, requestedMode);
      if (!replay) {
        json(res, 404, { error: "not_found", session_key: sessionKey });
        return;
      }
      json(res, 200, replay);
      return;
    }

    json(res, 404, { error: "not_found", path });
    return;
  }

  json(res, 405, { error: "method_not_allowed", method });
});

server.listen(PORT, HOST, () => {
  // eslint-disable-next-line no-console
  console.log(
    `[corlinman-mock] listening on http://${HOST}:${PORT} (plugins/agents/logs-stream/healthz)`,
  );
});

// Graceful shutdown so `concurrently` / nodemon don't leak handles.
const shutdown = (signal: string): void => {
  // eslint-disable-next-line no-console
  console.log(`[corlinman-mock] received ${signal}, closing`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 2000).unref();
};
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
