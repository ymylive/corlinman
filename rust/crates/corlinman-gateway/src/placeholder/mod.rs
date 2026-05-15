//! Gateway-side placeholder resolvers (`{{namespace.key}}`).
//!
//! `corlinman-core` ships the [`PlaceholderEngine`] machinery (regex
//! match → namespace dispatch → resolver call → recursive expand);
//! this module collects gateway-owned implementations of the
//! `DynamicResolver` trait that depend on gateway-owned state
//! (per-tenant SQLite stores, in particular).
//!
//! ## Why a gateway-side module
//!
//! Resolvers like `{{vector.…}}` and `{{episodes.…}}` need a concrete
//! per-tenant SQLite handle, and the gateway's `AppState` is where
//! those handles already live (admin-routes pool, evolution pool, …).
//! Putting the resolvers next to the state keeps the wiring local and
//! avoids leaking a gateway dep up into `corlinman-core`.
//!
//! ## What ships in iter 7
//!
//! Phase 4 W4 D1 iter 7 lands the [`episodes`] resolver — a
//! read-side adapter over `episodes.sqlite` that surfaces:
//!
//! - `{{episodes.last_24h}}` / `last_week` / `last_month` — top-N by
//!   `importance_score` over a rolling window.
//! - `{{episodes.recent}}` — last N by `ended_at` regardless of score.
//! - `{{episodes.kind(<kind>)}}` — filter by [`EpisodeKind`].
//! - `{{episodes.about_id(<id>)}}` — single-episode lookup for
//!   citation in agent answers.
//!
//! `{{episodes.about(<tag>)}}` (tag-filter via `corlinman-tagmemo`) is
//! a follow-up — see the design's §"Query surface" — and currently
//! returns the literal token for a forward-compatible round-trip.

pub mod episodes;
pub mod memory;

pub use episodes::EpisodesResolver;
pub use memory::{MemoryResolver, DEFAULT_MEMORY_NAMESPACE, DEFAULT_TOP_K};
