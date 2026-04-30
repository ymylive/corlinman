# Phase 4 W2 B3 — Per-tenant evolution federation (opt-in)

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-04-30 · **Estimate**: 5-7d

> Tenant A's lessons benefit tenant B without auto-propagating. An
> operator who approves a `skill_update` on tenant A can flag it
> `share_with_tenants = ["bravo"]`. The gateway then writes a fresh
> `pending` proposal into each opted-in peer's `evolution.sqlite`
> with provenance metadata. The peer operator approves it as if
> local — re-confirming intent. No auto-apply, no network, no loops.

Code lands in:

1. **`corlinman-tenant`** — schema bump for `tenant_federation_peers`.
2. **`corlinman-evolution`** — `metadata_json` column + types.
3. **`corlinman-gateway`** — `FederationRebroadcaster`, hooked into `EvolutionApplier::apply` success path.
4. **Admin REST + UI** — share-flag in approve dialog; federation peer config page.

## Why this exists

Phase 4 W1 made tenants strict isolation boundaries: per-tenant
`evolution.sqlite`, per-tenant skills tree, no cross-tenant reads.
Correct for data (PII, traits, sessions) but too strong for
*learnings*. When the operator approves a `skill_update` that fixes
a real `web_search` bug on tenant A, the same patch helps every
peer — but auto-pushing violates isolation. B3 splits the
difference: same-binary, in-process rebroadcast as a fresh
proposal; the recipient re-approves with full diff visibility.

`skill_update` is the *only* federated kind in B3. `prompt_template`
/ `tool_policy` / `agent_card` reference tenant-shaped resources
(prompt segments, tool policy, persona); `memory_op` /
`tag_rebalance` reference per-tenant chunk ids. `skill_update`
appends lines to `skills/<name>.md` whose path is tenant-agnostic
— the natural unit of cross-tenant lesson.

## Schema

### Federation peer table — `tenants.sqlite` (admin DB)

Peer list lives alongside the tenant roster so it's read once at
boot. One row per directional peering. **Asymmetric**: A → B does
not imply B → A.

```sql
CREATE TABLE IF NOT EXISTS tenant_federation_peers (
    source_tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    peer_tenant_id   TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    -- v1: only ["skill_update"] honoured. Forward-shaped so future
    -- kinds opt in without migration. Empty array = disabled.
    allowed_kinds    TEXT NOT NULL DEFAULT '["skill_update"]',
    created_at       INTEGER NOT NULL,
    created_by       TEXT NOT NULL,
    PRIMARY KEY (source_tenant_id, peer_tenant_id),
    CHECK (source_tenant_id != peer_tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_tfp_source
    ON tenant_federation_peers(source_tenant_id);
```

Reading: "tenant `peer_tenant_id` accepts federated proposals
**from** `source_tenant_id`." The recipient owns the row.

### `metadata_json` column on `evolution_proposals`

Append-only migration in `corlinman-evolution::schema::MIGRATIONS`:

```sql
("evolution_proposals", "metadata_json",
 "ALTER TABLE evolution_proposals ADD COLUMN metadata_json TEXT")
```

JSON shape:

```jsonc
{
  // Present only on rebroadcast rows; absent for native.
  "federated_from": {
    "source_tenant": "acme",
    "source_proposal_id": "evol-2026-04-30-007",
    "hop": 1
  },
  // Present on the source row when operator selected peers.
  "shared_with": ["bravo", "charlie"]
}
```

## Public types

```rust
// corlinman-tenant
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FederationPeerRow {
    pub source_tenant_id: TenantId,
    pub peer_tenant_id:   TenantId,
    pub allowed_kinds:    Vec<String>,
    pub created_at:       i64,
    pub created_by:       String,
}

// corlinman-evolution::types
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ProposalMetadata {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub federated_from: Option<FederationLink>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub shared_with: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FederationLink {
    pub source_tenant: String,
    pub source_proposal_id: String,
    pub hop: u8,
}
```

`EvolutionProposal` gains `pub metadata: ProposalMetadata`
(`Default` empty). `ProposalsRepo::insert/get` round-trips the
column; absent column on legacy rows decodes as `Default`.

## Rebroadcast contract — `FederationRebroadcaster`

Lives in `corlinman-gateway` (it consults `TenantPool` to write
into peer DBs). Hooked into `EvolutionApplier::apply` success path:
after the source row flips to `applied` and history is written,
the applier calls `rebroadcaster.broadcast(&proposal,
source_tenant)`.

```rust
pub struct FederationRebroadcaster {
    admin_db:    Arc<AdminDb>,
    tenant_pool: Arc<TenantPool>,
    config:      FederationConfig,
}

impl FederationRebroadcaster {
    /// No-op when: kind != skill_update, shared_with empty, federation
    /// disabled, or hop >= max_hop.
    pub async fn broadcast(
        &self,
        applied: &EvolutionProposal,
        source_tenant: &TenantId,
    ) -> Result<Vec<RebroadcastOutcome>, FederationError>;
}

pub enum RebroadcastOutcome {
    Sent { peer_tenant: TenantId, peer_proposal_id: ProposalId },
    PeerOptOut { peer_tenant: TenantId },
    PeerUnknown { peer_tenant: String },
    HopLimitReached { peer_tenant: TenantId, hop: u8 },
}
```

The peer row is fresh: new id (`evol-<date>-fed-<source-suffix>`),
`status = "pending"`, `tenant_id = peer`, `decided_at/by/applied_at
= null`, `kind = "skill_update"`, `target` and `diff` copied
verbatim, `risk` copied, `reasoning` prefixed with `[federated from
<source>] `, `metadata.federated_from = { source_tenant,
source_proposal_id, hop: source.hop + 1 }`. The source row is
unchanged. `metadata.shared_with` is set on the source at approve
time (see admin surface) and read here.

### What crosses tenant lines

Exactly: `target`, `diff`, `risk`, prefixed `reasoning`. Nothing
else. **Excluded**: `signal_ids` and `trace_ids` (point at source-
tenant rows the peer can't see; information leak about source
traffic), `shadow_metrics` (eval ran against source data),
`created_at` (re-stamped), `decided_by`, `eval_run_id`,
`baseline_metrics_json`. PII is bounded but real: `skill_update`
diffs are content the source operator already vetted. If they
contain PII, reject at source. **Federation is downstream of
operator approval — the trust root is "the source operator chose
to publish this".**

## Authentication & trust model

**No network surface.** Source and peer tenants live in the same
gateway process; rebroadcast is an in-memory call into
`TenantPool::pool_for(peer, "evolution")` followed by a sqlx
insert. Trust collapses to "the operator running this binary
trusts the binary to honour `tenant_federation_peers`". Cross-
deployment federation (different gateway processes) is Phase 5
material — needs signed envelopes; out of scope.

Peer-side authorisation is the `tenant_federation_peers` row: each
`shared_with` entry is checked against the peer's opt-in row before
writing. An entry the peer hasn't opted into is silently skipped
and counted in
`evolution_federation_skipped_total{reason="peer_opt_out"}`.

## Loop prevention

Rebroadcast forms a directed graph. With `peers(A)=[B]` and
`peers(B)=[A]` an unbounded chain would be A → B → A → … The
guard is **two-clause and cooperative**:

1. **Hop counter.** `metadata.federated_from.hop` increments by 1
   per rebroadcast. `FederationConfig::max_hop` (default **1**)
   bounds the chain. A's native (no `federated_from`, hop 0)
   rebroadcasts to B as hop 1. B's apply sees `hop == max_hop`
   and skips rebroadcast.
2. **Source-tenant exclusion.** When rebroadcasting an applied
   row whose `federated_from.source_tenant == X`, X is filtered
   out of `shared_with` even if peering exists. This handles
   `max_hop > 1` operator overrides without re-injecting back to
   origin.

Default `max_hop = 1` is the recommendation: federated proposals
**don't re-federate**. To re-share B's adaptations, B's operator
files a fresh native proposal. This matches "once approved by
recipient, it's the recipient's now" (see Withdrawal). The two-
clause design is belt-and-suspenders: hop alone suffices, but
source-exclusion makes the audit log readable when operators
experiment with `max_hop = 2`.

## Withdrawal independence

If A's source proposal is later auto-rolled-back, peer proposals
on B/C **do not** withdraw. By rollback time the peer either
already approved (real change in their tenant — withdrawing
violates operator intent) or hasn't (`pending` row stays in their
queue; operator can deny it once they notice). Rebroadcaster is
fire-and-forget; no reverse-channel notification. The peer admin
surface renders `metadata.federated_from.source_proposal_id` as a
link so operators can hand-check source state. UI states this:
"Federated proposals are independent once received. Source
rollback does not affect this proposal."

## Gateway integration

Single new wiring point in `EvolutionApplier::apply` (success
path, after history write):

```rust
self.proposals.mark_applied(&pid, now).await?;
self.history.insert(&history_row).await?;

if let Some(rb) = &self.rebroadcaster {
    if let Err(e) = rb.broadcast(&proposal, &source_tenant).await {
        tracing::warn!(error = %e, "federation rebroadcast failed; source apply unaffected");
    }
}
```

Rebroadcast failures are **non-fatal**. The source apply is
durable; rebroadcast failure is logged + counted in
`evolution_federation_broadcast_total{outcome="ok|skipped|error"}`.

## Admin surface

### Source — share-flag at approval

`POST /admin/evolution/:id/approve` body extended:

```jsonc
{
  "decided_by": "operator",
  "share_with_tenants": ["bravo", "charlie"]
}
```

Validation: each entry must exist in the active roster (else 422
`unknown_peer_tenant`); kind must be `skill_update` (else 422
`kind_not_federable`). Persisted into `metadata.shared_with`.

UI: existing approve dialog gains a multi-select populated from
`GET /admin/tenants?federation_peers_of=<source>` (joins
`tenant_federation_peers`). Empty list = no federation. Dialog
shows: "Each selected tenant's operator must approve a fresh
proposal — this does not auto-apply."

### Recipient — federated badge

Peer's `/admin/evolution` list renders a "Federated" badge on
rows where `metadata.federated_from.is_some()`. Detail page shows
source tenant + source proposal id (read-only).

### Federation peer admin

- `GET    /admin/tenants/:tenant/federation` — list peers (rows where `peer_tenant_id == :tenant`).
- `POST   /admin/tenants/:tenant/federation` — body `{ source, allowed_kinds }`. Operator must own `:tenant`.
- `DELETE /admin/tenants/:tenant/federation/:source` — revoke. Existing federated proposals already in queue are unaffected.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `peer_opt_in_round_trips` | tenant | Insert/list/delete; PK enforces `(source, peer)`; CHECK rejects self-peer |
| `metadata_json_round_trips` | evolution | `ProposalsRepo::insert/get` preserves `ProposalMetadata`; legacy decodes as `Default` |
| `share_flag_persists_at_approve` | gateway | Body with `share_with_tenants` → row's `metadata.shared_with` populated |
| `share_flag_rejects_unknown_peer` | gateway | Slug not in roster → 422 |
| `share_flag_only_for_skill_update` | gateway | `share_with_tenants` on `prompt_template` → 422 |
| `apply_triggers_rebroadcast` | applier | Source apply with `shared_with=["b"]` → exactly one new `pending` row in tenant b's DB |
| `rebroadcast_skips_unopted_peer` | applier | Peer not in `tenant_federation_peers` → no row; `peer_opt_out` outcome |
| `recipient_approval_applies_locally` | e2e | Peer approves federated row → applies to peer's `skills_dir`; peer history row written |
| `recipient_rejection_no_op_at_source` | e2e | Peer denies → source proposal unchanged (still `applied`) |
| `loop_prevention_hop_limit` | applier | Apply on `hop=1` row with `shared_with` → no rebroadcast (`hop_limit_reached`) |
| `loop_prevention_source_exclusion` | applier | `max_hop=2`, A→B→tries-A → A filtered |
| `withdrawal_independence` | e2e | Source auto-rollback → peer's federated row unchanged |
| `peer_db_failure_non_fatal` | applier | Peer DB locked → source apply still OK; warn logged |
| `federation_disabled_short_circuits` | applier | `enabled=false` → no-op even with `shared_with` set |

## Configuration

```toml
[evolution.federation]
enabled = true
max_hop = 1                       # 1 = federated proposals don't re-federate
allowed_kinds = ["skill_update"]  # forward-shaped; only honoured kind in v1
```

## Open questions

1. **Multi-select default in approve dialog.** Pre-fill all opted-
   in peers, or start empty? Lean: **empty** — pre-filling biases
   toward over-sharing.
2. **Peer proposal id format.** `evol-2026-04-30-fed-007` vs same
   shape as native. Lean: **fed marker** for grep-ability; metadata
   blob still authoritative.
3. **Bulk `share_with`.** `"*"` shorthand for all peers? Lean:
   defer — explicit list keeps audit log readable.

## Implementation order (suggested for autonomous iterations)

Each is one bounded iteration (~30 min - 2 hours):

1. **Schema bump in `corlinman-tenant`** — add
   `tenant_federation_peers` to `admin_schema.rs::SCHEMA_SQL`,
   `FederationPeerRow` + `AdminDb::{add_peer, list_peers_for,
   remove_peer}`. 4 tests: round-trip, FK cascade, self-peer CHECK,
   list ordering.
2. **`metadata_json` migration in `corlinman-evolution`** — append
   migration tuple, add `ProposalMetadata` + `FederationLink`,
   plumb through `ProposalsRepo::insert/get`. Tests: round-trip
   with metadata, legacy null → `Default`.
3. **`FederationConfig` in `corlinman-core::config`** — add
   `[evolution.federation]` section; defaults preserve disabled.
   2 tests: default round-trip, explicit block round-trip.
4. **`FederationRebroadcaster::broadcast` core** — kind gate,
   `shared_with` iteration, peer opt-in lookup, hop guard, source
   exclusion, peer DB write. 6 tests covering each
   `RebroadcastOutcome` + disabled-config short-circuit.
5. **Applier integration** — wire `rebroadcaster:
   Option<Arc<...>>` into `EvolutionApplier`, call at apply-success
   site. 2 tests: success triggers rebroadcast; broadcaster error
   logged but apply still succeeds.
6. **Approve route — `share_with_tenants` body field** — extend
   `ApproveBody`, validate roster + kind, persist into
   `metadata.shared_with`. 4 tests: happy, unknown peer 422, non-
   federable kind 422, empty list = no metadata write.
7. **Federation peer admin routes** —
   `GET/POST/DELETE /admin/tenants/:tenant/federation`. 6 tests:
   ACL (operator must own `:tenant`), idempotent delete, FK
   cascade.
8. **End-to-end integration test** — two tenants, opt-in, source
   approve+apply with `share_with`, peer queue shows pending,
   peer approve+apply runs against peer's `skills_dir`. 1 test;
   `TempDir` + `TenantPool`.
9. **Admin UI — share dialog + federated badge** — multi-select
   inside the approve dialog; "Federated" badge + source link on
   peer's evolution list. UI tests pin the contract.
10. **Federation peer admin UI page** —
    `/admin/tenants/:tenant/federation` table + add/remove dialog.
    Mock-server contract test.

## Out of scope (B3)

- **Cross-deployment federation** — different gateway processes
  exchanging proposals. Needs signed envelopes; Phase 5.
- **Auto-withdrawal on source rollback** — see Withdrawal
  independence. Operator-visible link is enough for B3.
- **Federation of non-`skill_update` kinds** — references per-
  tenant resources; needs peer-side rewrite step.
- **Bulk operations** — share-with-all shorthand, batch approve.
- **Federation observability dashboard** — counter ships in B3;
  dedicated UI page is a follow-up.
