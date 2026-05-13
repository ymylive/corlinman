# newapi integration — operator overview

Status: shipped in v0.5.0. Replaces the earlier sub2api integration.
Full design: [`docs/superpowers/specs/2026-05-13-newapi-integration-design.md`](../superpowers/specs/2026-05-13-newapi-integration-design.md).
Migration: [`docs/migration/sub2api-to-newapi.md`](../migration/sub2api-to-newapi.md).

## 1. What is newapi?

[QuantumNous/new-api](https://github.com/QuantumNous/new-api) is an
MIT-licensed Go service that pools OpenAI-wire upstream channels
(direct keys, OAuth subscriptions, third-party gateways) behind one URL.
corlinman uses it as a sidecar: operators run new-api separately,
point one `[providers.newapi]` entry at it, and every LLM / embedding
/ audio TTS call flows through it.

## 2. Why a dedicated `kind`?

new-api speaks pure OpenAI wire format, so it could ride on
`kind = "openai_compatible"`. The dedicated `kind = "newapi"` exists
so:

- The admin UI shows "newapi" in the kind dropdown instead of an
  undifferentiated "openai_compatible".
- The `[providers.newapi.params]` block can carry new-api-specific
  hints (`newapi_admin_url`, `newapi_admin_key`,
  `newapi_tts_channel_id`, …) without polluting the
  `openai_compatible` schema.
- The `/admin/newapi` connector page can dial new-api's admin API
  (`/api/user/self`, `/api/channel/`, `/api/status`) using the
  thin `corlinman-newapi-client` crate.

## 3. Configuration

```toml
[providers.newapi]
kind     = "newapi"
base_url = "http://127.0.0.1:3000"
api_key  = { env = "NEWAPI_TOKEN" }    # newapi user token (sk-…)
enabled  = true

[providers.newapi.params]
newapi_admin_url = "http://127.0.0.1:3000/api"
newapi_admin_key = { env = "NEWAPI_ADMIN_TOKEN" }  # system access token

[models]
default = "gpt-4o-mini"

[models.aliases.gpt-4o-mini]
model    = "gpt-4o-mini"
provider = "newapi"

[embedding]
enabled   = true
provider  = "newapi"
model     = "text-embedding-3-small"
dimension = 1536
```

Two tokens, distinct purposes:

| field | purpose | required |
|-------|---------|----------|
| `api_key` | user token; authorises chat/embedding/audio traffic to `/v1/*` | yes |
| `params.newapi_admin_key` | system access token; authorises admin reads (`/api/channel/`, `/api/user/self`) | optional but recommended; the `/admin/newapi` page needs it to surface health |

## 4. Admin surface

- **`GET /admin/newapi`** — summary (masked token, admin-key
  presence, status). 503 when no enabled newapi provider exists.
- **`GET /admin/newapi/channels?type={llm|embedding|tts}`** — live
  channel list via the sidecar's `/api/channel/?type=`.
- **`POST /admin/newapi/probe`** — no-side-effect validation of a
  `(base_url, token, admin_token?)` triple (used by the UI before
  saving an edit).
- **`POST /admin/newapi/test`** — 1-token `/v1/chat/completions`
  against the active newapi entry; reports latency.
- **`PATCH /admin/newapi`** — partial update with re-probe before
  atomic write.

The UI page lives at `/newapi` and offers the connection card +
test button + channel table (with type tabs).

## 5. First-run onboard wizard

The `/onboard` page is a 4-step wizard:

```
account → newapi → models → confirm
```

Step 1 writes the `[admin]` block via the existing
`POST /admin/onboard`. Step 2 probes the newapi connection. Step 3
fetches channels by capability and lets the operator pick one
(channel, model) pair each for LLM, embedding, and TTS. Step 4
atomically writes `[providers.newapi]`, `[models]`,
`[models.aliases.*]`, and `[embedding]`, then redirects to
`/login`.

Server-side session state is deliberately avoided: the UI carries
everything inline in each POST. The user types the newapi token
twice (probe + finalize) — accepted trade-off for simpler ops.

## 6. Voice / TTS caveat

corlinman's `[voice]` block currently uses OpenAI Realtime
(`wss://api.openai.com/v1/realtime`), which new-api does not
serve. When the onboard wizard records a TTS channel pick it
lands under `[providers.newapi.params]` as
`newapi_tts_{model,voice,channel_id}` for later adoption once
the voice subsystem migrates to REST `/v1/audio/speech`. Until
then, voice traffic continues to dial OpenAI directly.

## 7. Migration from sub2api

See [`docs/migration/sub2api-to-newapi.md`](../migration/sub2api-to-newapi.md).
TL;DR: `corlinman config migrate-sub2api --apply` rewrites
`kind = "sub2api"` to `kind = "newapi"` in place (with backup).

## 8. References

- new-api: https://github.com/QuantumNous/new-api (MIT)
- corlinman-newapi-client crate: `rust/crates/corlinman-newapi-client/`
- admin routes: `rust/crates/corlinman-gateway/src/routes/admin/newapi.rs`
- onboard routes: `rust/crates/corlinman-gateway/src/routes/admin/onboard.rs`
- migration CLI: `rust/crates/corlinman-cli/src/cmd/config.rs`
- UI: `ui/app/onboard/page.tsx`, `ui/app/(admin)/newapi/page.tsx`
- full design: `docs/superpowers/specs/2026-05-13-newapi-integration-design.md`
