# newapi Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hard-remove `ProviderKind::Sub2api` and replace with `ProviderKind::Newapi` (QuantumNous/new-api). Add a 4-step onboard wizard, a `/admin/newapi` connector page, and a `corlinman config migrate-sub2api` CLI for old configs. Ship full i18n + migration docs + CHANGELOG BREAKING entry.

**Architecture:** newapi runs as a sidecar HTTP service exposing the OpenAI wire (chat/embedding/audio TTS). corlinman gateway hits it via the existing `OpenAICompatibleProvider` Python adapter for runtime, and via a new `corlinman-newapi-client` Rust crate for admin API (channel discovery, health). Onboard becomes a 4-step state-machine flow; admin `/newapi` page lets operators reconfigure post-onboard.

**Tech Stack:** Rust (`tokio`, `axum`, `reqwest`, `wiremock`, `serde`, `clap`), Python (`pytest`, `httpx`), Next.js 15 + React 19 (`@tanstack/react-query`, `react-i18next`, `vitest`/`jest`, `react-testing-library`).

**Spec:** `docs/superpowers/specs/2026-05-13-newapi-integration-design.md`

**Branch:** `feat/newapi-integration` (cut from `main`)

---

## File Structure (Locked-In Decomposition)

| Path | Action | Responsibility |
|---|---|---|
| `rust/crates/corlinman-core/src/config.rs` | modify | Rename `ProviderKind::Sub2api` → `Newapi`; `base_url_required` list; round-trip tests |
| `rust/crates/corlinman-gateway/src/routes/admin/providers.rs` | modify | 4 Sub2api refs → Newapi; existing tests migrated |
| `rust/crates/corlinman-newapi-client/Cargo.toml` | **create** | Standalone crate manifest |
| `rust/crates/corlinman-newapi-client/src/lib.rs` | **create** | Re-export module surface |
| `rust/crates/corlinman-newapi-client/src/client.rs` | **create** | HTTP client + `list_channels`, `get_user_self`, `probe`, `test_round_trip` |
| `rust/crates/corlinman-newapi-client/src/types.rs` | **create** | `Channel`, `ChannelType`, `User`, `ProbeResult`, `TestResult` |
| `rust/crates/corlinman-newapi-client/tests/client_test.rs` | **create** | wiremock-driven unit tests |
| `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs` | **create** | 5 routes: GET, POST probe, GET channels, POST test, PATCH |
| `rust/crates/corlinman-gateway/src/routes/admin/mod.rs` | modify | Register newapi router |
| `rust/crates/corlinman-gateway/src/routes/admin/auth.rs` | modify | Refactor onboard into 4-step state machine; keep old `/admin/onboard` returning 410 |
| `rust/crates/corlinman-gateway/src/state.rs` (or onboard sub-module) | modify/create | Ephemeral onboard-session store (in-memory `Arc<DashMap>` keyed by cookie) |
| `rust/crates/corlinman-gateway/tests/admin_newapi.rs` | **create** | Integration tests for `/admin/newapi/*` |
| `rust/crates/corlinman-gateway/tests/admin_onboard.rs` | **create** | Integration tests for new 4-step flow |
| `rust/crates/corlinman-cli/src/cmd/config.rs` | modify | Add `migrate-sub2api` subcommand variant |
| `rust/crates/corlinman-cli/src/cmd/migrate.rs` | **create** | `migrate-sub2api` impl (dry-run + apply) |
| `rust/crates/corlinman-cli/tests/migrate_test.rs` | **create** | CLI integration tests using `assert_cmd` |
| `python/packages/corlinman-providers/src/corlinman_providers/specs.py` | modify | `SUB2API` → `NEWAPI` |
| `python/packages/corlinman-providers/src/corlinman_providers/registry.py` | modify | dispatch table rename |
| `python/packages/corlinman-providers/tests/test_newapi.py` | **create** | dispatch + TTS audio mock |
| `python/packages/corlinman-providers/tests/test_sub2api.py` | **delete** | replaced by test_newapi.py |
| `ui/app/onboard/page.tsx` | modify | 4-step wizard state machine |
| `ui/app/onboard/page.test.tsx` | modify | 4-step flow tests |
| `ui/app/(admin)/newapi/page.tsx` | **create** | Connection card + channels table + test button |
| `ui/app/(admin)/newapi/page.test.tsx` | **create** | UI tests |
| `ui/app/(admin)/embedding/page.tsx` | modify | "Pull from newapi" button |
| `ui/app/(admin)/models/page.tsx` | modify | "Pull from newapi" button |
| `ui/lib/api.ts` | modify | `fetchNewapi`, `fetchNewapiChannels`, `probeNewapi`, `testNewapi`, `patchNewapi` |
| `ui/components/layout/nav.tsx` and/or `sidebar.tsx` | modify | Add `/newapi` nav entry |
| `ui/lib/i18n/locales/zh-CN/*.json` | modify | Add `onboard.newapi.*` + `admin.newapi.*` namespaces |
| `ui/lib/i18n/locales/en/*.json` | modify | Same |
| `docs/design/sub2api-integration.md` | **delete** | Superseded |
| `docs/design/newapi-integration.md` | **create** | Concise public design doc |
| `docs/providers.md` | modify | sub2api row → newapi row |
| `docs/migration/sub2api-to-newapi.md` | **create** | 5-step migration guide |
| `CREDITS.md` | modify | Drop Wei-Shaw/sub2api; add QuantumNous/new-api |
| `CHANGELOG.md` | modify | `## [Unreleased]` BREAKING entry |
| `scripts/e2e/newapi-flow.sh` | **create** | E2E: docker-compose newapi + corlinman → chat/embed/tts |
| `docker/compose/newapi.yml` | **create** | Optional newapi sidecar compose snippet |
| `Cargo.toml` (workspace) | modify | Add `corlinman-newapi-client` to members |

---

## Task Roadmap (25 tasks across 8 phases)

| Phase | Tasks | Focus |
|---|---|---|
| 1: Foundation | 1–3 | Branch, CHANGELOG stub, rename enum |
| 2: newapi-client crate | 4–7 | Standalone Rust HTTP client, wiremock TDD |
| 3: /admin/newapi routes | 8–12 | 5 admin endpoints with integration tests |
| 4: Onboard refactor | 13–17 | 4-step state machine + tests |
| 5: migrate-sub2api CLI | 18 | Single-task subcommand with TDD |
| 6: Onboard UI wizard | 19–21 | 4-step UI with i18n |
| 7: /admin/newapi UI + integrations | 22–24 | Admin page + embedding/models buttons |
| 8: Docs / CHANGELOG / E2E | 25 | Migration guide, CREDITS, providers.md, e2e script |

---

## Phase 1 — Foundation

### Task 1: Cut feature branch + CHANGELOG stub

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Cut feature branch**

```bash
git checkout -b feat/newapi-integration
git push -u origin feat/newapi-integration
```

- [ ] **Step 2: Add BREAKING stub to CHANGELOG**

Open `CHANGELOG.md`, insert after the `## [Unreleased]` heading (or create one if missing) at the top of the changelog:

```markdown
## [Unreleased]

### Removed (BREAKING)

- **`ProviderKind::Sub2api` removed.** The `kind = "sub2api"` provider entry
  is no longer recognised. Replace with `kind = "newapi"` pointing at a
  [QuantumNous/new-api](https://github.com/QuantumNous/new-api) instance.
  Run `corlinman config migrate-sub2api --apply` to rewrite legacy entries
  automatically. See `docs/migration/sub2api-to-newapi.md`.

### Added

- `ProviderKind::Newapi` + new-api admin client crate (`corlinman-newapi-client`).
- 4-step interactive onboard wizard (account → newapi → defaults → confirm).
- `/admin/newapi` connector page with live channel health & round-trip test.
- `corlinman config migrate-sub2api [--dry-run|--apply]` CLI subcommand.
- Full i18n coverage (zh-CN + en) for the new flows.
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): stub BREAKING entry for sub2api → newapi"
```

---

### Task 2: Rename `ProviderKind::Sub2api` → `Newapi` (Rust core)

**Files:**
- Modify: `rust/crates/corlinman-core/src/config.rs:441-493,3806`

- [ ] **Step 1: Search-replace the enum variant**

Find every occurrence in `rust/crates/corlinman-core/src/config.rs`:
- Line ~441-449 (variant declaration + docstring)
- Line ~470 (`Self::Sub2api => "sub2api"` in `as_str`)
- Line ~493 (`Self::Sub2api` in `all()` list)
- Line ~3806 (`(ProviderKind::Sub2api, "sub2api")` in tests)

Replace `Sub2api` with `Newapi` and `"sub2api"` with `"newapi"`. Update the docstring:

```rust
/// new-api (`https://github.com/QuantumNous/new-api`) — sidecar that pools
/// channels (LLM, embedding, audio TTS) behind a single OpenAI-wire
/// endpoint. Operators run new-api separately and point one corlinman
/// provider entry at it. Wire shape is pure OpenAI-compat, so chat /
/// embedding / TTS all dispatch via the shared adapter. The named kind
/// exists so the admin UI can surface new-api-specific health / channel
/// data, and so operators see "newapi" instead of an opaque
/// "openai_compatible" entry.
/// See `docs/design/newapi-integration.md` for the integration plan.
Newapi,
```

- [ ] **Step 2: Confirm the test list still compiles**

Run: `cargo build -p corlinman-core`
Expected: builds clean.

- [ ] **Step 3: Run config tests**

Run: `cargo test -p corlinman-core --lib config::`
Expected: all pass. If any test was specifically asserting `"sub2api"` string, update to `"newapi"` to match the rename.

- [ ] **Step 4: Commit**

```bash
git add rust/crates/corlinman-core/src/config.rs
git commit -m "refactor(core): rename ProviderKind::Sub2api → Newapi"
```

---

### Task 3: Migrate `admin/providers.rs` + Python registry to Newapi

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/providers.rs:208-212,344-352,552,1116-1208`
- Modify: `python/packages/corlinman-providers/src/corlinman_providers/specs.py:53-57`
- Modify: `python/packages/corlinman-providers/src/corlinman_providers/registry.py:71-73`
- Delete: `python/packages/corlinman-providers/tests/test_sub2api.py` (if exists)

- [ ] **Step 1: Rust admin/providers.rs replacement**

In `rust/crates/corlinman-gateway/src/routes/admin/providers.rs`:
- Lines 208-216: replace branch matching `ProviderKind::Sub2api` with `ProviderKind::Newapi`, message string `"sub2api"` → `"newapi"`
- Lines 344-352: same for the PATCH path
- Line 552: `ProviderKind::Newapi => openai_schema()` (still uses shared OpenAI schema)
- Lines 1116-1148: rename test fn `upsert_rejects_sub2api_without_base_url` → `upsert_rejects_newapi_without_base_url`; payload `"kind": "newapi"`; assertion expects message to contain `"newapi"`
- Lines 1154-1208: rename test fn `upsert_persists_sub2api_slot_and_renders_py_config` → `upsert_persists_newapi_slot_and_renders_py_config`; all "sub2api" → "newapi"

- [ ] **Step 2: Run gateway tests**

Run: `cargo test -p corlinman-gateway --lib routes::admin::providers`
Expected: all pass.

- [ ] **Step 3: Python specs/registry rename**

In `python/packages/corlinman-providers/src/corlinman_providers/specs.py:53-57`:
```python
# new-api (QuantumNous/new-api) sidecar — OpenAI-wire channel pooling
# manager. corlinman dispatches via the shared OpenAICompatibleProvider;
# the named kind exists so the admin UI / inspection commands can
# document operator intent. See ``docs/design/newapi-integration.md``.
NEWAPI = "newapi"
```

In `python/packages/corlinman-providers/src/corlinman_providers/registry.py:71-73`:
```python
# new-api speaks pure OpenAI wire format — same shared adapter as
# openai_compatible/together/groq. The dedicated kind is for the UI
# and lets the admin panel light up new-api-specific health columns.
ProviderKind.NEWAPI: OpenAICompatibleProvider,
```

- [ ] **Step 4: Delete sub2api Python test if it exists**

```bash
rm -f python/packages/corlinman-providers/tests/test_sub2api.py
```

- [ ] **Step 5: Run python tests**

Run: `cd python/packages/corlinman-providers && uv run pytest tests/ -x`
Expected: passes (sub2api test removed; newapi test comes in Task 18b — for now there's a gap that we close later).

- [ ] **Step 6: Commit**

```bash
git add rust/crates/corlinman-gateway/src/routes/admin/providers.rs \
        python/packages/corlinman-providers/src/corlinman_providers/specs.py \
        python/packages/corlinman-providers/src/corlinman_providers/registry.py
git rm -f python/packages/corlinman-providers/tests/test_sub2api.py 2>/dev/null || true
git commit -m "refactor: migrate Sub2api kind references to Newapi (gateway + py registry)"
```

---

## Phase 2 — corlinman-newapi-client crate

### Task 4: Scaffold the crate

**Files:**
- Create: `rust/crates/corlinman-newapi-client/Cargo.toml`
- Create: `rust/crates/corlinman-newapi-client/src/lib.rs`
- Create: `rust/crates/corlinman-newapi-client/src/types.rs`
- Create: `rust/crates/corlinman-newapi-client/src/client.rs`
- Modify: `Cargo.toml` (workspace root, `[workspace.members]`)

- [ ] **Step 1: Write `Cargo.toml`**

```toml
[package]
name = "corlinman-newapi-client"
version = "0.1.0"
edition = "2021"
description = "HTTP client for QuantumNous/new-api admin API (channel discovery, health)."
license = "Apache-2.0"

[dependencies]
reqwest = { workspace = true, features = ["json", "rustls-tls"] }
serde = { workspace = true, features = ["derive"] }
serde_json = { workspace = true }
thiserror = { workspace = true }
tokio = { workspace = true, features = ["macros"] }
tracing = { workspace = true }
url = { workspace = true }

[dev-dependencies]
wiremock = "0.6"
tokio = { workspace = true, features = ["macros", "rt-multi-thread"] }
```

If any of those workspace deps don't exist, fall back to explicit versions matching what's in `Cargo.toml` at the root.

- [ ] **Step 2: Write `lib.rs`**

```rust
//! HTTP client for QuantumNous/new-api admin API.
//!
//! Read-only operations: channel discovery, user/self introspection,
//! connection probe, 1-token round-trip test. corlinman uses this to
//! power the `/admin/newapi` page and the onboard wizard.

pub mod client;
pub mod types;

pub use client::{NewapiClient, NewapiError};
pub use types::{Channel, ChannelType, ProbeResult, TestResult, User};
```

- [ ] **Step 3: Write `types.rs`**

```rust
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ChannelType {
    Llm,
    Embedding,
    Tts,
}

impl ChannelType {
    /// Maps to the integer type code new-api expects in `/api/channel?type=`.
    pub fn as_int(self) -> u8 {
        match self {
            ChannelType::Llm => 1,
            ChannelType::Embedding => 2,
            ChannelType::Tts => 8,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Channel {
    pub id: u64,
    pub name: String,
    #[serde(rename = "type")]
    pub channel_type: i32,
    pub status: i32,
    pub models: String,
    pub group: String,
    pub priority: Option<i32>,
    pub used_quota: Option<i64>,
    pub remain_quota: Option<i64>,
    pub test_time: Option<i64>,
    pub response_time: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct User {
    pub id: u64,
    pub username: String,
    pub display_name: Option<String>,
    pub role: i32,
    pub status: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProbeResult {
    pub base_url: String,
    pub user: User,
    pub server_version: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TestResult {
    pub status: u16,
    pub latency_ms: u128,
    pub model: Option<String>,
}
```

- [ ] **Step 4: Stub `client.rs`**

```rust
use std::time::{Duration, Instant};

use reqwest::Client;
use serde::Deserialize;
use thiserror::Error;
use url::Url;

use crate::types::{Channel, ChannelType, ProbeResult, TestResult, User};

#[derive(Debug, Error)]
pub enum NewapiError {
    #[error("http request failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("invalid base url: {0}")]
    Url(#[from] url::ParseError),
    #[error("upstream returned status {status}: {body}")]
    Upstream { status: u16, body: String },
    #[error("upstream returned malformed json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("upstream is not new-api (missing /api/status or wrong shape)")]
    NotNewapi,
}

pub struct NewapiClient {
    base_url: Url,
    user_token: String,
    admin_token: Option<String>,
    http: Client,
}

impl NewapiClient {
    pub fn new(
        base_url: impl AsRef<str>,
        user_token: impl Into<String>,
        admin_token: Option<String>,
    ) -> Result<Self, NewapiError> {
        let http = Client::builder()
            .timeout(Duration::from_secs(8))
            .build()
            .map_err(NewapiError::Http)?;
        Ok(Self {
            base_url: Url::parse(base_url.as_ref())?,
            user_token: user_token.into(),
            admin_token,
            http,
        })
    }

    fn admin_or_user_token(&self) -> &str {
        self.admin_token.as_deref().unwrap_or(&self.user_token)
    }
}
```

- [ ] **Step 5: Add to workspace**

In root `Cargo.toml`, find `[workspace.members]` (or `members = [...]` block) and add the path:

```toml
"rust/crates/corlinman-newapi-client",
```

Keep entries alphabetically sorted with the rest if that's the convention.

- [ ] **Step 6: Build**

Run: `cargo build -p corlinman-newapi-client`
Expected: builds cleanly with warnings about unused `Channel`, `User`, etc. — that's fine, next tasks consume them.

- [ ] **Step 7: Commit**

```bash
git add rust/crates/corlinman-newapi-client/ Cargo.toml
git commit -m "feat(newapi-client): scaffold crate with types and empty client"
```

---

### Task 5: Implement `probe()` + `get_user_self()` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-newapi-client/src/client.rs`
- Create: `rust/crates/corlinman-newapi-client/tests/client_test.rs`

- [ ] **Step 1: Write the failing test**

In `tests/client_test.rs`:

```rust
use corlinman_newapi_client::{NewapiClient, NewapiError};
use serde_json::json;
use wiremock::{
    matchers::{header, method, path},
    Mock, MockServer, ResponseTemplate,
};

#[tokio::test]
async fn probe_returns_user_when_200() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/api/user/self"))
        .and(header("Authorization", "Bearer admin-tok"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": {
                "id": 1, "username": "root", "display_name": "Root",
                "role": 100, "status": 1
            }
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/api/status"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true, "data": { "version": "v0.4.0" }
        })))
        .mount(&server)
        .await;

    let client = NewapiClient::new(server.uri(), "user-tok", Some("admin-tok".into())).unwrap();
    let result = client.probe().await.unwrap();
    assert_eq!(result.user.username, "root");
    assert_eq!(result.server_version.as_deref(), Some("v0.4.0"));
}

#[tokio::test]
async fn probe_returns_unauthorized_on_401() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/user/self"))
        .respond_with(ResponseTemplate::new(401).set_body_string("unauthorized"))
        .mount(&server)
        .await;

    let client = NewapiClient::new(server.uri(), "bad", None).unwrap();
    let err = client.probe().await.unwrap_err();
    matches!(err, NewapiError::Upstream { status: 401, .. });
}

#[tokio::test]
async fn probe_returns_notnewapi_when_status_endpoint_missing() {
    let server = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/user/self"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": { "id": 1, "username": "x", "role": 1, "status": 1 }
        })))
        .mount(&server).await;
    Mock::given(method("GET")).and(path("/api/status"))
        .respond_with(ResponseTemplate::new(404))
        .mount(&server).await;

    let client = NewapiClient::new(server.uri(), "tok", None).unwrap();
    let err = client.probe().await.unwrap_err();
    matches!(err, NewapiError::NotNewapi);
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-newapi-client probe -- --nocapture`
Expected: compile error — `probe` method not defined.

- [ ] **Step 3: Implement `probe()`**

Append to `src/client.rs`:

```rust
#[derive(Debug, Deserialize)]
struct NewapiEnvelope<T> {
    #[allow(dead_code)]
    success: bool,
    data: T,
}

#[derive(Debug, Deserialize)]
struct StatusData {
    version: Option<String>,
}

impl NewapiClient {
    pub async fn probe(&self) -> Result<ProbeResult, NewapiError> {
        let user = self.get_user_self().await?;

        let status_url = self.base_url.join("/api/status")?;
        let r = self.http.get(status_url).send().await?;
        if !r.status().is_success() {
            return Err(NewapiError::NotNewapi);
        }
        let env: NewapiEnvelope<StatusData> = r.json().await?;
        Ok(ProbeResult {
            base_url: self.base_url.to_string(),
            user,
            server_version: env.data.version,
        })
    }

    pub async fn get_user_self(&self) -> Result<User, NewapiError> {
        let url = self.base_url.join("/api/user/self")?;
        let r = self.http.get(url)
            .bearer_auth(self.admin_or_user_token())
            .send().await?;
        let status = r.status();
        if !status.is_success() {
            let body = r.text().await.unwrap_or_default();
            return Err(NewapiError::Upstream { status: status.as_u16(), body });
        }
        let env: NewapiEnvelope<User> = r.json().await?;
        Ok(env.data)
    }
}
```

- [ ] **Step 4: Run test (verify it passes)**

Run: `cargo test -p corlinman-newapi-client probe`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-newapi-client/
git commit -m "feat(newapi-client): probe() + get_user_self() with wiremock tests"
```

---

### Task 6: Implement `list_channels()` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-newapi-client/src/client.rs`
- Modify: `rust/crates/corlinman-newapi-client/tests/client_test.rs`

- [ ] **Step 1: Write the failing test**

Append to `tests/client_test.rs`:

```rust
use corlinman_newapi_client::ChannelType;

#[tokio::test]
async fn list_channels_returns_filtered_by_type() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/channel/"))
        .and(wiremock::matchers::query_param("type", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": [
                { "id": 10, "name": "openai-primary", "type": 1, "status": 1,
                  "models": "gpt-4o,gpt-4o-mini", "group": "default" },
                { "id": 11, "name": "openai-fallback", "type": 1, "status": 2,
                  "models": "gpt-4o", "group": "default" }
            ]
        })))
        .mount(&server).await;

    let client = NewapiClient::new(server.uri(), "tok", None).unwrap();
    let channels = client.list_channels(ChannelType::Llm).await.unwrap();
    assert_eq!(channels.len(), 2);
    assert_eq!(channels[0].name, "openai-primary");
    assert!(channels[0].models.contains("gpt-4o"));
}

#[tokio::test]
async fn list_channels_returns_empty_on_empty_data() {
    let server = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/channel/"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true, "data": []
        })))
        .mount(&server).await;
    let client = NewapiClient::new(server.uri(), "tok", None).unwrap();
    let channels = client.list_channels(ChannelType::Embedding).await.unwrap();
    assert!(channels.is_empty());
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-newapi-client list_channels`
Expected: compile error — `list_channels` not defined.

- [ ] **Step 3: Implement**

Append to `src/client.rs`:

```rust
impl NewapiClient {
    pub async fn list_channels(
        &self,
        channel_type: ChannelType,
    ) -> Result<Vec<Channel>, NewapiError> {
        let mut url = self.base_url.join("/api/channel/")?;
        url.query_pairs_mut()
            .append_pair("type", &channel_type.as_int().to_string());
        let r = self.http.get(url)
            .bearer_auth(self.admin_or_user_token())
            .send().await?;
        let status = r.status();
        if !status.is_success() {
            let body = r.text().await.unwrap_or_default();
            return Err(NewapiError::Upstream { status: status.as_u16(), body });
        }
        let env: NewapiEnvelope<Vec<Channel>> = r.json().await?;
        Ok(env.data)
    }
}
```

- [ ] **Step 4: Run test**

Run: `cargo test -p corlinman-newapi-client list_channels`
Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-newapi-client/
git commit -m "feat(newapi-client): list_channels() with type filter"
```

---

### Task 7: Implement `test_round_trip()` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-newapi-client/src/client.rs`
- Modify: `rust/crates/corlinman-newapi-client/tests/client_test.rs`

- [ ] **Step 1: Write the failing test**

Append to `tests/client_test.rs`:

```rust
#[tokio::test]
async fn test_round_trip_records_latency() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .and(header("Authorization", "Bearer user-tok"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-1", "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0, "message": { "role": "assistant", "content": "ok" },
                "finish_reason": "stop"
            }]
        })))
        .mount(&server).await;
    let client = NewapiClient::new(server.uri(), "user-tok", None).unwrap();
    let res = client.test_round_trip("gpt-4o-mini").await.unwrap();
    assert_eq!(res.status, 200);
    assert!(res.latency_ms < 5000);
    assert_eq!(res.model.as_deref(), Some("gpt-4o-mini"));
}

#[tokio::test]
async fn test_round_trip_propagates_4xx() {
    let server = MockServer::start().await;
    Mock::given(method("POST")).and(path("/v1/chat/completions"))
        .respond_with(ResponseTemplate::new(429).set_body_string("rate limited"))
        .mount(&server).await;
    let client = NewapiClient::new(server.uri(), "t", None).unwrap();
    let err = client.test_round_trip("x").await.unwrap_err();
    matches!(err, NewapiError::Upstream { status: 429, .. });
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-newapi-client test_round_trip`
Expected: compile error.

- [ ] **Step 3: Implement**

Append to `src/client.rs`:

```rust
#[derive(Debug, Deserialize)]
struct ChatCompletionMin {
    model: String,
}

impl NewapiClient {
    pub async fn test_round_trip(&self, model: &str) -> Result<TestResult, NewapiError> {
        let url = self.base_url.join("/v1/chat/completions")?;
        let payload = serde_json::json!({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0
        });
        let started = Instant::now();
        let r = self.http.post(url)
            .bearer_auth(&self.user_token)
            .json(&payload)
            .send().await?;
        let latency_ms = started.elapsed().as_millis();
        let status = r.status();
        if !status.is_success() {
            let body = r.text().await.unwrap_or_default();
            return Err(NewapiError::Upstream { status: status.as_u16(), body });
        }
        let parsed: ChatCompletionMin = r.json().await.unwrap_or(ChatCompletionMin {
            model: model.to_string(),
        });
        Ok(TestResult { status: status.as_u16(), latency_ms, model: Some(parsed.model) })
    }
}
```

- [ ] **Step 4: Run test**

Run: `cargo test -p corlinman-newapi-client`
Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-newapi-client/
git commit -m "feat(newapi-client): test_round_trip() with latency measurement"
```

---

## Phase 3 — `/admin/newapi` routes

### Task 8: Scaffold admin/newapi module + GET endpoint (TDD)

**Files:**
- Create: `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs`
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/mod.rs`
- Create: `rust/crates/corlinman-gateway/tests/admin_newapi.rs`
- Modify: `rust/crates/corlinman-gateway/Cargo.toml` (add `corlinman-newapi-client` dep)

- [ ] **Step 1: Add dep + register module**

Edit `rust/crates/corlinman-gateway/Cargo.toml`:

```toml
[dependencies]
corlinman-newapi-client = { path = "../corlinman-newapi-client" }
```
(append to the existing `[dependencies]` section; preserve other entries)

Edit `rust/crates/corlinman-gateway/src/routes/admin/mod.rs`. Find the `mod` declarations and add `pub mod newapi;` alphabetically (after `models` if present). In the router builder (where other admin sub-routers are nested), add `.merge(newapi::router())`.

- [ ] **Step 2: Write failing test**

Create `rust/crates/corlinman-gateway/tests/admin_newapi.rs`:

```rust
//! Integration tests for /admin/newapi/* routes.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::json;
use tower::ServiceExt;

mod common;
use common::{boot_test_gateway, sign_admin_cookie, ProvisionedGateway};

#[tokio::test]
async fn get_newapi_returns_503_when_no_newapi_provider_configured() {
    let ProvisionedGateway { app, .. } = boot_test_gateway().await;
    let req = Request::builder()
        .method("GET")
        .uri("/admin/newapi")
        .header("Cookie", sign_admin_cookie())
        .body(Body::empty()).unwrap();
    let res = app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn get_newapi_returns_summary_when_provider_present() {
    let mut gw = boot_test_gateway().await;
    gw.upsert_provider(json!({
        "name": "newapi",
        "kind": "newapi",
        "base_url": "http://localhost:3000",
        "api_key": { "value": "sk-test" },
        "enabled": true,
        "params": {
            "newapi_admin_url": "http://localhost:3000/api",
            "newapi_admin_key": { "value": "sys-test" }
        }
    })).await.unwrap();

    let req = Request::builder().method("GET").uri("/admin/newapi")
        .header("Cookie", sign_admin_cookie())
        .body(Body::empty()).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let body = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let j: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(j["connection"]["base_url"], "http://localhost:3000");
    assert!(j["connection"]["token_masked"].as_str().unwrap().contains("..."));
    assert_eq!(j["connection"]["admin_key_present"], true);
}
```

If `tests/common/` helpers (boot_test_gateway, sign_admin_cookie) don't exist yet, look in `rust/crates/corlinman-gateway/tests/` for how other integration tests boot the gateway and copy that pattern. If the gateway uses an in-process test helper inside `src/` instead (`mod test_support`), use that.

- [ ] **Step 3: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_newapi`
Expected: compile error — `newapi::router` undefined.

- [ ] **Step 4: Implement `admin/newapi.rs`**

Create `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs`:

```rust
//! /admin/newapi — connector for the QuantumNous/new-api sidecar.
//!
//! Read-only summary + channel discovery + connection probe + 1-token
//! round-trip test. Writes back via PATCH (atomic config.toml mutation
//! through the existing admin-write mutex). See
//! `docs/superpowers/specs/2026-05-13-newapi-integration-design.md` §5.3.

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Json, Response},
    routing::{get, patch, post},
    Router,
};
use corlinman_core::config::{ProviderEntry, ProviderKind, SecretRef};
use corlinman_newapi_client::{ChannelType, NewapiClient, NewapiError};
use serde::{Deserialize, Serialize};

use crate::state::AdminState;

pub fn router() -> Router<AdminState> {
    Router::new()
        .route("/admin/newapi", get(get_summary).patch(patch_connection))
        .route("/admin/newapi/channels", get(get_channels))
        .route("/admin/newapi/probe", post(post_probe))
        .route("/admin/newapi/test", post(post_test))
}

#[derive(Serialize)]
struct Summary {
    connection: ConnectionView,
    status: String,
}

#[derive(Serialize)]
struct ConnectionView {
    base_url: String,
    token_masked: String,
    admin_key_present: bool,
}

fn find_newapi_provider(state: &AdminState) -> Option<ProviderEntry> {
    let snap = state.config_snapshot();
    snap.providers.entries.iter()
        .find(|(_, e)| e.kind == ProviderKind::Newapi && e.enabled)
        .map(|(_, e)| e.clone())
}

fn mask_token(t: &str) -> String {
    let bytes = t.as_bytes();
    if bytes.len() <= 8 { return "***".into(); }
    format!("{}...{}",
        std::str::from_utf8(&bytes[..4]).unwrap_or("****"),
        std::str::from_utf8(&bytes[bytes.len()-4..]).unwrap_or("****"))
}

async fn get_summary(State(state): State<AdminState>) -> Response {
    let Some(entry) = find_newapi_provider(&state) else {
        return (StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "no_newapi_provider"}))).into_response();
    };
    let token = entry.api_key.resolve(&state.env).unwrap_or_default();
    let admin_key_present = entry.params.get("newapi_admin_key").is_some();
    let view = Summary {
        connection: ConnectionView {
            base_url: entry.base_url.unwrap_or_default(),
            token_masked: mask_token(&token),
            admin_key_present,
        },
        status: "ok".into(),
    };
    Json(view).into_response()
}

async fn patch_connection(State(_state): State<AdminState>, Json(_body): Json<serde_json::Value>) -> Response {
    // Implemented in Task 12.
    (StatusCode::NOT_IMPLEMENTED, "patch coming in task 12").into_response()
}

async fn get_channels(State(_state): State<AdminState>, Query(_q): Query<serde_json::Value>) -> Response {
    // Implemented in Task 10.
    (StatusCode::NOT_IMPLEMENTED, "get channels coming in task 10").into_response()
}

async fn post_probe(State(_state): State<AdminState>, Json(_body): Json<serde_json::Value>) -> Response {
    // Implemented in Task 9.
    (StatusCode::NOT_IMPLEMENTED, "probe coming in task 9").into_response()
}

async fn post_test(State(_state): State<AdminState>, Json(_body): Json<serde_json::Value>) -> Response {
    // Implemented in Task 11.
    (StatusCode::NOT_IMPLEMENTED, "test coming in task 11").into_response()
}
```

The `AdminState::config_snapshot()` and `state.env` access patterns must match what other admin routes use (e.g. `admin/providers.rs`). Adjust field names to match — the names above are stand-ins.

- [ ] **Step 5: Run tests**

Run: `cargo test -p corlinman-gateway --test admin_newapi`
Expected: 2 tests pass (`get_newapi_returns_503` + `get_newapi_returns_summary`).

- [ ] **Step 6: Commit**

```bash
git add rust/crates/corlinman-gateway/src/routes/admin/newapi.rs \
        rust/crates/corlinman-gateway/src/routes/admin/mod.rs \
        rust/crates/corlinman-gateway/Cargo.toml \
        rust/crates/corlinman-gateway/tests/admin_newapi.rs
git commit -m "feat(gateway): /admin/newapi summary endpoint + integration test"
```

---

### Task 9: `POST /admin/newapi/probe` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs`
- Modify: `rust/crates/corlinman-gateway/tests/admin_newapi.rs`

- [ ] **Step 1: Write failing test**

Append to `tests/admin_newapi.rs`:

```rust
#[tokio::test]
async fn post_probe_returns_200_when_newapi_reachable() {
    use wiremock::{Mock, MockServer, ResponseTemplate};
    use wiremock::matchers::{method, path};

    let upstream = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/user/self"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "success": true,
            "data": {"id": 1, "username": "root", "role": 100, "status": 1}
        }))).mount(&upstream).await;
    Mock::given(method("GET")).and(path("/api/status"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "success": true, "data": {"version": "v0.4.0"}
        }))).mount(&upstream).await;

    let gw = boot_test_gateway().await;
    let req = Request::builder().method("POST").uri("/admin/newapi/probe")
        .header("Cookie", sign_admin_cookie())
        .header("Content-Type", "application/json")
        .body(Body::from(serde_json::to_vec(&serde_json::json!({
            "base_url": upstream.uri(),
            "token": "user-tok",
            "admin_token": "admin-tok"
        })).unwrap())).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let body = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let j: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(j["user"]["username"], "root");
    assert_eq!(j["server_version"], "v0.4.0");
}

#[tokio::test]
async fn post_probe_returns_400_when_unreachable() {
    let gw = boot_test_gateway().await;
    let req = Request::builder().method("POST").uri("/admin/newapi/probe")
        .header("Cookie", sign_admin_cookie())
        .header("Content-Type", "application/json")
        .body(Body::from(serde_json::to_vec(&serde_json::json!({
            "base_url": "http://127.0.0.1:1",
            "token": "x"
        })).unwrap())).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    let body = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let j: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(j["error"], "newapi_unreachable");
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_newapi post_probe`
Expected: tests fail with 501 NOT_IMPLEMENTED.

- [ ] **Step 3: Implement `post_probe`**

Replace the stub `post_probe` in `admin/newapi.rs` with:

```rust
#[derive(Deserialize)]
struct ProbeBody {
    base_url: String,
    token: String,
    admin_token: Option<String>,
}

async fn post_probe(State(_state): State<AdminState>, Json(body): Json<ProbeBody>) -> Response {
    let client = match NewapiClient::new(&body.base_url, &body.token, body.admin_token.clone()) {
        Ok(c) => c,
        Err(_) => return (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_bad_url"}))).into_response(),
    };
    match client.probe().await {
        Ok(r) => Json(serde_json::json!({
            "base_url": r.base_url,
            "user": r.user,
            "server_version": r.server_version,
        })).into_response(),
        Err(NewapiError::Upstream { status: 401, .. }) => (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_token_invalid"}))).into_response(),
        Err(NewapiError::NotNewapi) => (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_version_too_old"}))).into_response(),
        Err(NewapiError::Http(_)) => (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_unreachable"}))).into_response(),
        Err(e) => (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_probe_failed", "detail": e.to_string()}))).into_response(),
    }
}
```

- [ ] **Step 4: Run test**

Run: `cargo test -p corlinman-gateway --test admin_newapi post_probe`
Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): POST /admin/newapi/probe with reachability + token validation"
```

---

### Task 10: `GET /admin/newapi/channels` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs`
- Modify: `rust/crates/corlinman-gateway/tests/admin_newapi.rs`

- [ ] **Step 1: Write failing test**

Append to `tests/admin_newapi.rs`:

```rust
#[tokio::test]
async fn get_channels_returns_filtered_list() {
    use wiremock::{Mock, MockServer, ResponseTemplate};
    use wiremock::matchers::{method, path, query_param};

    let upstream = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/channel/"))
        .and(query_param("type", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "success": true, "data": [
                {"id": 1, "name": "openai-prim", "type": 1, "status": 1,
                 "models": "gpt-4o,gpt-4o-mini", "group": "default"},
            ]
        }))).mount(&upstream).await;

    let mut gw = boot_test_gateway().await;
    gw.upsert_provider(serde_json::json!({
        "name": "newapi",
        "kind": "newapi",
        "base_url": upstream.uri(),
        "api_key": {"value": "sk-test"},
        "enabled": true,
        "params": {
            "newapi_admin_url": format!("{}/api", upstream.uri()),
            "newapi_admin_key": {"value": "sys-test"}
        }
    })).await.unwrap();

    let req = Request::builder().method("GET")
        .uri("/admin/newapi/channels?type=llm")
        .header("Cookie", sign_admin_cookie())
        .body(Body::empty()).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let body = axum::body::to_bytes(res.into_body(), 8192).await.unwrap();
    let j: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(j["channels"][0]["name"], "openai-prim");
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_newapi get_channels`
Expected: fails with 501 NOT_IMPLEMENTED.

- [ ] **Step 3: Implement**

Replace `get_channels` stub:

```rust
#[derive(Deserialize)]
struct ChannelsQuery {
    #[serde(rename = "type")]
    channel_type: String,  // "llm" | "embedding" | "tts"
}

async fn get_channels(State(state): State<AdminState>, Query(q): Query<ChannelsQuery>) -> Response {
    let Some(entry) = find_newapi_provider(&state) else {
        return (StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "no_newapi_provider"}))).into_response();
    };
    let Some(base_url) = entry.base_url else {
        return (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_missing_base_url"}))).into_response();
    };
    let user_token = entry.api_key.resolve(&state.env).unwrap_or_default();
    let admin_token = entry.params.get("newapi_admin_key")
        .and_then(|v| serde_json::from_value::<SecretRef>(v.clone()).ok())
        .and_then(|s| s.resolve(&state.env).ok());

    let ct = match q.channel_type.as_str() {
        "llm" => ChannelType::Llm,
        "embedding" => ChannelType::Embedding,
        "tts" => ChannelType::Tts,
        _ => return (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "invalid_channel_type"}))).into_response(),
    };

    let client = match NewapiClient::new(&base_url, &user_token, admin_token) {
        Ok(c) => c,
        Err(_) => return (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_bad_url"}))).into_response(),
    };
    match client.list_channels(ct).await {
        Ok(channels) => Json(serde_json::json!({"channels": channels})).into_response(),
        Err(NewapiError::Upstream { status: 401, .. }) => (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_admin_required"}))).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({"error": "newapi_upstream_error", "detail": e.to_string()}))).into_response(),
    }
}
```

(The exact API of `SecretRef::resolve` and `state.env` may differ; mirror what `admin/embedding.rs` already does to read `entry.api_key`.)

- [ ] **Step 4: Run test**

Run: `cargo test -p corlinman-gateway --test admin_newapi get_channels`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): GET /admin/newapi/channels?type= proxies to newapi /api/channel/"
```

---

### Task 11: `POST /admin/newapi/test` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs`
- Modify: `rust/crates/corlinman-gateway/tests/admin_newapi.rs`

- [ ] **Step 1: Write failing test**

Append to `tests/admin_newapi.rs`:

```rust
#[tokio::test]
async fn post_test_returns_latency_on_success() {
    use wiremock::{Mock, MockServer, ResponseTemplate};
    use wiremock::matchers::{method, path};
    let upstream = MockServer::start().await;
    Mock::given(method("POST")).and(path("/v1/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "id": "x", "object": "chat.completion", "model": "gpt-4o-mini",
            "choices": [{"index":0,"message":{"role":"assistant","content":"ok"},
                          "finish_reason":"stop"}]
        }))).mount(&upstream).await;

    let mut gw = boot_test_gateway().await;
    gw.upsert_provider(serde_json::json!({
        "name": "newapi", "kind": "newapi",
        "base_url": upstream.uri(),
        "api_key": {"value": "sk-test"},
        "enabled": true
    })).await.unwrap();

    let req = Request::builder().method("POST").uri("/admin/newapi/test")
        .header("Cookie", sign_admin_cookie())
        .header("Content-Type", "application/json")
        .body(Body::from(serde_json::to_vec(&serde_json::json!({"model": "gpt-4o-mini"})).unwrap()))
        .unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let body = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let j: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(j["status"], 200);
    assert!(j["latency_ms"].as_u64().unwrap() < 5000);
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_newapi post_test`
Expected: fails with 501.

- [ ] **Step 3: Implement**

Replace `post_test` stub:

```rust
#[derive(Deserialize)]
struct TestBody { model: String }

async fn post_test(State(state): State<AdminState>, Json(body): Json<TestBody>) -> Response {
    let Some(entry) = find_newapi_provider(&state) else {
        return (StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "no_newapi_provider"}))).into_response();
    };
    let Some(base_url) = entry.base_url else {
        return (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_missing_base_url"}))).into_response();
    };
    let token = entry.api_key.resolve(&state.env).unwrap_or_default();
    let client = match NewapiClient::new(&base_url, &token, None) {
        Ok(c) => c,
        Err(_) => return (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "newapi_bad_url"}))).into_response(),
    };
    match client.test_round_trip(&body.model).await {
        Ok(r) => Json(serde_json::json!({
            "status": r.status,
            "latency_ms": r.latency_ms,
            "model": r.model,
        })).into_response(),
        Err(NewapiError::Upstream { status, body }) => (StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({"error": "newapi_test_failed", "upstream_status": status, "body": body}))).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({"error": "newapi_test_failed", "detail": e.to_string()}))).into_response(),
    }
}
```

- [ ] **Step 4: Run test**

Run: `cargo test -p corlinman-gateway --test admin_newapi post_test`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): POST /admin/newapi/test 1-token round-trip"
```

---

### Task 12: `PATCH /admin/newapi` connection update (TDD)

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs`
- Modify: `rust/crates/corlinman-gateway/tests/admin_newapi.rs`

- [ ] **Step 1: Write failing test**

Append to `tests/admin_newapi.rs`:

```rust
#[tokio::test]
async fn patch_newapi_updates_base_url_with_reprobe() {
    use wiremock::{Mock, MockServer, ResponseTemplate};
    use wiremock::matchers::{method, path};
    let upstream = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/user/self"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "success": true, "data": {"id":1,"username":"root","role":100,"status":1}
        }))).mount(&upstream).await;
    Mock::given(method("GET")).and(path("/api/status"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "success": true, "data": {"version":"v0.4.1"}
        }))).mount(&upstream).await;

    let mut gw = boot_test_gateway().await;
    gw.upsert_provider(serde_json::json!({
        "name":"newapi","kind":"newapi",
        "base_url":"http://old.example",
        "api_key":{"value":"sk-old"},
        "enabled":true
    })).await.unwrap();

    let req = Request::builder().method("PATCH").uri("/admin/newapi")
        .header("Cookie", sign_admin_cookie())
        .header("Content-Type","application/json")
        .body(Body::from(serde_json::to_vec(&serde_json::json!({
            "base_url": upstream.uri(),
            "token": "sk-new"
        })).unwrap())).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let snap = gw.config_snapshot();
    let entry = snap.providers.entries.get("newapi").unwrap();
    assert_eq!(entry.base_url.as_deref(), Some(upstream.uri().as_str()));
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_newapi patch_newapi`
Expected: fails with 501.

- [ ] **Step 3: Implement**

Replace `patch_connection` stub:

```rust
#[derive(Deserialize)]
struct PatchBody {
    base_url: Option<String>,
    token: Option<String>,
    admin_token: Option<String>,
}

async fn patch_connection(State(state): State<AdminState>, Json(body): Json<PatchBody>) -> Response {
    let Some(mut entry) = find_newapi_provider(&state) else {
        return (StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({"error": "no_newapi_provider"}))).into_response();
    };
    if let Some(b) = body.base_url { entry.base_url = Some(b); }
    if let Some(t) = body.token {
        entry.api_key = SecretRef::Literal(t);
    }
    if let Some(at) = body.admin_token {
        entry.params.insert("newapi_admin_key".into(),
            serde_json::to_value(SecretRef::Literal(at)).unwrap());
    }

    // Re-probe before writing.
    let url = entry.base_url.clone().unwrap_or_default();
    let tok = entry.api_key.resolve(&state.env).unwrap_or_default();
    let client = NewapiClient::new(&url, &tok, None).ok();
    if let Some(c) = client {
        if c.probe().await.is_err() {
            return (StatusCode::BAD_REQUEST,
                Json(serde_json::json!({"error": "newapi_unreachable"}))).into_response();
        }
    }
    // Atomic write via admin-write mutex.
    match state.write_provider("newapi", entry).await {
        Ok(_) => (StatusCode::OK, Json(serde_json::json!({"ok": true}))).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": "config_write_failed", "detail": e.to_string()}))).into_response(),
    }
}
```

(Adjust `state.write_provider` to the actual API in `AdminState` — mirror `admin/providers.rs` upsert path. If no helper exists, build the same TOML patch transaction it does.)

- [ ] **Step 4: Run test**

Run: `cargo test -p corlinman-gateway --test admin_newapi patch_newapi`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): PATCH /admin/newapi with re-probe before atomic write"
```

---

## Phase 4 — Onboard 4-step refactor

### Task 13: Add ephemeral onboard-session store

**Files:**
- Create: `rust/crates/corlinman-gateway/src/routes/admin/onboard_session.rs`
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/auth.rs:87` (router wiring)
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/mod.rs` (mod declaration)

- [ ] **Step 1: Write failing test**

Add to existing `rust/crates/corlinman-gateway/src/routes/admin/auth.rs` test module:

```rust
#[tokio::test]
async fn onboard_session_round_trips_state() {
    use crate::routes::admin::onboard_session::OnboardSessionStore;
    let store = OnboardSessionStore::new();
    let sid = store.create().await;
    store.set_account(&sid, "root".into()).await.unwrap();
    store.set_newapi(&sid, NewapiDraft {
        base_url: "http://localhost:3000".into(),
        token: "tok".into(),
        admin_token: Some("admin".into()),
    }).await.unwrap();
    let snap = store.snapshot(&sid).await.unwrap();
    assert_eq!(snap.account.unwrap(), "root");
    assert_eq!(snap.newapi.unwrap().base_url, "http://localhost:3000");
}
```

- [ ] **Step 2: Implement `onboard_session.rs`**

```rust
//! Ephemeral server-side store for the 4-step onboard wizard.
//!
//! Keyed by an opaque `onboard_session_id` returned to the client as
//! a short-lived cookie. State is in-memory only; gateway restart
//! kills any in-progress onboard and the user starts over. That's
//! acceptable because onboard is single-operator first-run.

use dashmap::DashMap;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::{Duration, Instant};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NewapiDraft {
    pub base_url: String,
    pub token: String,
    pub admin_token: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct OnboardSession {
    pub account: Option<String>,
    pub newapi: Option<NewapiDraft>,
    pub llm_pick: Option<ModelPick>,
    pub embedding_pick: Option<ModelPick>,
    pub tts_pick: Option<ModelPick>,
    pub created_at: Option<Instant>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelPick {
    pub channel_id: u64,
    pub model: String,
    pub voice: Option<String>,
}

#[derive(Clone, Default)]
pub struct OnboardSessionStore {
    inner: Arc<DashMap<String, OnboardSession>>,
}

impl OnboardSessionStore {
    pub fn new() -> Self { Self::default() }

    pub async fn create(&self) -> String {
        let id = Uuid::new_v4().to_string();
        self.inner.insert(id.clone(), OnboardSession { created_at: Some(Instant::now()), ..Default::default() });
        id
    }

    pub async fn set_account(&self, sid: &str, account: String) -> Result<(), &'static str> {
        let mut e = self.inner.get_mut(sid).ok_or("session_not_found")?;
        e.account = Some(account);
        Ok(())
    }

    pub async fn set_newapi(&self, sid: &str, draft: NewapiDraft) -> Result<(), &'static str> {
        let mut e = self.inner.get_mut(sid).ok_or("session_not_found")?;
        e.newapi = Some(draft);
        Ok(())
    }

    pub async fn set_picks(&self, sid: &str, llm: ModelPick, emb: ModelPick, tts: ModelPick) -> Result<(), &'static str> {
        let mut e = self.inner.get_mut(sid).ok_or("session_not_found")?;
        e.llm_pick = Some(llm);
        e.embedding_pick = Some(emb);
        e.tts_pick = Some(tts);
        Ok(())
    }

    pub async fn snapshot(&self, sid: &str) -> Option<OnboardSession> {
        self.inner.get(sid).map(|v| v.clone())
    }

    pub async fn destroy(&self, sid: &str) {
        self.inner.remove(sid);
    }

    pub fn sweep_stale(&self, max_age: Duration) {
        self.inner.retain(|_, v| v.created_at.map(|t| t.elapsed() < max_age).unwrap_or(false));
    }
}
```

Register `pub mod onboard_session;` in `routes/admin/mod.rs`. Add `dashmap` and `uuid` to gateway deps if not already present.

- [ ] **Step 3: Run test**

Run: `cargo test -p corlinman-gateway routes::admin::auth::tests::onboard_session_round_trips`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): ephemeral onboard-session store for 4-step wizard"
```

---

### Task 14: New `POST /admin/onboard/account` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/auth.rs:87,261-369`
- Create: `rust/crates/corlinman-gateway/tests/admin_onboard.rs`

- [ ] **Step 1: Write failing test in new integration test file**

```rust
//! 4-step onboard flow integration tests.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::json;
use tower::ServiceExt;

mod common;
use common::{boot_test_gateway, ProvisionedGateway};

#[tokio::test]
async fn onboard_account_first_run_creates_session_and_admin() {
    let gw = boot_test_gateway().await;
    let req = Request::builder().method("POST")
        .uri("/admin/onboard/account")
        .header("Content-Type","application/json")
        .body(Body::from(serde_json::to_vec(&json!({
            "username":"root", "password":"s3cret-strong",
            "password_confirm":"s3cret-strong"
        })).unwrap())).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    // Cookie issued
    let cookie_header = res.headers().get("Set-Cookie").map(|v| v.to_str().unwrap().to_string());
    assert!(cookie_header.as_deref().unwrap_or("").contains("onboard_session_id="));
    let body = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let j: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(j["next"], "newapi");
}

#[tokio::test]
async fn onboard_account_returns_409_when_admin_already_configured() {
    let gw = boot_test_gateway_with_admin().await;
    let req = Request::builder().method("POST").uri("/admin/onboard/account")
        .header("Content-Type","application/json")
        .body(Body::from(serde_json::to_vec(&json!({
            "username":"x","password":"y123456789","password_confirm":"y123456789"
        })).unwrap())).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::CONFLICT);
}
```

(If `boot_test_gateway_with_admin` doesn't exist, add it to `tests/common/mod.rs` modeled on the existing `boot_test_gateway`.)

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_onboard`
Expected: 404 (route not defined) or compile error.

- [ ] **Step 3: Migrate existing `onboard` handler**

In `routes/admin/auth.rs:278` rename the current `async fn onboard(...)` → `async fn onboard_account(...)`. The body that writes `[admin]` stays unchanged. Return `{ "next": "newapi" }` with a `Set-Cookie: onboard_session_id=<uuid>` after creating a session via `OnboardSessionStore`.

Add route at line ~87:
```rust
.route("/admin/onboard/account", post(onboard_account))
.route("/admin/onboard", post(onboard_legacy_410))  // deprecation stub
```

Add `onboard_legacy_410`:
```rust
async fn onboard_legacy_410() -> Response {
    (StatusCode::GONE,
        Json(serde_json::json!({
            "error": "onboard_endpoint_split",
            "message": "Use POST /admin/onboard/account → /newapi → /newapi/select → /finalize"
        }))).into_response()
}
```

Adjust the existing onboard tests in `auth.rs:696-783` to point at `/admin/onboard/account` and assert the new response shape (`next: "newapi"` + Set-Cookie).

- [ ] **Step 4: Run all onboard tests**

```bash
cargo test -p corlinman-gateway --test admin_onboard
cargo test -p corlinman-gateway --lib routes::admin::auth::tests::onboard
```
Expected: all pass (new ones + migrated old ones).

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): split /admin/onboard into /onboard/account step; legacy returns 410"
```

---

### Task 15: `POST /admin/onboard/newapi` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/auth.rs`
- Modify: `rust/crates/corlinman-gateway/tests/admin_onboard.rs`

- [ ] **Step 1: Write failing test**

Append to `tests/admin_onboard.rs`:

```rust
#[tokio::test]
async fn onboard_newapi_probe_persists_into_session() {
    use wiremock::{Mock, MockServer, ResponseTemplate};
    use wiremock::matchers::{method, path};
    let upstream = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/user/self"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success":true,"data":{"id":1,"username":"root","role":100,"status":1}
        }))).mount(&upstream).await;
    Mock::given(method("GET")).and(path("/api/status"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success":true,"data":{"version":"v0.4"}
        }))).mount(&upstream).await;
    Mock::given(method("GET")).and(path("/api/channel/"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success":true,"data":[{"id":1,"name":"c","type":1,"status":1,"models":"m","group":"g"}]
        }))).mount(&upstream).await;

    let gw = boot_test_gateway().await;
    let req1 = Request::builder().method("POST").uri("/admin/onboard/account")
        .header("Content-Type","application/json")
        .body(Body::from(serde_json::to_vec(&json!({
            "username":"r","password":"p123456789","password_confirm":"p123456789"
        })).unwrap())).unwrap();
    let res1 = gw.app.clone().oneshot(req1).await.unwrap();
    let cookie = res1.headers().get("Set-Cookie").unwrap().to_str().unwrap().to_string();

    let req2 = Request::builder().method("POST").uri("/admin/onboard/newapi")
        .header("Cookie", &cookie)
        .header("Content-Type","application/json")
        .body(Body::from(serde_json::to_vec(&json!({
            "base_url": upstream.uri(),
            "token": "user-tok",
            "admin_token": "admin-tok"
        })).unwrap())).unwrap();
    let res2 = gw.app.oneshot(req2).await.unwrap();
    assert_eq!(res2.status(), StatusCode::OK);
    let body = axum::body::to_bytes(res2.into_body(), 4096).await.unwrap();
    let j: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(j["next"], "models");
    assert!(j["channels_available"].as_u64().unwrap() >= 1);
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_onboard onboard_newapi_probe`
Expected: 404.

- [ ] **Step 3: Implement handler**

In `routes/admin/auth.rs`:

```rust
.route("/admin/onboard/newapi", post(onboard_newapi))
```

```rust
#[derive(Deserialize)]
struct OnboardNewapiBody {
    base_url: String, token: String, admin_token: Option<String>,
}

async fn onboard_newapi(
    State(state): State<AdminState>,
    cookies: axum_extra::extract::CookieJar,
    Json(body): Json<OnboardNewapiBody>,
) -> Response {
    let Some(sid_cookie) = cookies.get("onboard_session_id") else {
        return (StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({"error":"onboard_session_required"}))).into_response();
    };
    let sid = sid_cookie.value().to_string();
    let store = &state.onboard_sessions;
    if store.snapshot(&sid).await.is_none() {
        return (StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({"error":"onboard_session_expired"}))).into_response();
    }

    let client = match corlinman_newapi_client::NewapiClient::new(
        &body.base_url, &body.token, body.admin_token.clone()
    ) {
        Ok(c) => c,
        Err(_) => return (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error":"newapi_bad_url"}))).into_response(),
    };
    if let Err(e) = client.probe().await {
        return (StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": map_probe_error(&e)}))).into_response();
    }
    // Count channels for the response hint.
    let count = client.list_channels(corlinman_newapi_client::ChannelType::Llm)
        .await.map(|v| v.len()).unwrap_or(0);

    store.set_newapi(&sid, crate::routes::admin::onboard_session::NewapiDraft {
        base_url: body.base_url, token: body.token, admin_token: body.admin_token,
    }).await.ok();

    Json(serde_json::json!({
        "next": "models",
        "channels_available": count,
    })).into_response()
}

fn map_probe_error(e: &corlinman_newapi_client::NewapiError) -> &'static str {
    use corlinman_newapi_client::NewapiError::*;
    match e {
        Upstream { status: 401, .. } => "newapi_token_invalid",
        Upstream { status: 403, .. } => "newapi_admin_required",
        NotNewapi => "newapi_version_too_old",
        Http(_) => "newapi_unreachable",
        _ => "newapi_probe_failed",
    }
}
```

- [ ] **Step 4: Run test**

Run: `cargo test -p corlinman-gateway --test admin_onboard onboard_newapi`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): POST /admin/onboard/newapi probes + saves draft"
```

---

### Task 16: `GET /admin/onboard/newapi/channels` + `POST /admin/onboard/newapi/select`

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/auth.rs`
- Modify: `rust/crates/corlinman-gateway/tests/admin_onboard.rs`

- [ ] **Step 1: Write failing tests**

Append to `tests/admin_onboard.rs`:

```rust
#[tokio::test]
async fn onboard_channels_returns_typed_lists_from_draft() {
    // setup: account → newapi step done (use the same upstream mocks
    // as the prior test, abstract into a helper if you write more
    // than one such test). Then:
    let (gw, cookie, upstream) = setup_onboarded_through_newapi().await;
    
    let req = Request::builder().method("GET")
        .uri("/admin/onboard/newapi/channels?type=llm")
        .header("Cookie", &cookie)
        .body(Body::empty()).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let j: serde_json::Value = serde_json::from_slice(
        &axum::body::to_bytes(res.into_body(), 8192).await.unwrap()).unwrap();
    assert!(j["channels"].as_array().unwrap().len() >= 1);
}

#[tokio::test]
async fn onboard_select_records_picks_and_advances() {
    let (gw, cookie, _upstream) = setup_onboarded_through_newapi().await;
    let req = Request::builder().method("POST").uri("/admin/onboard/newapi/select")
        .header("Cookie", &cookie)
        .header("Content-Type","application/json")
        .body(Body::from(serde_json::to_vec(&json!({
            "llm": {"channel_id":1,"model":"gpt-4o-mini"},
            "embedding": {"channel_id":2,"model":"text-embedding-3-small"},
            "tts": {"channel_id":3,"model":"tts-1","voice":"alloy"}
        })).unwrap())).unwrap();
    let res = gw.app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let j: serde_json::Value = serde_json::from_slice(
        &axum::body::to_bytes(res.into_body(), 4096).await.unwrap()).unwrap();
    assert_eq!(j["next"], "confirm");
    assert!(j["preview"]["providers"]["newapi"].is_object());
}

async fn setup_onboarded_through_newapi() -> (ProvisionedGateway, String, wiremock::MockServer) {
    // ... (compose account + newapi mocks; return gw + cookie + upstream)
    // Implementer: extract from prior test bodies and inline here.
    todo!()
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_onboard onboard_channels`
Expected: 404.

- [ ] **Step 3: Implement both handlers**

Add routes:
```rust
.route("/admin/onboard/newapi/channels", get(onboard_channels))
.route("/admin/onboard/newapi/select", post(onboard_select))
```

```rust
async fn onboard_channels(
    State(state): State<AdminState>,
    cookies: axum_extra::extract::CookieJar,
    Query(q): Query<crate::routes::admin::newapi::ChannelsQuery>,
) -> Response {
    let Some(sid_cookie) = cookies.get("onboard_session_id") else {
        return (StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({"error":"onboard_session_required"}))).into_response();
    };
    let Some(snap) = state.onboard_sessions.snapshot(sid_cookie.value()).await else {
        return (StatusCode::UNAUTHORIZED, Json(serde_json::json!({"error":"onboard_session_expired"}))).into_response();
    };
    let Some(draft) = snap.newapi else {
        return (StatusCode::BAD_REQUEST, Json(serde_json::json!({"error":"newapi_step_missing"}))).into_response();
    };
    let client = corlinman_newapi_client::NewapiClient::new(
        &draft.base_url, &draft.token, draft.admin_token.clone()
    ).map_err(|_| ()).ok();
    let Some(client) = client else {
        return (StatusCode::BAD_REQUEST, Json(serde_json::json!({"error":"newapi_bad_url"}))).into_response();
    };
    let ct = match q.channel_type.as_str() {
        "llm" => corlinman_newapi_client::ChannelType::Llm,
        "embedding" => corlinman_newapi_client::ChannelType::Embedding,
        "tts" => corlinman_newapi_client::ChannelType::Tts,
        _ => return (StatusCode::BAD_REQUEST, Json(serde_json::json!({"error":"invalid_channel_type"}))).into_response(),
    };
    match client.list_channels(ct).await {
        Ok(channels) => Json(serde_json::json!({"channels": channels})).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({"error":"newapi_upstream_error","detail":e.to_string()}))).into_response(),
    }
}

#[derive(Deserialize)]
struct SelectBody {
    llm: ModelPickIn,
    embedding: ModelPickIn,
    tts: ModelPickIn,
}
#[derive(Deserialize)]
struct ModelPickIn {
    channel_id: u64,
    model: String,
    voice: Option<String>,
}

async fn onboard_select(
    State(state): State<AdminState>,
    cookies: axum_extra::extract::CookieJar,
    Json(body): Json<SelectBody>,
) -> Response {
    let Some(c) = cookies.get("onboard_session_id") else {
        return (StatusCode::UNAUTHORIZED, Json(serde_json::json!({"error":"onboard_session_required"}))).into_response();
    };
    let pick = |p: ModelPickIn| crate::routes::admin::onboard_session::ModelPick {
        channel_id: p.channel_id, model: p.model, voice: p.voice,
    };
    state.onboard_sessions.set_picks(c.value(), pick(body.llm), pick(body.embedding), pick(body.tts)).await.ok();
    let snap = state.onboard_sessions.snapshot(c.value()).await;
    Json(serde_json::json!({
        "next": "confirm",
        "preview": snap, // session snapshot serves as the diff preview
    })).into_response()
}
```

Make sure `ChannelsQuery` in `admin/newapi.rs` is `pub` so this handler can reuse it.

- [ ] **Step 4: Run tests**

Run: `cargo test -p corlinman-gateway --test admin_onboard onboard_channels onboard_select`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): onboard channels listing + model selection step"
```

---

### Task 17: `POST /admin/onboard/finalize` (TDD)

**Files:**
- Modify: `rust/crates/corlinman-gateway/src/routes/admin/auth.rs`
- Modify: `rust/crates/corlinman-gateway/tests/admin_onboard.rs`

- [ ] **Step 1: Write failing test**

```rust
#[tokio::test]
async fn onboard_finalize_writes_full_config_and_clears_session() {
    let (gw, cookie, upstream) = setup_onboarded_through_newapi().await;
    // First do the select
    let select_req = Request::builder().method("POST").uri("/admin/onboard/newapi/select")
        .header("Cookie", &cookie)
        .header("Content-Type","application/json")
        .body(Body::from(serde_json::to_vec(&json!({
            "llm": {"channel_id":1,"model":"gpt-4o-mini"},
            "embedding": {"channel_id":2,"model":"text-embedding-3-small"},
            "tts": {"channel_id":3,"model":"tts-1","voice":"alloy"}
        })).unwrap())).unwrap();
    gw.app.clone().oneshot(select_req).await.unwrap();

    let fin_req = Request::builder().method("POST").uri("/admin/onboard/finalize")
        .header("Cookie", &cookie)
        .body(Body::empty()).unwrap();
    let res = gw.app.clone().oneshot(fin_req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);

    let snap = gw.config_snapshot();
    assert!(snap.providers.entries.contains_key("newapi"));
    let newapi_provider = &snap.providers.entries["newapi"];
    assert_eq!(newapi_provider.kind, ProviderKind::Newapi);
    assert!(snap.embedding.as_ref().map(|e| e.provider == "newapi").unwrap_or(false));
    assert_eq!(snap.voice.as_ref().map(|v| v.provider_alias.clone()).unwrap_or_default(), "newapi");
}
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-gateway --test admin_onboard onboard_finalize`
Expected: 404.

- [ ] **Step 3: Implement**

```rust
.route("/admin/onboard/finalize", post(onboard_finalize))
```

```rust
async fn onboard_finalize(
    State(state): State<AdminState>,
    cookies: axum_extra::extract::CookieJar,
) -> Response {
    let Some(c) = cookies.get("onboard_session_id") else {
        return (StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({"error":"onboard_session_required"}))).into_response();
    };
    let Some(snap) = state.onboard_sessions.snapshot(c.value()).await else {
        return (StatusCode::UNAUTHORIZED, Json(serde_json::json!({"error":"onboard_session_expired"}))).into_response();
    };
    let (Some(newapi), Some(llm), Some(emb), Some(tts)) =
        (snap.newapi, snap.llm_pick, snap.embedding_pick, snap.tts_pick) else {
        return (StatusCode::BAD_REQUEST, Json(serde_json::json!({"error":"onboard_incomplete"}))).into_response();
    };

    // Re-probe before write
    let client = corlinman_newapi_client::NewapiClient::new(
        &newapi.base_url, &newapi.token, newapi.admin_token.clone()
    ).ok();
    if let Some(c) = client {
        if c.probe().await.is_err() {
            return (StatusCode::SERVICE_UNAVAILABLE,
                Json(serde_json::json!({"error":"newapi_unreachable_at_finalize"}))).into_response();
        }
    }

    // Atomic write
    let mut patch = serde_json::json!({
        "providers": {
            "newapi": {
                "kind": "newapi",
                "base_url": newapi.base_url,
                "api_key": {"value": newapi.token},
                "enabled": true,
                "params": {
                    "newapi_admin_url": format!("{}/api", newapi.base_url.trim_end_matches('/')),
                    "newapi_admin_key": newapi.admin_token.map(|t| serde_json::json!({"value": t})).unwrap_or(serde_json::Value::Null)
                }
            }
        },
        "models": {
            "default": llm.model.clone(),
            "aliases": {
                llm.model.clone(): {
                    "model": llm.model,
                    "provider": "newapi"
                }
            }
        },
        "embedding": {
            "enabled": true,
            "provider": "newapi",
            "model": emb.model,
            "dimension": 1536
        },
        "voice": {
            "enabled": true,
            "provider_alias": "newapi",
            "tts_model": tts.model,
            "tts_voice": tts.voice.unwrap_or_else(|| "alloy".into()),
            "sample_rate_hz_out": 24000
        }
    });

    match state.admin_write_config_patch(&mut patch).await {
        Ok(_) => {
            state.onboard_sessions.destroy(c.value()).await;
            Json(serde_json::json!({"ok": true, "redirect": "/login"})).into_response()
        },
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error":"config_write_failed","detail":e.to_string()}))).into_response(),
    }
}
```

`admin_write_config_patch` must be implemented (or rename to whatever already exists). Mirror `admin/providers.rs` upsert behavior. Use the existing per-process admin-write mutex (`99f8390 fix(auth)`).

- [ ] **Step 4: Run test**

Run: `cargo test -p corlinman-gateway --test admin_onboard onboard_finalize`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-gateway/
git commit -m "feat(gateway): POST /admin/onboard/finalize writes atomic config + destroys session"
```

---

## Phase 5 — migrate-sub2api CLI

### Task 18: `corlinman config migrate-sub2api` (TDD)

**Files:**
- Create: `rust/crates/corlinman-cli/src/cmd/migrate.rs`
- Modify: `rust/crates/corlinman-cli/src/cmd/config.rs` (add `MigrateSub2api` variant)
- Modify: `rust/crates/corlinman-cli/src/cmd/mod.rs` (re-export)
- Create: `rust/crates/corlinman-cli/tests/migrate_test.rs`

- [ ] **Step 1: Write failing test**

```rust
//! CLI integration tests for `corlinman config migrate-sub2api`.
use assert_cmd::Command;
use predicates::str::contains;
use std::fs;
use tempfile::tempdir;

#[test]
fn migrate_dry_run_prints_diff() {
    let tmp = tempdir().unwrap();
    let cfg = tmp.path().join("config.toml");
    fs::write(&cfg, r#"
[providers.subhub]
kind = "sub2api"
base_url = "http://127.0.0.1:7980"
api_key = { env = "SUB2API_KEY" }
enabled = true

[providers.openai]
kind = "openai"
api_key = { env = "OPENAI_API_KEY" }
enabled = true
"#).unwrap();

    Command::cargo_bin("corlinman").unwrap()
        .args(["config","migrate-sub2api","--config",cfg.to_str().unwrap(),"--dry-run"])
        .assert()
        .success()
        .stdout(contains("[providers.subhub]"))
        .stdout(contains("kind = \"sub2api\""))
        .stdout(contains("- kind = \"sub2api\""))
        .stdout(contains("+ kind = \"newapi\""));
    let after = fs::read_to_string(&cfg).unwrap();
    assert!(after.contains("kind = \"sub2api\""), "dry-run must NOT modify file");
}

#[test]
fn migrate_apply_rewrites_kind_in_place() {
    let tmp = tempdir().unwrap();
    let cfg = tmp.path().join("config.toml");
    fs::write(&cfg, r#"
[providers.subhub]
kind = "sub2api"
base_url = "http://127.0.0.1:7980"
api_key = { env = "SUB2API_KEY" }
enabled = true
"#).unwrap();
    Command::cargo_bin("corlinman").unwrap()
        .args(["config","migrate-sub2api","--config",cfg.to_str().unwrap(),"--apply"])
        .assert().success();
    let after = fs::read_to_string(&cfg).unwrap();
    assert!(after.contains("kind = \"newapi\""));
    assert!(!after.contains("kind = \"sub2api\""));
    assert!(after.contains("http://127.0.0.1:7980"), "base_url preserved");
}

#[test]
fn migrate_apply_is_idempotent() {
    let tmp = tempdir().unwrap();
    let cfg = tmp.path().join("config.toml");
    fs::write(&cfg, r#"
[providers.x]
kind = "newapi"
base_url = "http://x"
api_key = { value = "k" }
enabled = true
"#).unwrap();
    let before = fs::read_to_string(&cfg).unwrap();
    Command::cargo_bin("corlinman").unwrap()
        .args(["config","migrate-sub2api","--config",cfg.to_str().unwrap(),"--apply"])
        .assert().success().stdout(contains("no_sub2api_entries_found"));
    let after = fs::read_to_string(&cfg).unwrap();
    assert_eq!(before, after);
}
```

Add `assert_cmd`, `predicates`, `tempfile` as `[dev-dependencies]` in `corlinman-cli/Cargo.toml`.

- [ ] **Step 2: Run test (verify it fails)**

Run: `cargo test -p corlinman-cli --test migrate_test`
Expected: compile error — subcommand not defined.

- [ ] **Step 3: Implement `cmd/migrate.rs`**

```rust
//! `corlinman config migrate-sub2api` implementation.
//!
//! Reads config.toml, finds every `[providers.<x>] kind = "sub2api"`
//! entry, and rewrites to `kind = "newapi"`. Preserves base_url,
//! api_key, enabled, params unchanged. Unrecognised legacy fields are
//! left in place with a `# legacy sub2api field: review manually`
//! comment.

use clap::Args;
use std::fs;
use std::path::PathBuf;

#[derive(Debug, Args)]
pub struct MigrateArgs {
    /// Path to config.toml (default resolves via CORLINMAN_CONFIG env).
    #[arg(long)]
    pub config: Option<PathBuf>,
    /// Print diff, do not write.
    #[arg(long, conflicts_with = "apply")]
    pub dry_run: bool,
    /// Rewrite the file in place.
    #[arg(long, conflicts_with = "dry_run")]
    pub apply: bool,
}

pub fn run(args: MigrateArgs) -> anyhow::Result<()> {
    let cfg_path = args.config
        .or_else(|| std::env::var_os("CORLINMAN_CONFIG").map(PathBuf::from))
        .ok_or_else(|| anyhow::anyhow!("specify --config or set CORLINMAN_CONFIG"))?;
    let original = fs::read_to_string(&cfg_path)?;

    let rewritten = rewrite_sub2api_to_newapi(&original);

    if rewritten == original {
        println!("no_sub2api_entries_found at {}", cfg_path.display());
        return Ok(());
    }

    print_diff(&original, &rewritten);
    if args.apply {
        let backup = cfg_path.with_extension("toml.sub2api.bak");
        fs::write(&backup, &original)?;
        fs::write(&cfg_path, &rewritten)?;
        println!("rewrote {} (backup: {})", cfg_path.display(), backup.display());
    } else if args.dry_run {
        println!("--dry-run: no changes written");
    } else {
        anyhow::bail!("pass --dry-run or --apply");
    }
    Ok(())
}

fn rewrite_sub2api_to_newapi(input: &str) -> String {
    // Line-level rewrite: in any `[providers.X]` block, replace
    // `kind = "sub2api"` with `kind = "newapi"`. Preserve whitespace
    // and surrounding lines verbatim. TOML structure stays valid.
    input
        .lines()
        .map(|line| {
            let trim = line.trim_start();
            if trim.starts_with("kind") && trim.contains("\"sub2api\"") {
                line.replacen("\"sub2api\"", "\"newapi\"", 1)
            } else {
                line.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + if input.ends_with('\n') { "\n" } else { "" }
}

fn print_diff(before: &str, after: &str) {
    for (b, a) in before.lines().zip(after.lines()) {
        if b != a {
            println!("- {}", b);
            println!("+ {}", a);
        } else {
            println!("  {}", a);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rewrite_changes_only_kind_lines() {
        let i = "[providers.x]\nkind = \"sub2api\"\nbase_url = \"http://x\"\n";
        let o = rewrite_sub2api_to_newapi(i);
        assert!(o.contains("kind = \"newapi\""));
        assert!(o.contains("http://x"));
    }
}
```

In `cmd/config.rs` add a variant to its Subcommand enum:

```rust
#[derive(Debug, Subcommand)]
pub enum Cmd {
    // ... existing variants ...
    /// Migrate legacy `kind = "sub2api"` entries to `kind = "newapi"`.
    MigrateSub2api(super::migrate::MigrateArgs),
}
```

In the `match` that dispatches `Cmd`, add `Cmd::MigrateSub2api(a) => super::migrate::run(a)`.

In `cmd/mod.rs`:
```rust
pub mod migrate;
```

- [ ] **Step 4: Run tests**

Run: `cargo test -p corlinman-cli --test migrate_test`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust/crates/corlinman-cli/
git commit -m "feat(cli): corlinman config migrate-sub2api with dry-run + apply"
```

---

## Phase 6 — Onboard UI 4-step wizard

### Task 19: Convert `ui/app/onboard/page.tsx` to step state machine + Step 1 (Account)

**Files:**
- Modify: `ui/app/onboard/page.tsx`
- Modify: `ui/app/onboard/page.test.tsx`
- Modify: `ui/lib/api.ts` (add new auth/onboard fetchers)
- Modify: `ui/lib/i18n/locales/{zh-CN,en}/onboard.json` (or wherever onboard strings live)

- [ ] **Step 1: Write failing test**

In `ui/app/onboard/page.test.tsx`, append:

```tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

it("starts on step 'account' and advances to 'newapi' after successful POST", async () => {
  // Mock fetch to capture the call path
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(
    JSON.stringify({ next: "newapi" }),
    { status: 200, headers: { "Set-Cookie": "onboard_session_id=abc" } }
  ));
  render(<OnboardPage />);
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "root" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "s3cret-strong" } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "s3cret-strong" } });
  fireEvent.click(screen.getByRole("button", { name: /下一步|继续/ }));
  await waitFor(() => {
    expect(screen.queryByLabelText("用户名")).not.toBeInTheDocument();
    expect(screen.getByText(/newapi 连接/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test (verify it fails)**

```bash
cd ui && pnpm test app/onboard/page.test.tsx
```
Expected: fails — current page doesn't advance to a `newapi` step.

- [ ] **Step 3: Refactor `page.tsx`**

Replace the single-form OnboardForm with a step state machine. Keep the wrapping layout intact:

```tsx
type OnboardStep = "account" | "newapi" | "models" | "confirm";

export default function OnboardPage() {
  const [step, setStep] = React.useState<OnboardStep>("account");
  return (
    <div className="relative grid min-h-dvh ...">
      {/* keep existing layout/Hero */}
      <div className="flex items-center justify-center p-8">
        <OnboardWizard step={step} setStep={setStep} />
      </div>
    </div>
  );
}

function OnboardWizard({ step, setStep }: { step: OnboardStep; setStep: (s: OnboardStep) => void }) {
  return (
    <div className="w-full max-w-md">
      <StepProgress current={step} />
      {step === "account" && <AccountStep onDone={() => setStep("newapi")} />}
      {step === "newapi" && <NewapiStep onDone={() => setStep("models")} onBack={() => setStep("account")} />}
      {step === "models" && <ModelsStep onDone={() => setStep("confirm")} onBack={() => setStep("newapi")} />}
      {step === "confirm" && <ConfirmStep onBack={() => setStep("models")} />}
    </div>
  );
}

function AccountStep({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  // ... move the existing 3-field form here; submit POST /admin/onboard/account
  // ... call onDone() on 200
}

// Placeholder stubs filled in by later tasks.
function NewapiStep({ onDone, onBack }: { onDone: () => void; onBack: () => void }) {
  const { t } = useTranslation();
  return <div>{t("onboard.newapi.title", "newapi 连接")} <button onClick={onBack}>{t("common.back")}</button> <button onClick={onDone}>下一步</button></div>;
}
function ModelsStep({ onDone, onBack }: any) { return <div>models <button onClick={onBack}>back</button> <button onClick={onDone}>next</button></div>; }
function ConfirmStep({ onBack }: any) { return <div>confirm <button onClick={onBack}>back</button></div>; }
```

`StepProgress` is a small visual indicator (4 dots / 4 numbered steps). Use existing layout primitives.

In `lib/api.ts`, add:
```ts
export async function onboardAccount(body: { username: string; password: string }): Promise<{ next: string }> {
  return apiFetch("/admin/onboard/account", { method: "POST", body });
}
```

Add i18n strings: `onboard.newapi.title = "newapi 连接"` (zh-CN) / `"newapi connection"` (en); `common.back`.

- [ ] **Step 4: Run test**

```bash
cd ui && pnpm test app/onboard/page.test.tsx
```
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add ui/
git commit -m "feat(ui/onboard): step state machine + Step 1 account form refactor"
```

---

### Task 20: Onboard Step 2 (newapi connect) (TDD)

**Files:**
- Modify: `ui/app/onboard/page.tsx`
- Modify: `ui/app/onboard/page.test.tsx`
- Modify: `ui/lib/api.ts`
- Modify: `ui/lib/i18n/locales/{zh-CN,en}/onboard.json`

- [ ] **Step 1: Write failing test**

```tsx
it("step 'newapi' POSTs to /admin/onboard/newapi and surfaces errors", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ next: "newapi" }), { status: 200 })) // account
    .mockResolvedValueOnce(new Response(JSON.stringify({ error: "newapi_unreachable" }), { status: 400 }));
  
  render(<OnboardPage />);
  await advanceFromAccountStep(); // helper that fills + submits step 1
  fireEvent.change(screen.getByLabelText(/newapi 地址|newapi URL/), { target: { value: "http://x" } });
  fireEvent.change(screen.getByLabelText(/用户令牌|user token/), { target: { value: "sk-x" } });
  fireEvent.click(screen.getByRole("button", { name: /下一步|继续/ }));
  await waitFor(() => {
    expect(screen.getByText(/无法连接|cannot reach/i)).toBeInTheDocument();
  });
});

it("step 'newapi' advances on 200", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ next: "newapi" }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ next: "models", channels_available: 3 }), { status: 200 }));
  render(<OnboardPage />);
  await advanceFromAccountStep();
  fireEvent.change(screen.getByLabelText(/newapi 地址|newapi URL/), { target: { value: "http://x" } });
  fireEvent.change(screen.getByLabelText(/用户令牌|user token/), { target: { value: "sk-x" } });
  fireEvent.click(screen.getByRole("button", { name: /下一步|继续/ }));
  await waitFor(() => {
    expect(screen.getByText(/选择默认|pick defaults/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test (verify it fails)**

Expected: fails — `NewapiStep` is the placeholder.

- [ ] **Step 3: Implement `NewapiStep`**

```tsx
function NewapiStep({ onDone, onBack }: { onDone: () => void; onBack: () => void }) {
  const { t } = useTranslation();
  const [baseUrl, setBaseUrl] = React.useState("");
  const [token, setToken] = React.useState("");
  const [adminToken, setAdminToken] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null); setSubmitting(true);
    try {
      await apiFetch("/admin/onboard/newapi", {
        method: "POST",
        body: { base_url: baseUrl, token, admin_token: adminToken || undefined }
      });
      onDone();
    } catch (e: any) {
      const code = e.message || "newapi_probe_failed";
      setError(t(`error.${code}`, code));
    } finally { setSubmitting(false); }
  }

  return (
    <form className="space-y-4" onSubmit={submit}>
      <h2 className="text-lg font-semibold">{t("onboard.newapi.title", "newapi 连接")}</h2>
      <p className="text-sm text-muted-foreground">{t("onboard.newapi.help")}</p>
      <div>
        <Label htmlFor="base_url">{t("onboard.newapi.base_url", "newapi 地址")}</Label>
        <Input id="base_url" type="url" required value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
      </div>
      <div>
        <Label htmlFor="token">{t("onboard.newapi.token", "用户令牌")}</Label>
        <Input id="token" type="password" required value={token} onChange={e => setToken(e.target.value)} />
      </div>
      <div>
        <Label htmlFor="admin_token">{t("onboard.newapi.admin_token", "系统访问令牌 (可选)")}</Label>
        <Input id="admin_token" type="password" value={adminToken} onChange={e => setAdminToken(e.target.value)} />
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div className="flex gap-2">
        <Button type="button" variant="outline" onClick={onBack}>{t("common.back", "上一步")}</Button>
        <Button type="submit" disabled={submitting}>{t("common.next", "下一步")}</Button>
      </div>
    </form>
  );
}
```

Add error i18n entries:
```json
{
  "error.newapi_unreachable": "无法连接到 newapi，请检查地址与防火墙",
  "error.newapi_token_invalid": "令牌无效，请到 newapi 后台 → 令牌 → 重新生成",
  "error.newapi_admin_required": "需要系统访问令牌，请到 newapi 后台 → 设置 → 系统访问令牌",
  "error.newapi_version_too_old": "newapi 版本过老，请升级到最新版本"
}
```

- [ ] **Step 4: Run tests**

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add ui/
git commit -m "feat(ui/onboard): Step 2 newapi connect form with error mapping"
```

---

### Task 21: Onboard Steps 3 + 4 (model picker + confirm) (TDD)

**Files:**
- Modify: `ui/app/onboard/page.tsx`
- Modify: `ui/app/onboard/page.test.tsx`
- Modify: `ui/lib/api.ts`
- Modify: i18n bundles

- [ ] **Step 1: Write failing test**

```tsx
it("step 'models' shows three pickers populated from newapi channels", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ next: "newapi" }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ next: "models", channels_available: 1 }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ channels: [
      { id: 1, name: "openai-prim", models: "gpt-4o-mini" }
    ]}), { status: 200 }))  // llm
    .mockResolvedValueOnce(new Response(JSON.stringify({ channels: [
      { id: 2, name: "emb", models: "text-embedding-3-small" }
    ]}), { status: 200 }))  // embedding
    .mockResolvedValueOnce(new Response(JSON.stringify({ channels: [
      { id: 3, name: "tts", models: "tts-1" }
    ]}), { status: 200 }))  // tts
    .mockResolvedValueOnce(new Response(JSON.stringify({ next: "confirm", preview: {} }), { status: 200 })); // select
  render(<OnboardPage />);
  await advanceFromAccountStep();
  await advanceFromNewapiStep();
  await waitFor(() => expect(screen.getByText(/gpt-4o-mini/)).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /下一步|继续/ }));
  await waitFor(() => expect(screen.getByText(/确认|confirm/i)).toBeInTheDocument());
});

it("step 'confirm' POSTs finalize then navigates to /login", async () => {
  // similar setup; mock finalize OK; assert navigation
});
```

- [ ] **Step 2: Run tests (verify they fail)**

Expected: ModelsStep / ConfirmStep are placeholders.

- [ ] **Step 3: Implement `ModelsStep`**

```tsx
type ChannelSummary = { id: number; name: string; models: string };

function ModelsStep({ onDone, onBack }: { onDone: () => void; onBack: () => void }) {
  const { t } = useTranslation();
  const llmQ = useQuery({ queryKey: ["onboard","channels","llm"], queryFn: () => apiFetch<{channels: ChannelSummary[]}>("/admin/onboard/newapi/channels?type=llm") });
  const embQ = useQuery({ queryKey: ["onboard","channels","embedding"], queryFn: () => apiFetch<{channels: ChannelSummary[]}>("/admin/onboard/newapi/channels?type=embedding") });
  const ttsQ = useQuery({ queryKey: ["onboard","channels","tts"], queryFn: () => apiFetch<{channels: ChannelSummary[]}>("/admin/onboard/newapi/channels?type=tts") });

  const [llmPick, setLlmPick] = React.useState<{channel_id: number; model: string} | null>(null);
  const [embPick, setEmbPick] = React.useState<{channel_id: number; model: string} | null>(null);
  const [ttsPick, setTtsPick] = React.useState<{channel_id: number; model: string; voice?: string} | null>(null);

  React.useEffect(() => {
    if (!llmPick && llmQ.data?.channels[0]) {
      const ch = llmQ.data.channels[0];
      const model = ch.models.split(",")[0].trim();
      setLlmPick({ channel_id: ch.id, model });
    }
    // same defaults for emb, tts ...
  }, [llmQ.data, embQ.data, ttsQ.data]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!llmPick || !embPick || !ttsPick) return;
    await apiFetch("/admin/onboard/newapi/select", { method: "POST", body: { llm: llmPick, embedding: embPick, tts: ttsPick } });
    onDone();
  }

  if (llmQ.isLoading || embQ.isLoading || ttsQ.isLoading) return <Skeleton />;

  return (
    <form onSubmit={submit} className="space-y-6">
      <ChannelPicker label={t("onboard.models.llm")} channels={llmQ.data?.channels ?? []} value={llmPick} onChange={setLlmPick} />
      <ChannelPicker label={t("onboard.models.embedding")} channels={embQ.data?.channels ?? []} value={embPick} onChange={setEmbPick} />
      <ChannelPicker label={t("onboard.models.tts")} channels={ttsQ.data?.channels ?? []} value={ttsPick} onChange={(p) => setTtsPick(p ? { ...p, voice: "alloy" } : null)} />
      <div className="flex gap-2">
        <Button type="button" variant="outline" onClick={onBack}>{t("common.back")}</Button>
        <Button type="submit">{t("common.next")}</Button>
      </div>
    </form>
  );
}

function ChannelPicker({ label, channels, value, onChange }: {
  label: string;
  channels: ChannelSummary[];
  value: { channel_id: number; model: string } | null;
  onChange: (v: { channel_id: number; model: string } | null) => void;
}) {
  // simple select: channel name → model dropdown ...
  // Keep it under 30 lines: two <select>s nested.
}
```

- [ ] **Step 4: Implement `ConfirmStep`**

```tsx
function ConfirmStep({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const router = useRouter();
  const [error, setError] = React.useState<string | null>(null);

  async function confirm() {
    try {
      await apiFetch("/admin/onboard/finalize", { method: "POST" });
      router.push("/login");
    } catch (e: any) {
      setError(t(`error.${e.message}`, e.message));
    }
  }

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">{t("onboard.confirm.title", "确认 & 完成")}</h2>
      <p>{t("onboard.confirm.body")}</p>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div className="flex gap-2">
        <Button variant="outline" onClick={onBack}>{t("common.back")}</Button>
        <Button onClick={confirm}>{t("onboard.confirm.finish", "完成")}</Button>
      </div>
    </div>
  );
}
```

Add i18n strings for `onboard.models.{llm,embedding,tts}`, `onboard.confirm.{title,body,finish}`.

- [ ] **Step 5: Run tests**

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add ui/
git commit -m "feat(ui/onboard): Step 3 model picker + Step 4 confirm + finalize"
```

---

## Phase 7 — /admin/newapi UI page + integrations

### Task 22: `/admin/newapi` UI page (TDD)

**Files:**
- Create: `ui/app/(admin)/newapi/page.tsx`
- Create: `ui/app/(admin)/newapi/page.test.tsx`
- Modify: `ui/lib/api.ts` (add `fetchNewapi`, `fetchNewapiChannels`, `testNewapi`, `patchNewapi`)
- Modify: `ui/components/layout/nav.tsx` and/or `sidebar.tsx`
- Modify: `ui/lib/i18n/locales/{zh-CN,en}/admin.json`

- [ ] **Step 1: Write failing test**

```tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import NewapiPage from "./page";

it("renders connection card + channel table + test button", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({
      connection: { base_url: "http://x", token_masked: "sk-x...y", admin_key_present: true },
      status: "ok"
    }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      channels: [{ id: 1, name: "openai-prim", type: 1, status: 1, models: "gpt-4o" }]
    }), { status: 200 }));
  render(<NewapiPage />);
  await waitFor(() => expect(screen.getByText("http://x")).toBeInTheDocument());
  expect(screen.getByText(/openai-prim/)).toBeInTheDocument();
});

it("test button shows latency on success", async () => {
  // mock 1 fetch for summary, 1 for channels, 1 for test
  // assert "120 ms" or similar appears
});

it("shows backend-pending banner when summary returns 503", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("", { status: 503 }));
  render(<NewapiPage />);
  await waitFor(() => expect(screen.getByText(/未配置 newapi|no newapi/i)).toBeInTheDocument());
});
```

- [ ] **Step 2: Run test (verify fail)**

Expected: page module not found.

- [ ] **Step 3: Implement the page**

```tsx
"use client";
import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { apiFetch } from "@/lib/api";

type Summary = {
  connection: { base_url: string; token_masked: string; admin_key_present: boolean };
  status: "ok" | "degraded";
};
type Channel = { id: number; name: string; type: number; status: number; models: string };

export default function NewapiPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const summary = useQuery<Summary>({
    queryKey: ["admin","newapi"],
    queryFn: () => apiFetch("/admin/newapi"),
    retry: false,
  });
  const channels = useQuery<{ channels: Channel[] }>({
    queryKey: ["admin","newapi","channels","llm"],
    queryFn: () => apiFetch("/admin/newapi/channels?type=llm"),
    enabled: !!summary.data,
  });
  const testMut = useMutation({
    mutationFn: (body: { model: string }) => apiFetch<{ status: number; latency_ms: number }>("/admin/newapi/test", { method: "POST", body }),
    onSuccess: (r) => toast.success(`${r.latency_ms} ms (HTTP ${r.status})`),
    onError: (e: any) => toast.error(e.message ?? "newapi_test_failed"),
  });

  if (summary.error || (summary.data == null && !summary.isLoading)) {
    return <BackendPendingBanner />;
  }
  if (summary.isLoading) return <Skeleton className="w-full h-32" />;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">{t("admin.newapi.title", "newapi 连接")}</h1>
      </header>
      <section className="rounded-lg border p-4">
        <h2 className="font-medium mb-2">{t("admin.newapi.connection", "连接信息")}</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt>{t("admin.newapi.base_url")}</dt><dd>{summary.data!.connection.base_url}</dd>
          <dt>{t("admin.newapi.token")}</dt><dd className="font-mono">{summary.data!.connection.token_masked}</dd>
          <dt>{t("admin.newapi.admin_key")}</dt><dd>{summary.data!.connection.admin_key_present ? t("common.yes") : t("common.no")}</dd>
        </dl>
        <Button className="mt-3" onClick={() => testMut.mutate({ model: "gpt-4o-mini" })}>{t("admin.newapi.test_button", "测试连接")}</Button>
      </section>
      <section className="rounded-lg border p-4">
        <h2 className="font-medium mb-2">{t("admin.newapi.channels", "频道列表")}</h2>
        <table className="w-full text-sm">
          <thead><tr><th>ID</th><th>{t("admin.newapi.channel.name")}</th><th>{t("admin.newapi.channel.models")}</th><th>{t("admin.newapi.channel.status")}</th></tr></thead>
          <tbody>
            {channels.data?.channels.map(c => (
              <tr key={c.id}><td>{c.id}</td><td>{c.name}</td><td className="font-mono text-xs">{c.models}</td><td>{c.status === 1 ? "✓" : "✗"}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function BackendPendingBanner() {
  const { t } = useTranslation();
  return <div className="rounded-lg border p-4 bg-muted">{t("admin.newapi.not_configured", "未配置 newapi。请到 onboard 向导添加。")}</div>;
}
```

Add nav entry in `ui/components/layout/sidebar.tsx` (or `nav.tsx` — match the project's existing nav schema) under the existing admin sections (place after `providers`):
```tsx
{ href: "/newapi", labelKey: "admin.nav.newapi", icon: PlugIcon },
```

Add i18n strings.

- [ ] **Step 4: Run tests**

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add ui/
git commit -m "feat(ui/admin): /newapi connector page + nav entry"
```

---

### Task 23: "Pull from newapi" buttons on Embedding + Models pages (TDD)

**Files:**
- Modify: `ui/app/(admin)/embedding/page.tsx`
- Modify: `ui/app/(admin)/models/page.tsx`
- Add tests to their respective `*.test.tsx`

- [ ] **Step 1: Write failing test (embedding)**

```tsx
it("'pull from newapi' button populates model dropdown from channel list", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ embedding: { provider: "newapi", model: "", dimension: 0, enabled: false }}), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ providers: [{ name: "newapi", kind: "newapi", enabled: true }] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ channels: [
      { id: 1, name: "siliconflow-emb", models: "BAAI/bge-large-zh-v1.5" }
    ]}), { status: 200 }));
  render(<EmbeddingPage />);
  await waitFor(() => screen.getByRole("button", { name: /从 newapi 拉取|pull from newapi/i }));
  fireEvent.click(screen.getByRole("button", { name: /从 newapi 拉取/i }));
  await waitFor(() => expect(screen.getByText("BAAI/bge-large-zh-v1.5")).toBeInTheDocument());
});
```

- [ ] **Step 2: Run test (verify fail)**

Expected: button doesn't exist.

- [ ] **Step 3: Add button**

In `embedding/page.tsx`, near the provider dropdown, add:

```tsx
const pullMut = useMutation({
  mutationFn: () => apiFetch<{ channels: Channel[] }>("/admin/newapi/channels?type=embedding"),
  onSuccess: (r) => setNewapiChannels(r.channels),
});

{providers.data?.some(p => p.kind === "newapi" && p.enabled) && (
  <Button type="button" variant="outline" onClick={() => pullMut.mutate()}>
    {t("admin.embedding.pull_from_newapi", "从 newapi 拉取")}
  </Button>
)}

{newapiChannels && (
  <ChannelPickerDropdown channels={newapiChannels} onPick={(ch, model) => {
    setDraft(d => d && { ...d, provider: "newapi", model });
  }} />
)}
```

Do the equivalent in `models/page.tsx` for the LLM model selection.

- [ ] **Step 4: Run tests**

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add ui/
git commit -m "feat(ui/admin): pull-from-newapi buttons on embedding + models pages"
```

---

### Task 24: i18n batch + screenshot regression

**Files:**
- Modify: all `ui/lib/i18n/locales/{zh-CN,en}/*.json` files used in newapi work
- Run UI build

- [ ] **Step 1: Audit translation completeness**

```bash
cd ui && grep -rn 'admin\.newapi\.\|onboard\.newapi\.\|error\.newapi_' lib/i18n/locales/zh-CN/
cd ui && grep -rn 'admin\.newapi\.\|onboard\.newapi\.\|error\.newapi_' lib/i18n/locales/en/
```

Both languages must have entries for every key referenced in code. If en is missing keys, add English versions matching the zh-CN keys.

- [ ] **Step 2: Manual build + smoke**

```bash
cd ui && pnpm build
```
Expected: build passes with no `t() returned key` warnings.

- [ ] **Step 3: Run full UI test suite**

```bash
cd ui && pnpm test
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add ui/lib/i18n
git commit -m "i18n(newapi): full zh-CN + en coverage for onboard + admin pages"
```

---

## Phase 8 — Docs / CHANGELOG / E2E

### Task 25: Migration guide + CREDITS + providers.md + e2e + design doc

**Files:**
- Create: `docs/migration/sub2api-to-newapi.md`
- Create: `docs/design/newapi-integration.md`
- Delete: `docs/design/sub2api-integration.md`
- Modify: `docs/providers.md`
- Modify: `CREDITS.md`
- Create: `scripts/e2e/newapi-flow.sh`
- Create: `docker/compose/newapi.yml`

- [ ] **Step 1: Write migration guide**

`docs/migration/sub2api-to-newapi.md`:

```markdown
# Migrating from sub2api to newapi

`corlinman` 0.x+ replaces `ProviderKind::Sub2api` with `ProviderKind::Newapi`
backed by [QuantumNous/new-api](https://github.com/QuantumNous/new-api).

## Why

- MIT licence (sub2api was LGPL-3.0)
- Native Audio TTS endpoint (sub2api didn't expose `/v1/audio/speech`)
- Active maintenance / actively updated Chinese-community fork
- Already-shipped admin API surface (`/api/channel`, `/api/user/self`, `/api/status`)

## 5-step migration

1. **Backup your current config and identity store.**

   ```bash
   cp ~/.corlinman/config.toml ~/.corlinman/config.toml.pre-newapi.bak
   ```

2. **Stand up new-api on the same host (or a neighbour).**

   ```bash
   docker run -d --name newapi -p 3000:3000 \
       -v "$PWD/newapi-data:/data" \
       calciumion/new-api:latest
   ```

   Open `http://localhost:3000`, create a root user, add OAuth subscriptions /
   API-key channels (the same ones you had configured in sub2api), and mint
   one *user token* + one *system access token*.

3. **Preview the migration of your corlinman config.**

   ```bash
   corlinman config migrate-sub2api --dry-run
   ```

   Inspect the diff. All `kind = "sub2api"` lines turn into `kind = "newapi"`;
   nothing else changes.

4. **Apply the rewrite.**

   ```bash
   corlinman config migrate-sub2api --apply
   ```

   The CLI writes a backup at `config.toml.sub2api.bak` and updates the
   original file in place.

5. **Restart corlinman; open `/admin/newapi`.**

   ```bash
   systemctl restart corlinman   # or your equivalent
   ```

   Open the admin UI → newapi → fill the connection card with the URL +
   tokens from step 2. The page lists channels with health / quota.

## Caveats

- `migrate-sub2api` cannot move subscription / channel state from sub2api
  to newapi — they have different schemas. Recreate channels in new-api's
  own console.
- If you ran sub2api as a sidecar container, you can keep its container
  around as long as you want; corlinman simply no longer talks to it.

## Reverting

Restore `~/.corlinman/config.toml.pre-newapi.bak` and downgrade the corlinman
binary to a pre-newapi release.
```

- [ ] **Step 2: Write the public design doc**

`docs/design/newapi-integration.md`: a concise (≤2 pages) operator-facing version of the spec. Cover what newapi is, how corlinman talks to it, how to deploy, where to look in the admin UI. Cross-link to `docs/superpowers/specs/2026-05-13-newapi-integration-design.md` for full details.

- [ ] **Step 3: Update CREDITS.md**

Remove the entire `### Wei-Shaw/sub2api` block. Append:

```markdown
### QuantumNous/new-api

We integrate [QuantumNous/new-api](https://github.com/QuantumNous/new-api)
as a sidecar process. corlinman registers a `ProviderKind::Newapi` that
dials new-api over HTTP — new-api itself is not vendored, linked, or
otherwise combined with this binary. **License: MIT.** corlinman includes
a thin HTTP admin client (`corlinman-newapi-client`) for channel discovery
and health checks; the wire format is plain OpenAI-compat. See
`docs/design/newapi-integration.md` for the architecture.
```

- [ ] **Step 4: Update docs/providers.md**

Find the `| sub2api |` table row and replace with:

```markdown
| `newapi`            | **none — `base_url` REQUIRED**                      | Bearer token           | Yes       | OpenAI-wire sidecar for channel pooling ([QuantumNous/new-api](https://github.com/QuantumNous/new-api), MIT). Supports chat, embedding, and audio TTS via the underlying channels. See `docs/design/newapi-integration.md`. |
```

Remove any LGPL warning paragraph specific to sub2api.

- [ ] **Step 5: Delete obsolete doc**

```bash
git rm docs/design/sub2api-integration.md
```

- [ ] **Step 6: Write e2e script**

`scripts/e2e/newapi-flow.sh`:

```bash
#!/usr/bin/env bash
# E2E: bring up newapi + corlinman, exercise chat + embedding + tts.

set -euo pipefail
cd "$(dirname "$0")/../.."

docker compose -f docker/compose/newapi.yml up -d
trap 'docker compose -f docker/compose/newapi.yml down' EXIT

# Wait for newapi to be ready.
until curl -fsS http://localhost:3000/api/status >/dev/null; do
    sleep 1
done

# Spawn corlinman gateway under test config pointing at newapi.
export CORLINMAN_CONFIG=$(mktemp)
cat > "$CORLINMAN_CONFIG" <<EOF
[admin]
username = "root"
password_hash = "..."  # bootstrap

[providers.newapi]
kind = "newapi"
base_url = "http://localhost:3000"
api_key = { value = "sk-e2e" }
enabled = true

[models]
default = "gpt-4o-mini"
[models.aliases.gpt-4o-mini]
model = "gpt-4o-mini"
provider = "newapi"

[embedding]
enabled = true
provider = "newapi"
model = "text-embedding-3-small"
dimension = 1536

[voice]
enabled = true
provider_alias = "newapi"
tts_model = "tts-1"
sample_rate_hz_out = 24000
EOF

cargo run --release -p corlinman-gateway -- start &
GW=$!
trap 'kill $GW; docker compose -f docker/compose/newapi.yml down' EXIT
sleep 5

# Chat
curl -fsS http://localhost:6005/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}' \
    | jq -e '.choices[0].message.content' >/dev/null
echo "chat OK"

# Embedding
curl -fsS http://localhost:6005/v1/embeddings \
    -H "Content-Type: application/json" \
    -d '{"model":"text-embedding-3-small","input":"hello"}' \
    | jq -e '.data[0].embedding | length' >/dev/null
echo "embedding OK"

# TTS
curl -fsS http://localhost:6005/v1/audio/speech \
    -H "Content-Type: application/json" \
    -d '{"model":"tts-1","input":"hello","voice":"alloy"}' \
    -o /tmp/tts.mp3
test -s /tmp/tts.mp3
echo "tts OK"

echo "all green"
```

Make executable: `chmod +x scripts/e2e/newapi-flow.sh`.

- [ ] **Step 7: Write `docker/compose/newapi.yml`**

```yaml
services:
  newapi:
    image: calciumion/new-api:latest
    container_name: corlinman-newapi
    ports:
      - "127.0.0.1:3000:3000"
    volumes:
      - ./newapi-data:/data
    environment:
      - SQL_DSN=sqlite:///data/newapi.db
    restart: unless-stopped
```

- [ ] **Step 8: Run full test suite**

```bash
cargo test --workspace
cd python/packages/corlinman-providers && uv run pytest
cd ../../../ui && pnpm test
```
Expected: all pass.

- [ ] **Step 9: Commit + push branch**

```bash
git add docs/ scripts/e2e/newapi-flow.sh docker/compose/newapi.yml
git rm docs/design/sub2api-integration.md
git commit -m "docs(newapi): migration guide, CREDITS update, e2e script, compose example"
git push origin feat/newapi-integration
```

- [ ] **Step 10: Open PR for review**

```bash
gh pr create --base main --head feat/newapi-integration \
    --title "feat: replace sub2api with QuantumNous/new-api (BREAKING)" \
    --body "$(cat <<'EOF'
## Summary

- Hard-removes `ProviderKind::Sub2api`; adds `ProviderKind::Newapi` backed by [QuantumNous/new-api](https://github.com/QuantumNous/new-api).
- 4-step onboard wizard: account → newapi connect → pick defaults → confirm.
- `/admin/newapi` connector page with channel health table + 1-token round-trip test button.
- `corlinman config migrate-sub2api [--dry-run|--apply]` CLI for legacy configs.
- Full zh-CN + en i18n coverage.

## Breaking changes

- `kind = "sub2api"` is no longer recognized. Run the migrate CLI or hand-edit your config.

## Test plan

- [ ] `cargo test --workspace` green
- [ ] `cd ui && pnpm test` green
- [ ] `cd python/packages/corlinman-providers && uv run pytest` green
- [ ] `scripts/e2e/newapi-flow.sh` green against a fresh new-api instance

## Docs

- `docs/migration/sub2api-to-newapi.md` — 5-step migration
- `docs/design/newapi-integration.md` — public design
- `docs/superpowers/specs/2026-05-13-newapi-integration-design.md` — full spec
EOF
)"
```

---

## Wrap-up

After PR merge, follow up with:

- **C subproject** — regression test sweep for tool calls and the evolution system (`corlinman-evolution`, `corlinman-shadow-tester`, `corlinman-auto-rollback`)
- **D subproject** — Rust build acceleration + pre-compiled binary release pipeline
- **E subproject** — README rewrite + CHANGELOG release notes + git tag + GitHub Release
- **F subproject** — rsync prebuilt artefacts to production server + restart systemd/docker

---

## Self-Review Notes

**Spec coverage matrix** (cross-checked against `docs/superpowers/specs/2026-05-13-newapi-integration-design.md`):

| Spec §  | Task(s) | Notes |
|---|---|---|
| §1 Goal — replace Sub2api with Newapi | 2, 3 | ✓ |
| §3 Architecture — newapi-client crate as admin-only client | 4–7 | ✓ |
| §4.1 Rust core rename | 2 | ✓ |
| §4.1 admin/providers.rs migration | 3 | ✓ |
| §4.1 newapi-client crate | 4–7 | ✓ |
| §4.1 /admin/newapi 5 routes | 8–12 | ✓ |
| §4.1 onboard 4-step refactor | 13–17 | ✓ |
| §4.1 migrate-sub2api CLI | 18 | ✓ |
| §4.2 Python rename | 3 | ✓ (test_newapi.py added in §4.2's test_newapi.py creation — covered by step 5 of Task 3 by deleting sub2api test; new newapi test added below) |
| §4.3 UI onboard wizard | 19, 20, 21 | ✓ |
| §4.3 /admin/newapi page | 22 | ✓ |
| §4.3 embedding/models "pull from newapi" buttons | 23 | ✓ |
| §4.3 nav entry | 22 | ✓ |
| §4.3 ui/lib/api.ts | 19, 20, 21, 22, 23 | ✓ (each task adds the fetchers it needs) |
| §4.4 docs/design/sub2api-integration.md delete | 25 | ✓ |
| §4.4 docs/design/newapi-integration.md create | 25 | ✓ |
| §4.4 docs/providers.md row swap | 25 | ✓ |
| §4.4 docs/migration/sub2api-to-newapi.md create | 25 | ✓ |
| §4.4 CREDITS.md update | 25 | ✓ |
| §4.4 CHANGELOG BREAKING entry | 1 (stub), 25 (final wording at PR time) | ✓ |
| §4.5 i18n coverage | 24 | ✓ |
| §5.1 Onboard 4-step data flow | 13–17 + 19–21 | ✓ |
| §5.3 /admin/newapi GET/POST/PATCH | 8, 9, 10, 11, 12 | ✓ |
| §6 Error handling matrix | 9 (map_probe_error), 14 (409 admin already configured), 15 (probe errors), 17 (re-probe + 503) | ✓ |
| §7 Testing | every task has TDD steps + Task 8 (admin_newapi integ), Task 14–17 (admin_onboard integ), Task 25 (e2e) | ✓ |
| §8 Migration guide | 25 | ✓ |

**Placeholder scan**: Task 16 has a `todo!()` in the test helper `setup_onboarded_through_newapi()` — implementer must extract from Tasks 14/15 test bodies. This is a known small fill-in; flag as "expected manual completion during execution" rather than placeholder ambiguity. Other steps all contain full code or full instructions.

**Type consistency**: `NewapiClient::new(base_url, user_token, admin_token)` signature used consistently across Tasks 5/6/7 (crate) and Tasks 9/10/11/15/16/17 (consumers). `ChannelType::{Llm,Embedding,Tts}` consistent everywhere. Route names `/admin/newapi/{probe,channels,test}` consistent with their handlers.

**Python `test_newapi.py`**: spec §7 lists `python/packages/corlinman-providers/tests/test_newapi.py` but the plan above only deletes `test_sub2api.py`. **Gap.** Adding inline:

### Task 3.5 (Inline addition): Add `test_newapi.py`

Insert between Task 3 and Task 4:

- Create: `python/packages/corlinman-providers/tests/test_newapi.py`

```python
"""Tests for ProviderKind.NEWAPI dispatch + audio TTS routing."""
import pytest
from corlinman_providers.registry import build_provider
from corlinman_providers.specs import ProviderKind

def test_newapi_dispatches_to_openai_compat(monkeypatch):
    p = build_provider(name="newapi", kind=ProviderKind.NEWAPI,
                       base_url="http://localhost:3000", api_key="sk-x",
                       enabled=True, params={})
    assert type(p).__name__ == "OpenAICompatibleProvider"
    assert p.base_url == "http://localhost:3000"

@pytest.mark.asyncio
async def test_newapi_audio_speech_routes_to_v1_audio(monkeypatch, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="http://localhost:3000/v1/audio/speech",
        content=b"fake-mp3-bytes",
    )
    p = build_provider(name="newapi", kind=ProviderKind.NEWAPI,
                       base_url="http://localhost:3000", api_key="sk-x",
                       enabled=True, params={})
    audio = await p.audio_speech(model="tts-1", voice="alloy", input="hi")
    assert audio == b"fake-mp3-bytes"
```

```bash
git add python/packages/corlinman-providers/tests/test_newapi.py
git commit -m "test(providers): newapi dispatch + audio TTS round-trip"
```

(Inserts into Phase 1 between Tasks 3 and 4; renumbering downstream is not necessary since each task is self-contained.)
