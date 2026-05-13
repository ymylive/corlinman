# Migrating from sub2api to newapi

corlinman v0.5.0 replaces `ProviderKind::Sub2api`
([Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api), LGPL-3.0) with
`ProviderKind::Newapi`
([QuantumNous/new-api](https://github.com/QuantumNous/new-api), MIT) as
the channel-pool sidecar. The wire shape (OpenAI-compat REST) stays the
same; the swap brings:

- **MIT licence** instead of LGPL-3.0 — no linkage-boundary
  concerns even if you change the deployment shape later.
- **Audio TTS** (`/v1/audio/speech`) supported natively, in addition to
  chat and embeddings.
- **Active maintenance**, plus the canonical Chinese-community
  channel-pool fork operators already know.

## 5-step migration

1. **Back up the existing config and identity store.**

   ```bash
   cp ~/.corlinman/config.toml ~/.corlinman/config.toml.pre-newapi.bak
   ```

2. **Stand up new-api on the same host (or a neighbour).**

   ```bash
   docker run -d --name newapi -p 3000:3000 \
       -v "$PWD/newapi-data:/data" \
       calciumion/new-api:latest
   ```

   Open `http://localhost:3000`, create a root user, add the OAuth
   subscriptions / API-key channels you used in sub2api, then mint
   **one user token** (sk-…) and **one system access token**.
   corlinman stores both: the user token authorises chat /
   embedding / audio traffic; the system token authorises the admin
   `/api/channel/` endpoint used by the connector page.

3. **Preview the corlinman config rewrite.**

   ```bash
   corlinman config migrate-sub2api --dry-run
   ```

   Inspect the diff. The CLI rewrites every `[providers.X]` block
   where `kind = "sub2api"` to `kind = "newapi"`. Nothing else
   changes; `base_url`, `api_key`, and `params` are preserved.

4. **Apply the rewrite.**

   ```bash
   corlinman config migrate-sub2api --apply
   ```

   The CLI writes `config.toml.sub2api.bak` next to the original
   and rewrites the file in place. Re-running is a no-op
   (`no_sub2api_entries_found`).

5. **Restart corlinman → open `/admin/newapi`.**

   ```bash
   systemctl restart corlinman   # or docker compose restart corlinman
   ```

   The admin UI's new `/admin/newapi` page surfaces the connection
   summary, lets you test a 1-token round-trip, and lists channels by
   capability. The first-run `/onboard` wizard also gains a newapi
   step for fresh installs.

## Caveats

- **No state migration.** `migrate-sub2api` only edits corlinman's
  `config.toml`. It can't move sub2api's `Account` / `Channel` /
  `ApiKey` rows to new-api — different schemas, different OAuth
  implementations. Recreate those in new-api's own console.
- **TTS still uses OpenAI Realtime WebSocket.** corlinman's `[voice]`
  block continues to dial `wss://api.openai.com/v1/realtime`. If you
  pick a TTS channel during onboard, the choice is stored under
  `[providers.newapi.params].newapi_tts_*` for later adoption once
  voice migrates to REST `/v1/audio/speech`. Until then, the voice
  subsystem behaves identically to pre-newapi.
- **Old sub2api containers can stay running.** corlinman simply no
  longer talks to them; tear them down at your leisure.

## Reverting

If you need to roll back:

```bash
mv ~/.corlinman/config.toml.pre-newapi.bak ~/.corlinman/config.toml
```

…and downgrade the corlinman binary to a pre-newapi release. The
`config.toml.sub2api.bak` file produced by `migrate-sub2api --apply`
is also a valid restore point (it captures the on-disk state immediately
before the rewrite).
