# corlinman-identity

Python port of the Rust `corlinman-identity` crate. Cross-channel
`UserIdentityResolver` that resolves channel-scoped IDs (`qq:1234`,
`telegram:9876`, `ios:device-uuid`) to a canonical opaque `UserId`.

Two humans on different channels stay distinct until they prove they
are the same person via the operator-driven verification-phrase
protocol; only then does the resolver unify their aliases under one
`UserId`.

Tenant-scoped: each tenant has its own
`<data_dir>/tenants/<slug>/user_identity.sqlite` so one tenant's
identity graph never spills into another's.

## Status

Direct port of the Rust crate (`rust/crates/corlinman-identity`).
Schema, error variants, and resolver method signatures match the Rust
1:1 so the two implementations can interoperate against the same on-disk
DB.

The tenant boundary is currently expressed as a `TenantId` `NewType`
alias and a structural `TenantIdLike` `Protocol`. Once the concurrent
`corlinman-server.tenancy` module lands, switch `identity_db_path` over
to the canonical implementation (see TODO in `store.py`).
