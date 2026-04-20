//! Telegram live smoke test (Sprint 4 T4).
//!
//! Gated behind `#[ignore]` so `cargo test` does **not** run it by default.
//! Invoke explicitly:
//!
//! ```bash
//! TELEGRAM_TEST_TOKEN=123:abc \
//!   cargo test -p corlinman-channels --test telegram_smoke -- --ignored
//! ```
//!
//! Behaviour:
//! - No `TELEGRAM_TEST_TOKEN` → skip (test returns early with `println!`).
//! - Token set → a single live `GET /bot<token>/getMe` against
//!   `api.telegram.org`. Asserts HTTP 200 + `ok: true`. No `sendMessage`
//!   traffic is generated so running this doesn't DM anyone.
//!
//! The point is to prove the adapter can actually talk to Telegram, not to
//! exercise the dispatch loop end-to-end — teloxide-style mocks are out of
//! scope (`mod.rs` explains why we don't depend on teloxide).

use std::time::Duration;

/// Env var the operator sets to opt in to a live round-trip.
const TOKEN_ENV: &str = "TELEGRAM_TEST_TOKEN";

#[tokio::test]
#[ignore = "requires TELEGRAM_TEST_TOKEN; run with `-- --ignored`"]
async fn telegram_get_me_live() {
    let Ok(token) = std::env::var(TOKEN_ENV) else {
        println!("{TOKEN_ENV} not set; skipping telegram live smoke test");
        return;
    };
    assert!(!token.trim().is_empty(), "{TOKEN_ENV} is present but empty");

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .expect("build reqwest client");

    let url = format!("https://api.telegram.org/bot{token}/getMe");
    let resp = client.get(&url).send().await.expect("getMe request");
    assert!(
        resp.status().is_success(),
        "getMe failed: HTTP {}",
        resp.status()
    );

    // Don't deserialise into `telegram::message::User` — that's an
    // implementation detail the public crate API doesn't expose. The envelope
    // shape (`{ok, result: {id, ...}}`) is stable and sufficient.
    let body: serde_json::Value = resp.json().await.expect("getMe json");
    assert_eq!(body["ok"], true, "getMe returned ok != true: {body}");
    let id = body["result"]["id"].as_i64().expect("result.id i64");
    assert!(id > 0, "bot id should be positive; got {id}");
}
