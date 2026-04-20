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
  genTraceId,
} from "./seed";

const PORT = Number(process.env.MOCK_PORT ?? 7777);
const HOST = process.env.MOCK_HOST ?? "127.0.0.1";

// Default to permissive CORS so Next.js dev server on :3000 can hit us.
const CORS_HEADERS: Record<string, string> = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,OPTIONS",
  "access-control-allow-headers": "content-type,x-request-id",
  "access-control-expose-headers": "x-request-id",
};

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

const server = createServer((req, res) => {
  const method = req.method ?? "GET";
  const url = req.url ?? "/";

  if (method === "OPTIONS") {
    res.writeHead(204, CORS_HEADERS);
    res.end();
    return;
  }

  if (method !== "GET") {
    json(res, 405, { error: "method_not_allowed", method });
    return;
  }

  // Strip query string before matching; none of our mock routes care about params.
  const path = url.split("?")[0];

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
    default:
      notFound(res, path);
      return;
  }
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
