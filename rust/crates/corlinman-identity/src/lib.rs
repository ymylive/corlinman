//! `corlinman-identity` — Phase 4 Wave 2 B2: cross-channel
//! `UserIdentityResolver`.
//!
//! Resolves channel-scoped IDs (`qq:1234`, `telegram:9876`,
//! `ios:device-uuid`) to a canonical opaque [`UserId`]. Two humans on
//! different channels stay distinct until they prove they're the same
//! person via the verification-phrase protocol; only then does the
//! resolver unify their aliases under one [`UserId`].
//!
//! Tenant-scoped: each tenant has its own
//! `<data_dir>/tenants/<slug>/user_identity.sqlite`. One tenant's
//! identity graph never spills into another's — that boundary is the
//! same one Phase 4 Wave 1 enforced for sessions and KB.
//!
//! ## Modules
//!
//! - [`types`] — [`UserId`] / [`ChannelAlias`] / [`BindingKind`] /
//!   [`VerificationPhrase`].
//! - [`store`] — schema constants + (next iteration) `IdentityStore`
//!   trait + `SqliteIdentityStore` impl.
//! - [`error`] — [`IdentityError`].
//!
//! ## Status
//!
//! v1 ships only the type vocabulary + schema constants. The store
//! impl, resolver trait, and verification-phrase protocol land in
//! follow-up iterations per the design at
//! `docs/design/phase4-w2-b2-design.md` §"Implementation order".

pub mod error;
pub mod resolver;
pub mod store;
pub mod types;
pub mod verification;

pub use error::IdentityError;
pub use resolver::{IdentityStore, UserSummary};
pub use store::{identity_db_path, SqliteIdentityStore, SCHEMA_SQL};
pub use types::{BindingKind, ChannelAlias, UserId, VerificationPhrase};
pub use verification::DEFAULT_TTL_MIN;
