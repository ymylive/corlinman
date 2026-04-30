# Phase 4 W2 B2 — Cross-channel `UserIdentityResolver`

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-04-30 · **Estimate**: 6-8d

> Deterministic same-human resolution across QQ, Telegram, native iOS,
> Discord, and any future channel. The principle: a `user_id` is the
> **canonical handle for one human**, regardless of which channel they
> spoke through. Channel-specific IDs (`qq:1234`, `tg:9876`,
> `ios:device-uuid`) are *aliases* of a `user_id`, not synonyms for it.

This doc is the design seed for the implementation iterations that
follow. It sets the schema, the resolver contract, and the
verification-phrase exchange protocol. Implementation lands in:

1. **`corlinman-identity` crate** — schema, types, resolver, sqlx-backed store.
2. **Gateway integration** — chat request middleware looks up
   `user_id` from `(channel, channel_user_id)` and stamps it on the
   `HookEvent`/`SessionContext`/etc.
3. **Admin surface** — `/admin/identity` UI + REST routes for
   operator-driven verification + manual unification.
4. **Verification protocol** — operator triggers a one-time phrase from
   one channel, user pastes it in the other, server unifies.

## Why this exists

Today, the only persistent ID associated with a chat is the
**channel-scoped session key** (`qq:1234`, `tg:private:9001`). If the
same human chats over both QQ and Telegram, the agent treats them as
two strangers. The roadmap (`phase4-roadmap.md` §4 W2 B2) calls out the
specific need: traits the persona learned from the QQ channel should
carry over when that human starts a Telegram conversation.

The agent doesn't get to *guess* — wrong unification merges traits
across actually-different humans, and that's a privacy violation. The
verification-phrase protocol forces the human to prove they are the
same person on both channels before the union happens.

## Schema

`<data_dir>/tenants/<slug>/user_identity.sqlite`. Tenant-scoped to keep
one tenant's identity graph from leaking into another's.

```sql
CREATE TABLE IF NOT EXISTS user_identities (
    user_id TEXT PRIMARY KEY,         -- ULID-like handle, opaque
    display_name TEXT,                -- last-known display name (any channel)
    created_at TEXT NOT NULL,         -- RFC-3339
    updated_at TEXT NOT NULL,         -- RFC-3339
    -- Confidence the unification is correct. 1.0 for verified, lower
    -- for proposed-but-not-confirmed alias unions. Operator-set.
    confidence REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS user_aliases (
    channel TEXT NOT NULL,            -- "qq" | "telegram" | "ios" | ...
    channel_user_id TEXT NOT NULL,    -- raw ID inside that channel
    user_id TEXT NOT NULL,            -- FK -> user_identities.user_id
    created_at TEXT NOT NULL,
    -- How the alias was bound. "auto" = first-seen, treated as own user.
    -- "verified" = bound via verification-phrase exchange.
    -- "operator" = manually merged by an admin via /admin/identity.
    binding_kind TEXT NOT NULL,
    PRIMARY KEY (channel, channel_user_id),
    FOREIGN KEY (user_id) REFERENCES user_identities(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_aliases_user_id ON user_aliases(user_id);

CREATE TABLE IF NOT EXISTS verification_phrases (
    phrase TEXT PRIMARY KEY,          -- e.g. "purple-river-83"
    issued_to_user_id TEXT NOT NULL,  -- user the phrase will unify INTO
    issued_on_channel TEXT NOT NULL,
    issued_on_channel_user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,         -- RFC-3339; default 30 min
    consumed_at TEXT,                 -- RFC-3339 once redeemed; NULL while live
    consumed_on_channel TEXT,
    consumed_on_channel_user_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_verification_phrases_expires ON verification_phrases(expires_at);
```

## Crate layout

```
rust/crates/corlinman-identity/
├── Cargo.toml
├── src/
│   ├── lib.rs           # public API (re-exports)
│   ├── types.rs         # UserId, ChannelAlias, BindingKind, VerificationPhrase
│   ├── store.rs         # IdentityStore trait + SqliteIdentityStore impl
│   ├── resolver.rs      # UserIdentityResolver — public surface used by gateway
│   ├── verification.rs  # phrase issue / redeem / expire logic
│   └── error.rs         # IdentityError
└── tests/
    └── integration.rs   # cross-trait round-trip
```

## Public types

```rust
/// Opaque canonical handle for one human. ULID-style: lexicographic-
/// sortable + 26-char base32. Cheap to clone (Arc<str> internally).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct UserId(Arc<str>);

/// One channel-specific binding. The PK in `user_aliases` —
/// `(channel, channel_user_id)` is unique per row.
#[derive(Debug, Clone)]
pub struct ChannelAlias {
    pub channel: String,
    pub channel_user_id: String,
    pub user_id: UserId,
    pub binding_kind: BindingKind,
    pub created_at: OffsetDateTime,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindingKind {
    /// First-seen; treated as its own user until verification or
    /// operator action unifies it.
    Auto,
    /// Bound via the verification-phrase protocol (user proved they
    /// own both channels).
    Verified,
    /// Bound by operator decision on `/admin/identity`.
    Operator,
}

/// Issued by `issue_phrase`, redeemed by `redeem_phrase`. Phrases
/// are short, memorable, and one-shot.
#[derive(Debug, Clone)]
pub struct VerificationPhrase {
    pub phrase: String,
    pub user_id: UserId,
    pub issued_on: ChannelAlias,
    pub expires_at: OffsetDateTime,
}
```

## Resolver contract

The gateway middleware needs **one** call: "give me the canonical
`user_id` for this incoming message." Everything else (issuing
phrases, listing identities for /admin) is admin-side.

```rust
#[async_trait]
pub trait UserIdentityResolver: Send + Sync {
    /// Resolve the canonical user_id for an incoming message. If the
    /// `(channel, channel_user_id)` pair is already known, returns
    /// the bound `user_id`. If new, mints a fresh `user_id` and
    /// records an `Auto` alias for it.
    ///
    /// Idempotent. Concurrent first-call races are serialised by an
    /// internal mutex (or `INSERT OR IGNORE` race for sqlx).
    async fn resolve_or_create(
        &self,
        channel: &str,
        channel_user_id: &str,
        display_name_hint: Option<&str>,
    ) -> Result<UserId, IdentityError>;

    /// Look up without minting. Returns `None` for unknown aliases.
    async fn lookup(
        &self,
        channel: &str,
        channel_user_id: &str,
    ) -> Result<Option<UserId>, IdentityError>;

    /// All aliases for a user (admin surface helper).
    async fn aliases_for(
        &self,
        user_id: &UserId,
    ) -> Result<Vec<ChannelAlias>, IdentityError>;
}
```

## Verification-phrase protocol

The deliberate friction is the point — automatic merging based on
fuzzy signals (same display name, same timezone) is a privacy hazard.
The phrase makes the human prove they own both ends.

1. **Operator triggers**: from one channel (say QQ), the operator runs
   `/admin/identity/issue-phrase` against the QQ alias. Server stores
   a row in `verification_phrases` with `expires_at = now + 30 min`
   and emits the phrase via the chat channel ("Your verification
   phrase is **purple-river-83**. Send it on Telegram within 30
   minutes to unify your identity").
2. **User redeems**: on the second channel (Telegram), user pastes
   the phrase. The chat plugin sees the message, sends it to
   `IdentityResolver::redeem_phrase(phrase, redeemed_on_channel,
   redeemed_on_channel_user_id)`.
3. **Server unifies**: `redeem_phrase`:
   - Loads the row by `phrase`. 404 if missing or `expires_at < now`
     or `consumed_at IS NOT NULL`.
   - Reassigns the redeemer's existing alias (created lazily during
     normal chat) to point to the issuer's `user_id`. The
     redeemer's old `user_id` row is deleted (cascade — its aliases
     all moved over).
   - Marks the phrase consumed.
4. **Channel reply**: plugin echoes "Identity unified. Your traits
   from QQ now apply on Telegram."

Edge cases:
- **Conflicting traits**: when merging two `user_id`s with divergent
  trait state, the persona's reflector runs a one-shot reconcile job
  (out of scope for B2 — flag a future task).
- **Phrase typed wrong**: `redeem_phrase` returns `IdentityError::PhraseUnknown`;
  no row is touched.
- **Phrase already redeemed**: `IdentityError::PhraseAlreadyConsumed`.
- **Race**: two redemptions of the same phrase (unlikely but
  possible) — the second redemption sees `consumed_at IS NOT NULL`
  and errors. SQL-level `UPDATE ... WHERE consumed_at IS NULL`
  serialises the race deterministically.

## Gateway integration

One new place: `chat_request_middleware` (or wherever
`SessionContext` is built per-request). After resolving the
channel-scoped session key, it calls `resolver.resolve_or_create()`
and stamps `session.user_id = Some(user_id)`. Downstream code
(`HookEvent`, `EvolutionObserver`, persona reads) receives the
canonical `user_id` instead of the channel-scoped key.

## Admin surface

- `GET /admin/identity` — list users (paginated, default 50/page).
- `GET /admin/identity/:user_id` — detail view: aliases, last seen
  per channel, trait summary stub.
- `POST /admin/identity/:user_id/issue-phrase` — body
  `{ "channel": "qq", "channel_user_id": "1234" }` → 201
  `{ phrase, expires_at }`.
- `POST /admin/identity/merge` — operator-driven manual merge. Body
  `{ "into_user_id", "from_user_id" }` → 200; binding_kind=Operator
  on the moved aliases.

## Test plan

| Test | Layer | Asserts |
|---|---|---|
| `resolve_or_create_mints_for_unknown` | store | First call mints, second call (same channel pair) returns same `user_id` |
| `resolve_or_create_concurrent_first_call` | store | 100 concurrent first-calls all get the same `user_id` |
| `lookup_returns_none_for_unknown` | store | — |
| `aliases_for_returns_all_bindings` | store | Multiple aliases for one user |
| `issue_phrase_emits_unique_phrase` | verification | 100 issuances → 100 distinct phrases |
| `redeem_phrase_unifies_aliases` | verification | Two `user_id`s → one after redeem; aliases moved |
| `redeem_phrase_expired_errors` | verification | `expires_at < now` → `PhraseExpired` |
| `redeem_phrase_consumed_errors` | verification | Second redemption of same phrase errors |
| `merge_operator_reattributes_traits` | resolver | When B2 lands persona-merge, this test gates the reconcile job |
| `tenant_isolation` | store | User in tenant A invisible to resolver scoped to tenant B |
| `gateway_chat_request_stamps_user_id` | integration | Real chat request → `SessionContext.user_id = Some(...)` |

## Open questions for the implementation iteration

1. **Phrase format**: dictionary words (`purple-river-83`) vs ULID-
   prefix (`U7K3`)? Dictionary is friendlier; ULID-prefix is
   collision-free. Recommendation: 3 dictionary words from a 1024-word
   list, no number suffix → 1B combos with a 30-min window is plenty.
2. **Per-tenant vs global**: cross-tenant unification needed? The
   schema is per-tenant, which means a user with QQ on tenant A and
   Telegram on tenant B can't unify. Phase 4 holds tenants as
   isolation boundaries — this is correct behaviour. If it changes,
   it's a B3 federation concern, not B2.
3. **Trait merge policy**: when two `user_id`s unify, do we merge
   trait state or pick the more-recent? Defer — flag as a follow-up
   task once the persona crate has the API for "import these traits
   into this user". For B2, after merge the moved aliases see only
   the surviving `user_id`'s traits.

## Implementation order (suggested for autonomous iterations)

Each numbered item is a single bounded iteration (~30 min - 2 hours):

1. ✅ **Crate skeleton + schema** [done `e05be35`] — `corlinman-identity/Cargo.toml`,
   `lib.rs` with module decls, `error.rs` with `IdentityError`
   variants, `types.rs` with `UserId`/`ChannelAlias`/`BindingKind`,
   schema constants in `store.rs`. 8 unit tests.
2. ✅ **`SqliteIdentityStore::open` + schema bootstrap** [done `63756c5`] — sqlx
   pool, idempotent re-open, optional `open_with_pool_size` for the
   workspace's WAL-race test convention. 2 new tests.
3. ✅ **`resolve_or_create` + `lookup` + `aliases_for`** [done `5d07c84`] — core CRUD
   on the `IdentityStore` trait. 10 new tests including a 32-way
   concurrent first-call correctness check.
4. ✅ **`issue_phrase` + `redeem_phrase` + `sweep_expired_phrases`** [done `8bed667`]
   — combined into one iteration since the issue/redeem pair is
   tightly coupled. Crockford-base32 phrase format. 9 new tests
   covering happy unify, fresh-bind, expired, already-consumed,
   GC sweep, and generator format.
5. **Gateway integration** — middleware lookup; stamp on
   `SessionContext`. 1 integration test with a real chat request.
   Touches `rust/crates/corlinman-gateway/src/routes/chat.rs`;
   needs an `IdentityStore` field added to `ChatState` and plumbed
   from the boot path.
6. **Admin REST routes** — `/admin/identity` (list), `/admin/identity/:user_id`
   (detail), `/admin/identity/:user_id/issue-phrase` (POST),
   `/admin/identity/merge` (POST operator-driven). ~6 tests.
7. **Admin UI page** — `/admin/identity` table + dialog flow for
   issue-phrase + manual merge. Mock-server contract first, like
   B4 did with sessions.
8. **HookEvent + persona attribution** — once the gateway stamps
   `user_id` on `SessionContext`, propagate it to `HookEvent` and
   the `EvolutionObserver` so per-user (rather than per-alias)
   trait attribution lands. Pairs with the Phase 4 W1.5 A1 work.

**Status snapshot (2026-04-30)**: iters 1-4 done; identity primitive
crate fully self-contained at 29 unit tests. Iters 5-8 are the
integration-side work that surfaces the primitive to chat traffic + UI.

## Out of scope (B2)

- Trait merge policy (see Open questions §3) — separate follow-up.
- Display-name fuzzy matching ("auto-suggest unification when display
  names collide") — privacy hazard; deliberately not built.
- Phone-number / email confirmation channels — phrase-via-chat is
  enough for MVP; channel auth is the trust root.
