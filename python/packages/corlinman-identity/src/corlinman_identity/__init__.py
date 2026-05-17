"""corlinman-identity — cross-channel ``UserIdentityResolver``.

Python port of the Rust ``corlinman-identity`` crate. Resolves
channel-scoped IDs (``qq:1234``, ``telegram:9876``, ``ios:device-uuid``)
to a canonical opaque :class:`UserId`. Two humans on different channels
stay distinct until they prove they're the same person via the
verification-phrase protocol; only then does the resolver unify their
aliases under one :class:`UserId`.

Tenant-scoped: each tenant has its own
``<data_dir>/tenants/<slug>/user_identity.sqlite`` file.

Public surface mirrors the Rust crate's ``pub use`` list (see
``rust/crates/corlinman-identity/src/lib.rs``).
"""

from corlinman_identity.channels import ChannelAdapter, ChannelRegistry
from corlinman_identity.error import (
    IdentityError,
    InvalidInputError,
    OpenError,
    PhraseAlreadyConsumedError,
    PhraseExpiredError,
    PhraseUnknownError,
    StorageError,
    UserNotFoundError,
)
from corlinman_identity.resolver import IdentityStore
from corlinman_identity.store import SCHEMA_SQL, SqliteIdentityStore, identity_db_path
from corlinman_identity.tenancy import (
    LEGACY_DEFAULT_SLUG,
    TenantId,
    TenantIdLike,
    legacy_default,
    tenant_db_path,
    tenant_slug,
)
from corlinman_identity.types import (
    BindingKind,
    ChannelAlias,
    UserId,
    UserSummary,
    VerificationPhrase,
)
from corlinman_identity.user_identity_resolver import UserIdentityResolver
from corlinman_identity.verification import DEFAULT_TTL_MIN

__all__ = [
    "DEFAULT_TTL_MIN",
    "LEGACY_DEFAULT_SLUG",
    "SCHEMA_SQL",
    "BindingKind",
    "ChannelAdapter",
    "ChannelAlias",
    "ChannelRegistry",
    "IdentityError",
    "IdentityStore",
    "InvalidInputError",
    "OpenError",
    "PhraseAlreadyConsumedError",
    "PhraseExpiredError",
    "PhraseUnknownError",
    "SqliteIdentityStore",
    "StorageError",
    "TenantId",
    "TenantIdLike",
    "UserId",
    "UserIdentityResolver",
    "UserNotFoundError",
    "UserSummary",
    "VerificationPhrase",
    "identity_db_path",
    "legacy_default",
    "tenant_db_path",
    "tenant_slug",
]
