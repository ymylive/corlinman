/**
 * Federation admin API client (Phase 4 W2 B3 iter 6+).
 *
 * Wraps the operator-only `/admin/federation/peers*` surface. Backed by
 * `corlinman-gateway::routes::admin::federation`.
 *
 * Contract — mirrors the Rust route module docs:
 *
 *   GET    /admin/federation/peers
 *     → 200 { accepted_from: FederationPeer[], peers_of_us: FederationPeer[] }
 *     → 503 { error: "tenants_disabled" }
 *
 *   POST   /admin/federation/peers
 *     body { source_tenant_id }
 *     → 201 { peer_tenant_id, source_tenant_id, accepted_at_ms, accepted_by }
 *     → 400 { error: "invalid_tenant_slug" | "invalid_input", reason? }
 *     → 503 { error: "tenants_disabled" }
 *
 *   DELETE /admin/federation/peers/:source_tenant_id
 *     → 200 { removed: true }
 *     → 404 { error: "not_found", source_tenant_id }
 *     → 503 { error: "tenants_disabled" }
 *
 *   GET    /admin/federation/peers/:source_tenant_id/recent_proposals
 *     → 200 { proposals: FederatedProposal[] }
 *     → 400 { error: "invalid_tenant_slug" }   (treated as not_found by UI)
 *     → 503 { error: "tenants_disabled" }
 *
 * Tagged result types so consumers can branch on 503 / 404 without
 * pattern-matching `CorlinmanApiError.message`. The 503 envelope keys
 * the disabled-state banner the page renders in lieu of an error toast.
 */

import { CorlinmanApiError, apiFetch } from "@/lib/api";

/* ------------------------------------------------------------------ */
/*                           Public types                             */
/* ------------------------------------------------------------------ */

/** One row in the federation peer table — both `accepted_from` and
 *  `peers_of_us` use this shape (the meaning of `peer`/`source` flips
 *  between the two collections). Mirrors Rust `FederationPeerOut`. */
export interface FederationPeer {
  /** The recipient tenant — accepts proposals from `source_tenant_id`. */
  peer_tenant_id: string;
  /** The publishing tenant — proposals originate here. */
  source_tenant_id: string;
  /** Unix milliseconds the opt-in was recorded on the peer side. */
  accepted_at_ms: number;
  /** Operator username that accepted; null for system-seeded rows. */
  accepted_by: string | null;
}

/** New row returned by POST /admin/federation/peers. `accepted_by` is
 *  always populated (Basic-auth-derived or the "admin" fallback). */
export interface FederationPeerCreated {
  peer_tenant_id: string;
  source_tenant_id: string;
  accepted_at_ms: number;
  accepted_by: string;
}

/** `metadata.federated_from` block — provenance of a federated proposal. */
export interface FederatedFrom {
  tenant: string;
  source_proposal_id: string;
  hop: number;
}

/** One row in `GET /admin/federation/peers/:source/recent_proposals`. */
export interface FederatedProposal {
  id: string;
  kind: string;
  status: string;
  /** Unix milliseconds. */
  created_at: number;
  federated_from: FederatedFrom;
}

/* ------------------------------------------------------------------ */
/*                       Tagged result types                          */
/* ------------------------------------------------------------------ */

export type FederationListResult =
  | {
      kind: "ok";
      accepted_from: FederationPeer[];
      peers_of_us: FederationPeer[];
    }
  | { kind: "tenants_disabled" };

export type AddFederationResult =
  | { kind: "ok"; peer: FederationPeerCreated }
  | { kind: "invalid_input"; message: string }
  | { kind: "tenants_disabled" };

export type RemoveFederationResult =
  | { kind: "ok"; removed: boolean }
  | { kind: "not_found" }
  | { kind: "tenants_disabled" };

export type RecentProposalsResult =
  | { kind: "ok"; proposals: FederatedProposal[] }
  | { kind: "not_found" }
  | { kind: "tenants_disabled" };

/* ------------------------------------------------------------------ */
/*                          URL builders                              */
/* ------------------------------------------------------------------ */

export const FEDERATION_PEERS_PATH = "/admin/federation/peers";

export function federationPeerPath(sourceTenantId: string): string {
  return `/admin/federation/peers/${encodeURIComponent(sourceTenantId)}`;
}

export function federationRecentProposalsPath(sourceTenantId: string): string {
  return `/admin/federation/peers/${encodeURIComponent(sourceTenantId)}/recent_proposals`;
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

function is400(err: unknown): boolean {
  return err instanceof CorlinmanApiError && err.status === 400;
}

/* ------------------------------------------------------------------ */
/*                            Public fetches                          */
/* ------------------------------------------------------------------ */

export async function fetchFederationPeers(): Promise<FederationListResult> {
  try {
    const res = await apiFetch<{
      accepted_from?: FederationPeer[];
      peers_of_us?: FederationPeer[];
    }>(FEDERATION_PEERS_PATH);
    return {
      kind: "ok",
      accepted_from: res.accepted_from ?? [],
      peers_of_us: res.peers_of_us ?? [],
    };
  } catch (err) {
    if (is503(err)) return { kind: "tenants_disabled" };
    throw err;
  }
}

export async function addFederationPeer(
  sourceTenantId: string,
): Promise<AddFederationResult> {
  try {
    const peer = await apiFetch<FederationPeerCreated>(
      FEDERATION_PEERS_PATH,
      {
        method: "POST",
        body: { source_tenant_id: sourceTenantId },
      },
    );
    return { kind: "ok", peer };
  } catch (err) {
    if (is503(err)) return { kind: "tenants_disabled" };
    if (is400(err)) {
      return {
        kind: "invalid_input",
        message:
          err instanceof CorlinmanApiError ? err.message : "invalid input",
      };
    }
    throw err;
  }
}

export async function removeFederationPeer(
  sourceTenantId: string,
): Promise<RemoveFederationResult> {
  try {
    const res = await apiFetch<{ removed: boolean }>(
      federationPeerPath(sourceTenantId),
      { method: "DELETE" },
    );
    return { kind: "ok", removed: Boolean(res.removed) };
  } catch (err) {
    if (is503(err)) return { kind: "tenants_disabled" };
    if (is404(err)) return { kind: "not_found" };
    throw err;
  }
}

export async function fetchRecentFederatedProposals(
  sourceTenantId: string,
): Promise<RecentProposalsResult> {
  try {
    const res = await apiFetch<{ proposals?: FederatedProposal[] }>(
      federationRecentProposalsPath(sourceTenantId),
    );
    return { kind: "ok", proposals: res.proposals ?? [] };
  } catch (err) {
    if (is503(err)) return { kind: "tenants_disabled" };
    // Both 400 (invalid_tenant_slug) and 404 collapse to "not_found"
    // for the dialog — neither is a happy path the operator can recover
    // from inside this dialog.
    if (is404(err) || is400(err)) return { kind: "not_found" };
    throw err;
  }
}
