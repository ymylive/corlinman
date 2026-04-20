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
  genTraceId,
  type MockApproval,
} from "./seed";

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
      default:
        notFound(res, path);
        return;
    }
  }

  if (method === "POST") {
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
