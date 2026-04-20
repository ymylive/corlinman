//! QQ / OneBot v11 channel adapter.
//!
//! Two submodules:
//! - [`message`]: OneBot v11 event + action wire types (serde).
//! - [`onebot`]: forward-WebSocket client with reconnect / heartbeat.
//!
//! The `qqBot.js` reference implementation remains the 1.1 fallback (plan §14 R3).

pub mod message;
pub mod onebot;
