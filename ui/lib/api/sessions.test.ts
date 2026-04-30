/**
 * Sessions API client corpus (Phase 4 W2 4-2D).
 *
 * Covers:
 *   - URL builder encodes session keys correctly
 *   - List `200` happy path
 *   - List `503 sessions_disabled` → `{ kind: "disabled" }`
 *   - Replay default-mode body shape
 *   - Replay `200` happy path returns tagged `{ kind: "ok", replay }`
 *   - Replay `404 not_found` → `{ kind: "not_found", session_key }`
 *   - Replay `503 sessions_disabled` → `{ kind: "disabled" }`
 *   - Replay rerun mode includes the `rerun_diff` sentinel
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  RERUN_NOT_IMPLEMENTED,
  SESSIONS_LIST_PATH,
  fetchSessions,
  replaySession,
  sessionsReplayPath,
} from "./sessions";

type FetchInit = RequestInit & { method?: string; body?: BodyInit | null };

interface RecordedCall {
  url: string;
  init: FetchInit;
}

function makeFetchStub(
  responder: (init: FetchInit) => Response | Promise<Response>,
): { fn: ReturnType<typeof vi.fn>; calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const safeInit = (init ?? {}) as FetchInit;
    calls.push({ url, init: safeInit });
    return responder(safeInit);
  });
  return { fn, calls };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("sessionsReplayPath URL builder", () => {
  it("encodes the session key so colons survive the round-trip", () => {
    expect(sessionsReplayPath("qq:1234")).toBe(
      "/admin/sessions/qq%3A1234/replay",
    );
  });

  it("encodes group-style keys with multiple colons + punctuation", () => {
    expect(sessionsReplayPath("qq:group:123/abc")).toBe(
      "/admin/sessions/qq%3Agroup%3A123%2Fabc/replay",
    );
  });

  it("anchors the list path at /admin/sessions", () => {
    expect(SESSIONS_LIST_PATH).toBe("/admin/sessions");
  });
});

describe("fetchSessions", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the session array on 200", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, {
        sessions: [
          { session_key: "qq:1234", last_message_at: 1, message_count: 7 },
        ],
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchSessions();
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.sessions).toHaveLength(1);
    expect(result.sessions[0]?.session_key).toBe("qq:1234");
    expect(calls[0]?.url).toContain("/admin/sessions");
    expect(calls[0]?.init.method ?? "GET").toBe("GET");
  });

  it("returns the disabled tag on 503 sessions_disabled", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(503, { error: "sessions_disabled" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchSessions();
    expect(result.kind).toBe("disabled");
  });

  it("rethrows other failures (e.g. 500)", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(500, { error: "boom" }),
    );
    vi.stubGlobal("fetch", fn);

    await expect(fetchSessions()).rejects.toThrow();
  });

  it("tolerates a missing `sessions` field on 200", async () => {
    const { fn } = makeFetchStub(() => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fn);

    const result = await fetchSessions();
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.sessions).toEqual([]);
  });
});

describe("replaySession", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults to mode=transcript when none is supplied", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, {
        session_key: "qq:1234",
        mode: "transcript",
        transcript: [],
        summary: { message_count: 0, tenant_id: "default" },
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await replaySession("qq:1234");
    expect(result.kind).toBe("ok");
    expect(calls[0]?.init.method).toBe("POST");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body.mode).toBe("transcript");
    expect(calls[0]?.url).toContain("/admin/sessions/qq%3A1234/replay");
  });

  it("returns the replay payload on 200", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(200, {
        session_key: "qq:1234",
        mode: "transcript",
        transcript: [
          { role: "user", content: "hi", ts: "2026-04-30T01:02:03Z" },
          { role: "assistant", content: "hello", ts: "2026-04-30T01:02:04Z" },
        ],
        summary: { message_count: 2, tenant_id: "default" },
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await replaySession("qq:1234");
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.replay.transcript).toHaveLength(2);
    expect(result.replay.transcript[0]?.role).toBe("user");
    expect(result.replay.summary.tenant_id).toBe("default");
  });

  it("returns the not_found tag on 404", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, { error: "not_found", session_key: "missing" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await replaySession("missing");
    expect(result.kind).toBe("not_found");
    if (result.kind !== "not_found") throw new Error("expected not_found");
    expect(result.session_key).toBe("missing");
  });

  it("returns the disabled tag on 503 sessions_disabled", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(503, { error: "sessions_disabled" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await replaySession("qq:1234");
    expect(result.kind).toBe("disabled");
  });

  it("forwards rerun mode in the request body and surfaces the rerun_diff sentinel", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, {
        session_key: "qq:1234",
        mode: "rerun",
        transcript: [],
        summary: {
          message_count: 0,
          tenant_id: "default",
          rerun_diff: RERUN_NOT_IMPLEMENTED,
        },
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await replaySession("qq:1234", { mode: "rerun" });
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body.mode).toBe("rerun");
    expect(result.replay.summary.rerun_diff).toBe(RERUN_NOT_IMPLEMENTED);
  });
});
