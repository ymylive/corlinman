/**
 * Federation API client corpus (Phase 4 W2 B3 iter 6+).
 *
 * Covers the tagged result mapping for each route:
 *   - fetchFederationPeers happy path + 503 tenants_disabled
 *   - addFederationPeer happy path + 400 invalid_input
 *   - removeFederationPeer 200 + 404 not_found
 *   - fetchRecentFederatedProposals happy path + 404 → not_found
 *
 * The same `vi.stubGlobal('fetch', …)` discipline used by sessions.test.ts.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FEDERATION_PEERS_PATH,
  addFederationPeer,
  federationPeerPath,
  federationRecentProposalsPath,
  fetchFederationPeers,
  fetchRecentFederatedProposals,
  removeFederationPeer,
} from "./federation";

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

describe("URL builders", () => {
  it("anchors the list path at /admin/federation/peers", () => {
    expect(FEDERATION_PEERS_PATH).toBe("/admin/federation/peers");
  });

  it("encodes the source tenant id in the per-row paths", () => {
    expect(federationPeerPath("acme-corp")).toBe(
      "/admin/federation/peers/acme-corp",
    );
    expect(federationRecentProposalsPath("acme-corp")).toBe(
      "/admin/federation/peers/acme-corp/recent_proposals",
    );
  });
});

describe("fetchFederationPeers", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the two-collection envelope on 200", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, {
        accepted_from: [
          {
            peer_tenant_id: "acme",
            source_tenant_id: "bravo",
            accepted_at_ms: 1_777_000_000_000,
            accepted_by: "alice",
          },
        ],
        peers_of_us: [
          {
            peer_tenant_id: "charlie",
            source_tenant_id: "acme",
            accepted_at_ms: 1_777_111_111_111,
            accepted_by: "bob",
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchFederationPeers();
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.accepted_from).toHaveLength(1);
    expect(result.accepted_from[0]?.source_tenant_id).toBe("bravo");
    expect(result.peers_of_us).toHaveLength(1);
    expect(result.peers_of_us[0]?.peer_tenant_id).toBe("charlie");
    expect(calls[0]?.url).toContain("/admin/federation/peers");
    expect(calls[0]?.init.method ?? "GET").toBe("GET");
  });

  it("fetchFederationPeers_returns_disabled_on_503", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(503, { error: "tenants_disabled" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchFederationPeers();
    expect(result.kind).toBe("tenants_disabled");
  });

  it("tolerates a missing accepted_from / peers_of_us field on 200", async () => {
    const { fn } = makeFetchStub(() => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fn);

    const result = await fetchFederationPeers();
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.accepted_from).toEqual([]);
    expect(result.peers_of_us).toEqual([]);
  });

  it("rethrows other failures (e.g. 500)", async () => {
    const { fn } = makeFetchStub(() => jsonResponse(500, { error: "boom" }));
    vi.stubGlobal("fetch", fn);

    await expect(fetchFederationPeers()).rejects.toThrow();
  });
});

describe("addFederationPeer", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the body and surfaces the created row on 201", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(201, {
        peer_tenant_id: "acme",
        source_tenant_id: "bravo",
        accepted_at_ms: 1_777_000_000_000,
        accepted_by: "alice",
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await addFederationPeer("bravo");
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.peer.source_tenant_id).toBe("bravo");
    expect(calls[0]?.init.method).toBe("POST");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body.source_tenant_id).toBe("bravo");
  });

  it("returns invalid_input on 400 with the server message", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(400, {
        error: "invalid_tenant_slug",
        slug: "NOT a slug",
        reason: "must match [a-z][a-z0-9-]*",
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await addFederationPeer("NOT a slug");
    expect(result.kind).toBe("invalid_input");
    if (result.kind !== "invalid_input") {
      throw new Error("expected invalid_input");
    }
    expect(result.message.length).toBeGreaterThan(0);
  });

  it("returns tenants_disabled on 503", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(503, { error: "tenants_disabled" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await addFederationPeer("bravo");
    expect(result.kind).toBe("tenants_disabled");
  });
});

describe("removeFederationPeer", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns ok when the row was deleted", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { removed: true }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await removeFederationPeer("bravo");
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.removed).toBe(true);
    expect(calls[0]?.init.method).toBe("DELETE");
    expect(calls[0]?.url).toContain("/admin/federation/peers/bravo");
  });

  it("removeFederationPeer_returns_not_found_on_404", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, { error: "not_found", source_tenant_id: "bravo" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await removeFederationPeer("bravo");
    expect(result.kind).toBe("not_found");
  });

  it("returns tenants_disabled on 503", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(503, { error: "tenants_disabled" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await removeFederationPeer("bravo");
    expect(result.kind).toBe("tenants_disabled");
  });
});

describe("fetchRecentFederatedProposals", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the proposals array on 200", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, {
        proposals: [
          {
            id: "evol-from-acme-1",
            kind: "skill_update",
            status: "pending",
            created_at: 1_777_000_000_000,
            federated_from: {
              tenant: "acme",
              source_proposal_id: "evol-acme-2026-05-01-007",
              hop: 1,
            },
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchRecentFederatedProposals("acme");
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.proposals).toHaveLength(1);
    expect(result.proposals[0]?.federated_from.tenant).toBe("acme");
    expect(calls[0]?.url).toContain(
      "/admin/federation/peers/acme/recent_proposals",
    );
  });

  it("collapses 400 invalid_tenant_slug onto not_found", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(400, { error: "invalid_tenant_slug" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchRecentFederatedProposals("BAD SLUG");
    expect(result.kind).toBe("not_found");
  });
});
