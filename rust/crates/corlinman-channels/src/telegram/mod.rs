//! Telegram Bot channel adapter (S4 T4).
//!
//! Two submodules:
//! - [`message`]: Bot API wire types (Update / Message / User / Chat /
//!   MessageEntity) — narrow, serde-only.
//! - [`service`]: HTTPS long-poll client (`getUpdates`) + outbound
//!   (`sendMessage`) plus [`run_telegram_channel`], the entry the gateway
//!   spawns.
//!
//! # Why bare HTTPS instead of teloxide
//!
//! teloxide is a fine framework but it pulls a dispatcher / state-machine
//! stack we don't use — corlinman only needs `getUpdates` in and
//! `sendMessage` out. A 200-line `reqwest` client keeps the dep graph
//! predictable (no new workspace deps) and avoids compile-time bloat.
//! If we later need inline keyboards / webhook mode the plan is to
//! swap in teloxide behind the same `run_telegram_channel` signature.

pub mod message;
pub mod service;

pub use message::*;
pub use service::*;
