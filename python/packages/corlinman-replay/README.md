# corlinman-replay

Python port of the Rust crate `corlinman-replay`. Loads a session by key
from a per-tenant `sessions.sqlite` and reconstructs a deterministic,
structured transcript that downstream callers (the `corlinman replay`
CLI; the `/admin/sessions/:key/replay` HTTP route) format for human or
JSON consumption.

## Modes

- **Transcript** (default): read-only deterministic dump of the stored
  session messages, ordered by `seq` ASC. No agent execution.
  Idempotent: same `(sessions.sqlite, session_key)` always yields the
  same transcript.
- **Rerun** (Wave 2.5+, stub in v1): ships the wire shape with a
  `not_implemented_yet` marker so the UI can render the deferral.

## Tenant scoping

Callers pass a `TenantId` slug; the replay primitive opens
`<data_dir>/tenants/<tenant>/sessions.sqlite`. Single-tenant deployments
pass `TenantId.legacy_default()` and read from the reserved-default
path. The tenant slug type is local to this package (Protocol-friendly
`TenantId` newtype) so it stays decoupled from any sibling package's
own tenant definition.
