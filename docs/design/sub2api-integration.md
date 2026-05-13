# sub2api integration — upstream provider management

Status: design + minimal first slice (Phase 3 stub).
Author: Backend Architect agent.
Date: 2026-05-12.

## 1. Goal

Stitch in a dedicated "subscription → OpenAI-Chat-Completion" upstream
management layer so operators can pool consumer subscriptions (Claude
Pro, ChatGPT Plus, Gemini Advanced, Antigravity) behind one OpenAI-wire
endpoint, and corlinman treats that endpoint as just another provider
in the existing `[providers.*]` registry.

Non-goal: rewrite corlinman's existing free-form provider model — it
already supports arbitrary OpenAI-compatible endpoints via
`kind = "openai_compatible"`. We add a *named* kind so the admin UI can
surface sub2api-specific knobs (channel health, subscription token
expiry, per-account rate budget) and so operators see "sub2api" instead
of an undifferentiated "openai_compatible" entry.

## 2. Research summary

| Candidate                        | Lang  | License    | Stars | Last push   | OAuth subscriptions    | OpenAI-compat | Verdict        |
| -------------------------------- | ----- | ---------- | ----- | ----------- | ---------------------- | ------------- | -------------- |
| Wei-Shaw/sub2api                 | Go    | LGPL-3.0   | 20.2k | 2026-05-12  | yes (Claude/CGPT/Gem.) | yes           | **picked**     |
| songquanpeng/one-api             | Go+JS | MIT        | 33.6k | 2026-01-09  | no (API-key only)      | yes           | runner-up      |
| MartialBE/one-hub                | Go+JS | MIT        | —     | —           | no                     | yes           | not eval'd     |
| AmazingAng/auth2api              | —     | —          | —     | —           | yes (Claude only)      | yes           | too narrow     |
| router-for-me/CLIProxyAPI        | —     | —          | —     | —           | partial                | yes           | too narrow     |

### 2.1 Why sub2api wins on fit

The user request is literally the sub2api thesis: "把订阅式上游 URL 统一
成 OpenAI-Chat-Completion 网关 + 一个 admin UI". sub2api has:

- `POST /v1/chat/completions` (OpenAI wire) — directly compatible with
  corlinman's `openai_compatible` adapter.
- `POST /v1/messages` (Anthropic wire) and `POST /v1beta/models/*`
  (Gemini wire) — useful later if we want to bypass corlinman's
  OpenAI-shape translation for Claude/Gemini.
- OAuth subscription pooling for Claude / ChatGPT / Gemini / Antigravity
  — the *whole reason* a thing called "sub2api" exists.
- Channel groups, per-channel TLS fingerprinting, billing, circuit
  breakers, failover — all the operator concerns we'd otherwise build.
- An Ent + Postgres schema (`Account`, `AccountGroup`, `ApiKey`,
  `AuthIdentity`, `AuthIdentityChannel`, etc.) and a Vue admin
  dashboard. Porting this to Rust is many engineer-weeks; consuming it
  over HTTP is one afternoon.

### 2.2 Why one-api is the runner-up (and our fallback)

one-api has the friendlier MIT license and is more mature/stable
(less churn — last push Jan 2026 vs. sub2api's daily commits). But it
**does not pool consumer subscriptions** — it manages vendor API keys.
If license posture forbids LGPL-3.0 even at HTTP-boundary distance,
swap sub2api for one-api and accept the loss of OAuth subscription
support.

### 2.3 License posture: LGPL-3.0 over HTTP

LGPL-3.0 attaches obligations to *linking*. Running sub2api as a
separate process behind an HTTP boundary is not linking — it's the
classic "service across a network" exemption. corlinman's source stays
under its own license. We must:

1. Not vendor sub2api's Go source into our tree.
2. Ship sub2api as a separate container/binary; the operator pulls or
   builds it themselves.
3. Mention sub2api in `CREDITS.md` with the link + LGPL-3.0 notice.

Vendoring as a git submodule or copy-pasting the Go code into our repo
would trigger combined-work questions — we are explicitly not doing
that.

## 3. 缝合 vs 嵌入 vs 引用 — the choice

| Option              | Effort         | License risk     | Operability    | Decision |
| ------------------- | -------------- | ---------------- | -------------- | -------- |
| Port logic to Rust  | 6-10 weeks     | none             | best           | rejected |
| Vendor as submodule | 1-2 weeks      | LGPL combined-work concern | medium | rejected |
| Embed as crate      | n/a (Go)       | n/a              | n/a            | n/a      |
| **Sidecar binary**  | **2-3 days**   | **clean**        | good           | **picked** |
| Sidecar container   | 2-3 days       | clean            | good           | (same as above, default deployment) |

corlinman runs sub2api as a **sidecar process** (the operator runs
`docker run weishaw/sub2api:latest` or the bare binary on the same
host) and points one corlinman provider entry at it.

## 4. Wire & data-model mapping

### 4.1 corlinman provider entry — new `kind = "sub2api"`

```toml
[providers.subhub]
kind     = "sub2api"
base_url = "http://127.0.0.1:7980"     # sub2api gateway
api_key  = { env = "SUB2API_KEY" }      # sub2api-issued sk-... token
enabled  = true

[providers.subhub.params]
# Optional, sub2api-specific advisory metadata. Not interpreted by the
# Rust adapter (which just speaks OpenAI wire). Surfaced in /admin/upstream.
sub2api_admin_url  = "http://127.0.0.1:7980/api/v1/admin"
sub2api_admin_key  = { env = "SUB2API_ADMIN_KEY" }
# group_id pins this corlinman provider to one sub2api account group, so
# /v1/chat/completions traffic stays inside that subscription pool.
group_id           = "claude-pro-pool"
```

At chat-time the Rust side does **nothing special**: it dispatches via
the existing `OpenAICompatibleProvider` Python adapter using
`base_url + "/v1/chat/completions"` and the bearer token. The `sub2api`
kind is a UI/policy hint, not a wire-shape divergence.

### 4.2 Schema bridge

| sub2api entity          | corlinman concept                                  |
| ----------------------- | -------------------------------------------------- |
| `Account` (OAuth login) | n/a — owned by sub2api operator                    |
| `AccountGroup`          | `providers.<name>.params.group_id`                 |
| `ApiKey` (sk-...)       | `providers.<name>.api_key` (one per provider entry)|
| `Channel` (model alias) | `[models.aliases.<name>]` pointing at this provider|
| `Channel.platform`      | informational only — wire is OpenAI either way     |

This is a deliberately *thin* mapping. corlinman doesn't try to mirror
sub2api state into its own DB; it just trusts the bearer token and
treats sub2api as an opaque OpenAI-compatible endpoint.

### 4.3 What stays separate

- `[providers.openai]`, `[providers.anthropic]`, `[providers.google]`
  etc. remain first-class. Operators choose per-alias whether the model
  goes direct or via sub2api. No forced migration.
- `[embedding]` ignores sub2api by default — embeddings should keep
  going direct to a paid OpenAI / SiliconFlow / Ollama key. sub2api's
  pooled subscriptions don't typically expose embedding endpoints.

## 5. Admin UI — `/admin/upstream`

A new page on the existing admin UI (sits next to `/admin/providers`):

- Lists every `kind = "sub2api"` provider entry.
- For each, calls the sub2api admin API (`GET /api/v1/admin/channels`
  with `sub2api_admin_key`) to surface:
  - channel id, platform (claude / openai / gemini), enabled state
  - last-seen rate-limit / cooldown
  - subscription token expiry (Claude OAuth refresh token TTL)
  - per-channel usage today vs. budget
- "Test" button: corlinman issues `POST {base_url}/v1/chat/completions`
  with a fixed 1-token prompt against this provider entry and shows
  round-trip latency + status.

For Phase 3 we ship **only** the corlinman-side provider kind and the
"test" round-trip. The full health-panel page is Phase 4.

## 6. Migration

Existing operators don't need to change anything. To opt in:

1. `docker run -d -p 7980:7980 weishaw/sub2api:latest` (or run the
   binary; sub2api ships as one Go binary + Postgres).
2. Through sub2api's own dashboard, add their Claude / ChatGPT / Gemini
   OAuth accounts and mint an API key.
3. Add `[providers.subhub] kind = "sub2api" base_url = "http://..."`
   to `config.toml`.
4. Point one or more `[models.aliases.*]` at `provider = "subhub"`.

No data migration. The `[providers.openai]`-style entries operators
have today keep working unchanged.

## 7. Effort estimate

| Slice                                          | Effort     | Status      |
| ---------------------------------------------- | ---------- | ----------- |
| Add `ProviderKind::Sub2api` + schema test      | 0.5 day    | done (P3)   |
| Wire `Sub2api` through `OpenAICompatibleProvider` mapping | 0.5 day | done (P3, Rust-side only)  |
| Integration test: chat round-trip via mock     | 0.5 day    | done (P3)   |
| `/admin/upstream` Rust handler (list + test)   | 1 day      | deferred    |
| Admin UI Vue page                              | 1.5 days   | deferred    |
| sub2api admin-API client crate (channel list, health) | 1 day | deferred    |
| Docs page + CREDITS update                     | 0.5 day    | done (P3)   |
| Deploy compose / docker example                | 0.5 day    | deferred    |

Phase 3 ships ~1.5 days of work (the Rust slice + tests + docs).
Phases 4-5 finish the admin UI and ops glue.

## 8. Open issues for follow-up

1. **License notice**: add a `CREDITS.md` block mentioning Wei-Shaw/sub2api
   under LGPL-3.0 once we ship the docker-compose example that pulls
   their image. Phase 3 doesn't ship that compose file yet, so the
   credit is deferred.
2. **sub2api admin API stability**: the admin endpoints under
   `/api/v1/admin/...` aren't documented in sub2api's public README;
   we'd be reverse-engineering them for the `/admin/upstream` page.
   Risk: breakage on sub2api upgrades. Mitigation: pin sub2api version
   in compose, ship our own thin client crate with a feature flag.
3. **Health/circuit-breaking**: corlinman currently treats provider
   errors as fatal-per-request. sub2api itself does failover across
   channels, so this is OK at the boundary — but if multiple
   `kind = "sub2api"` entries exist, corlinman has no cross-provider
   failover. Out of scope for this design.
4. **Embedding policy**: explicitly forbid `[embedding].provider` from
   pointing at a `kind = "sub2api"` entry, or warn loudly. Punted to
   Phase 4 — current code lets you do it; it'll fail at first
   embedding call with whatever upstream error sub2api propagates.
5. **Tenant isolation**: a single sub2api `group_id` is a flat namespace.
   Multi-tenant corlinman (`corlinman-tenant` crate) probably wants
   one sub2api group per tenant, but mapping is operator-managed for
   now.

## 9. References

- sub2api: https://github.com/Wei-Shaw/sub2api (LGPL-3.0, 20.2k stars)
- one-api: https://github.com/songquanpeng/one-api (MIT, 33.6k stars)
- corlinman provider schema: `rust/crates/corlinman-core/src/config.rs`
  (`ProviderKind` enum, `ProviderEntry` struct)
- corlinman provider docs: `docs/providers.md`
- Existing OpenAI-compat adapter: `python/packages/corlinman-providers/`
