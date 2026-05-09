# Auth flow — corlinman native client

**Status**: Phase 4 W3 C4 iter 10 — extracted from the Mac client's
working implementation. Language-neutral; iOS / Android porters should
read this and `wire-protocol.md` together.

This page documents the *single* authentication contract the gateway
exposes to a native client. The Mac reference client (`AuthStore.swift`,
`OnboardingViewModel.swift`, `GatewayClient.swift`) is one consumer;
nothing here is Swift-specific.

## Overview

There are two credentials a chat client juggles:

| Credential | Used for | Storage |
|---|---|---|
| **Admin Basic auth** (username + password) | One-shot login during onboarding to mint a chat-scoped api_key. After that, used only on tenant switch. | Keychain / Keystore — never UserDefaults / SharedPreferences. |
| **Chat-scoped api_key** (Bearer token) | Every `/v1/*` request — chat completions, approval round-trip, push device registration. | Keychain / Keystore. |

The two live in different secret-store services so a "log out" gesture
can wipe the bearer without dropping the operator's gateway URL +
admin creds.

## First-launch onboarding

```
┌────────────────────────────────────────────────────────────────────┐
│  Operator opens the app for the first time.                        │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────┐
   │  Phase 1 — credentials capture                          │
   │  Form fields: gatewayURL, adminUsername, adminPassword. │
   └─────────────────────────────────────────────────────────┘
                              │  POST /admin/auth/login
                              │  Authorization: Basic <b64(user:pw)>
                              │  Body: <empty>
                              ▼
   ┌─────────────────────────────────────────────────────────┐
   │  Gateway → 200 OK + Set-Cookie: corlinman_admin=...     │
   │  HTTP cookie store (URLSession's, OkHttp's, etc.) picks │
   │  it up automatically; subsequent admin calls thread it. │
   └─────────────────────────────────────────────────────────┘
                              │  GET /admin/tenants?for_user=<username>
                              ▼
   ┌─────────────────────────────────────────────────────────┐
   │  Phase 2 — tenant selection                             │
   │  Response: [ { id, slug, display_name }, … ]            │
   │  Singleton list → auto-pick.                            │
   │  Multi-tenant → render a Picker / Spinner.              │
   └─────────────────────────────────────────────────────────┘
                              │  POST /admin/api_keys
                              │  Authorization: Basic <…>     (or cookie)
                              │  Content-Type: application/json
                              │  Body: { "scope": "chat",
                              │          "username": "<admin>",
                              │          "label": "macOS — <hostname>" }
                              ▼
   ┌─────────────────────────────────────────────────────────┐
   │  Phase 3 — bearer token mint                            │
   │  Response: { key_id, tenant_id, username, scope,        │
   │              label, token, created_at_ms }              │
   │  `token` is the cleartext bearer — returned exactly     │
   │  ONCE. Stash it in Keychain immediately; the gateway    │
   │  only persists the hash.                                │
   └─────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────┐
   │  Phase 4 — done                                         │
   │  Persisted state:                                       │
   │    Keychain "com.corlinman.<platform>.admin":           │
   │      • gateway_base_url                                 │
   │      • admin_username                                   │
   │      • admin_password                                   │
   │    Keychain "com.corlinman.<platform>.chat":            │
   │      • chat_api_key                                     │
   │    UserDefaults / SharedPreferences:                    │
   │      • currentTenantSlug   (non-secret)                 │
   └─────────────────────────────────────────────────────────┘
```

### Concrete request examples

#### Admin login

```http
POST /admin/auth/login HTTP/1.1
Host: gateway.example.com
Authorization: Basic YWRtaW46aHVudGVyMg==
Content-Length: 0

```

Response:

```http
HTTP/1.1 200 OK
Set-Cookie: corlinman_admin=eyJhbGc...; Path=/; HttpOnly; SameSite=Lax
Content-Length: 0
```

#### Tenant list

```http
GET /admin/tenants?for_user=admin HTTP/1.1
Host: gateway.example.com
Cookie: corlinman_admin=eyJhbGc...
```

Response (gateway tolerates either shape — the Mac client decodes both):

```json
{
  "tenants": [
    { "id": "tenant-uuid-1", "slug": "acme", "display_name": "Acme Inc" },
    { "id": "tenant-uuid-2", "slug": "globex", "display_name": "Globex" }
  ]
}
```

or:

```json
[
  { "id": "tenant-uuid-1", "slug": "acme", "display_name": "Acme Inc" }
]
```

#### API key mint

```http
POST /admin/api_keys HTTP/1.1
Host: gateway.example.com
Authorization: Basic YWRtaW46aHVudGVyMg==
Content-Type: application/json
Cookie: corlinman_admin=eyJhbGc...

{ "scope": "chat", "username": "admin", "label": "macOS — alice-mbp" }
```

Response (`MintedApiKey`):

```json
{
  "key_id": "key_01HQX…",
  "tenant_id": "tenant-uuid-1",
  "username": "admin",
  "scope": "chat",
  "label": "macOS — alice-mbp",
  "token": "ck_live_abc123…",
  "created_at_ms": 1746812345678
}
```

`token` is the cleartext bearer. Persist it; the server keeps only
the hash.

## Subsequent launches

```
┌──────────────────────────────────────────────────────────────────┐
│  App start.                                                      │
│  Read Keychain "<…>.admin": gateway_base_url, username, password │
│       Keychain "<…>.chat":  chat_api_key                         │
│       UserDefaults: currentTenantSlug                            │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Bearer present?                                         │
   │   Yes → use as `Authorization: Bearer <token>` for       │
   │         all `/v1/*` requests.                            │
   │   No  → bounce to onboarding (creds may exist but        │
   │         api_key was wiped on tenant switch / log-out).   │
   └──────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Admin cookie expired? (gateway returns 401 on /admin/*) │
   │   → re-POST /admin/auth/login with stored Basic creds.   │
   │   → if THAT 401s, drop cookie + creds, bounce to         │
   │     onboarding.                                          │
   └──────────────────────────────────────────────────────────┘
```

## Tenant switch

Native clients with multi-tenant operators expose a tenant picker in
the toolbar (the Mac client defers this to a follow-up — see iter 10
README "Known gaps"). Switching tenants does:

1. `POST /admin/api_keys` against the new tenant — same body shape,
   new bearer.
2. Stash the new bearer (overwrites the old).
3. Update `currentTenantSlug` in non-secret storage.
4. **Wipe local session cache** for the old tenant — sessions are
   tenant-scoped per `routes/admin/sessions.rs:30`. The Mac client's
   `SessionStore` filters by `tenant_slug` so sessions from the old
   tenant just go stale rather than being deleted; future iters add
   an explicit purge.

## Error taxonomy

| Server response | Native client action |
|---|---|
| `401 Unauthorized` on `/admin/*` | Re-login from stored Basic creds. If THAT fails, drop creds → onboarding. |
| `401 Unauthorized` on `/v1/*` | Mint a fresh api_key against the current tenant. If admin login also fails, drop creds → onboarding. |
| `403 Forbidden` on `/admin/tenants` | Operator's user no longer has the tenant — drop the cached slug, re-list. |
| `5xx` | Surface as a transient error in the UI; do not invalidate creds. |

## What NOT to do

- **Don't** put the bearer in UserDefaults / SharedPreferences. macOS,
  iOS, and Android all have bullet-proof secret stores; use them.
- **Don't** roundtrip the cleartext bearer back through the gateway
  for "verification" — the mint response is the only place you ever
  see it. The hash on the server is one-way.
- **Don't** stash both Bearer + Basic in the same Keychain service;
  log-out / tenant-switch flows want to wipe one without the other.
- **Don't** treat the admin cookie as durable. It's a session cookie
  the gateway can rotate at any time — re-login on 401 is the contract.

## Cross-platform reference

| Surface | macOS / iOS | Android |
|---|---|---|
| Secret storage (admin) | `Security.framework` Keychain, service `com.corlinman.mac.admin` | Android Keystore + EncryptedSharedPreferences, prefs file `corlinman_admin_prefs` |
| Secret storage (chat) | `…mac.chat` | `corlinman_chat_prefs` |
| Cookie storage | `URLSession`'s `HTTPCookieStorage` | OkHttp's `CookieJar` (e.g. `JavaNetCookieJar`) |
| HTTP client | `URLSession` | OkHttp / Ktor / Retrofit |
| First-launch detection | `requiresOnboarding == true` if any of `(baseURL, username, password)` are missing from secret store | Same predicate, against your secret-prefs read |
