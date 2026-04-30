# Tenant slug — cross-language contract

**Status**: Active · **Last revised**: 2026-04-30 · **Phase**: 4 W1.5 (next-tasks A5)

> Canonical specification for the `tenant_id` slug shape. Both the
> Rust `corlinman-tenant::TenantId` newtype and the TypeScript admin
> UI's `ui/lib/api/tenants.ts` validator anchor on this document. A
> third caller (Python proposers, MCP exporters, future SDKs) MUST
> implement the same shape and link back here.

## Pattern

```
^[a-z][a-z0-9-]{0,62}$
```

Equivalent constraints:

- ASCII lowercase letters, digits, and hyphen
- Must start with a letter (`[a-z]`)
- Length **1 to 63** characters (the leading letter plus 0–62 trailing
  chars)
- No leading or trailing whitespace
- No uppercase letters (case-insensitive filesystems would otherwise
  let `Acme` and `acme` collide)
- No dots, underscores, or non-ASCII

## Reserved values

`default` — the legacy single-tenant fallback. Reserved by the Phase
3.1 / S-2 schema migration (every per-tenant SQLite column has
`DEFAULT 'default'`). Do not rename without paired data migration.

## Anchor implementations

| Layer | File | Notes |
|---|---|---|
| Rust  | `rust/crates/corlinman-tenant/src/id.rs::TENANT_ID_RE`        | Source of truth on the write path. Tests in the same module pin a corpus. |
| TS    | `ui/lib/api/tenants.ts::TENANT_SLUG_RE`                       | Defensive client-side validator for the `/admin/tenants` form. |
| Rust  | `rust/crates/corlinman-gateway/src/middleware/tenant_scope.rs` | Re-uses `TenantId::new` to validate `?tenant=` query values. |
| Rust  | `rust/crates/corlinman-gateway/src/routes/admin/tenants.rs`   | Re-uses `TenantId::new` on the `:tenant` path component. |

The TS regex MUST be a literal copy of the Rust pattern. CI grep
against this doc + both files would catch drift.

## Test corpus

Both implementations must produce the same accept / reject decision
for every line below. The Rust integration test
`tenant_id_corpus_round_trips` and the TS unit test
`TENANT_SLUG_RE.test corpus` enumerate the same list:

### Accept

```
default
acme
bravo
acme-corp
acme-2
agency-of-record
a
a-b-c
abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmnopqrstuvwxyz
```

The last entry is exactly 63 characters — the upper bound.

### Reject

```
                          # empty
ACME                      # uppercase
Acme                      # mixed case
0acme                     # leading digit
-acme                     # leading hyphen
acme_corp                 # underscore
acme.corp                 # dot
acme/corp                 # slash
acme corp                 # space
acme corp                 # leading whitespace
acme!                     # punctuation
abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmnopqrstuvwxyzz  # 64 chars
```

## Drift detection

A single shared regex string would prevent drift entirely. Until
codegen is on the table, the cheaper guard is:

1. Both files include a top-of-file comment naming this doc as the
   source of truth.
2. The Rust corpus test
   (`rust/crates/corlinman-tenant/tests/slug_corpus.rs`) and the TS
   corpus test (`ui/lib/api/tenants.test.ts`) both reference this doc
   in a top-of-file comment.
3. Any regex change touches the doc as part of the same commit; if
   the doc isn't touched, reviewers reject the diff.

## Future work

- **Codegen step** — emit the regex string from a single TOML/JSON
  spec file under `docs/contracts/`. Rust `include_str!` + a build
  script for compile-time substitution; TS imports the same string
  via a small loader. Worth it once a third caller appears.
- **Reserved-slug list** — `default` today. If the platform grows
  reserved slugs (e.g. `system`, `internal`), keep them here so
  every implementation rejects the same set.
