//! corlinman multi-tenancy primitives (Phase 4 Wave 1 / 4-1A).
//!
//! Phase 3.1 already added a `tenant_id` column with default `'default'`
//! to `user_traits` and `agent_persona_state` and threaded a `--tenant-id`
//! flag through the Python CLIs. This crate is the Rust-side companion:
//!
//! * [`TenantId`] — slug-shaped newtype with validation, `serde`,
//!   `JsonSchema`, `Display`, `FromStr`. The shape is fixed at
//!   `^[a-z][a-z0-9-]{0,62}$` (per Phase 3.1's de-facto choice; see
//!   `docs/design/phase4-roadmap.md` §3 "Implicit decisions"). The
//!   reserved value `default` represents legacy single-tenant
//!   deployments and is the only `default()` for back-compat boots.
//! * [`tenant_db_path`] — derives the per-tenant SQLite path under
//!   `<root>/tenants/<tenant_id>/<name>.sqlite`. Single source of truth
//!   for every component that opens a per-tenant DB so we can grep for
//!   path leaks at audit time.
//! * [`TenantPool`] — multi-DB pool wrapper keyed by `(TenantId,
//!   db_name)`. Lazy-opens each per-tenant `SqlitePool` on first use,
//!   caches it, and hands out `&SqlitePool` references that downstream
//!   repos clone-and-bind against — preserving the Phase 2/3 connect
//!   pattern without rewriting every call site.
//!
//! The crate is intentionally thin: it does **not** know any of the
//! schemas it stores — Each downstream crate (`corlinman-evolution`,
//! `corlinman-vector`, `corlinman-core::session_sqlite`) keeps its own
//! `SCHEMA_SQL` + idempotent ALTER block. `TenantPool` only manages the
//! `(tenant, name) -> pool` map.

pub mod admin_schema;
mod id;
mod path;
mod pool;

pub use admin_schema::{AdminDb, AdminDbError, AdminRow, FederationPeer, TenantRow};
pub use id::{TenantId, TenantIdError, DEFAULT_TENANT_ID, TENANT_SLUG_REGEX_STR};
pub use path::{tenant_db_path, tenant_root_dir};
pub use pool::{TenantPool, TenantPoolError};
