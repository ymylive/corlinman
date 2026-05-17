"""Schema + thin CRUD wrapper for the root-level ``tenants.sqlite`` admin DB.

Python port of ``corlinman-tenant::admin_schema``. This DB is **not**
per-tenant — it lives at ``<data_dir>/tenants.sqlite`` (singular, no
subdirectory) and stores the master list of tenants plus their
per-tenant admin credentials, federation peerings, and API-key roster.

Four tables, all append-mostly:

- ``tenants`` — canonical tenant roster (slug + display name +
  created-at + reserved soft-delete column).
- ``tenant_admins`` — argon2id password hashes for each
  ``(tenant_id, username)`` pair. ``ON DELETE CASCADE`` on the FK
  keeps the rows in sync if the parent tenant is hard-deleted.
- ``tenant_federation_peers`` — asymmetric directional opt-in:
  "tenant ``peer_tenant_id`` accepts federated proposals from tenant
  ``source_tenant_id``".
- ``tenant_api_keys`` — per-(tenant, username) bearer tokens.
  ``token_hash`` is the hex-encoded sha256 of the cleartext (never
  the cleartext itself). Cleartext is returned **once** from
  :meth:`AdminDb.mint_api_key` and never persisted.

:class:`AdminDb` is the user-facing wrapper. Cheap to share across
coroutines (single ``aiosqlite.Connection``); CLIs open it once per
command and the server opens it once at boot.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from corlinman_server.tenancy.id import TenantId, TenantIdError


def _unix_now_ms() -> int:
    """Wall-clock unix-millis. Saturates at :data:`sys.maxsize` rather
    than overflow on a clock set absurdly far in the future; clamps
    to 0 on pre-1970 clocks. Local helper so the federation API
    doesn't pull in a time library just to stamp a row."""
    ts = time.time()
    if ts <= 0:
        return 0
    # Python ints are unbounded — no need for the `i64::MAX`
    # saturation that the Rust side does, but we still clamp to
    # 2^63-1 to keep the on-disk integer storage within INTEGER
    # range across all SQLite implementations.
    millis = int(ts * 1000)
    if millis > 9223372036854775807:
        return 9223372036854775807
    return millis


# CREATE TABLE script for ``tenants.sqlite``. Idempotent: re-applying
# is safe against an existing file. New columns must land via an
# idempotent ALTER (mirror the Phase 3.1 / Phase 4 Item 1 pattern in
# other crates) — append a migration constant if/when the time comes;
# v1 is column-stable.
SCHEMA_SQL: str = r"""
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    deleted_at    INTEGER
);

CREATE TABLE IF NOT EXISTS tenant_admins (
    tenant_id     TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, username)
);

CREATE INDEX IF NOT EXISTS idx_tenants_active
    ON tenants(deleted_at) WHERE deleted_at IS NULL;

-- Phase 4 W2 B3 iter 1: per-tenant evolution federation opt-in roster.
-- Asymmetric directional peering: a row reads "tenant `peer_tenant_id`
-- accepts federated proposals from tenant `source_tenant_id`". A → B
-- opt-in does NOT imply B → A. Both slugs are TenantId values; the
-- wrapper enforces shape at the API boundary, not at the SQL layer, to
-- keep this table forward-compatible with future ID shapes.
-- `accepted_by` is the operator (admin username) who accepted on the
-- peer side; nullable so historical / system-seeded rows don't have to
-- pretend a human approved them.
CREATE TABLE IF NOT EXISTS tenant_federation_peers (
    peer_tenant_id   TEXT NOT NULL,
    source_tenant_id TEXT NOT NULL,
    accepted_at_ms   INTEGER NOT NULL,
    accepted_by      TEXT,
    PRIMARY KEY (peer_tenant_id, source_tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_federation_peers_source
    ON tenant_federation_peers(source_tenant_id);

-- Phase 4 W3 C4 iter 2: per-(tenant, username) bearer tokens minted via
-- `POST /admin/api_keys` for native clients. Stores only the sha256 hash
-- of the cleartext token; the operator is shown the cleartext **once**
-- on the response to the mint call. Subsequent listings expose the
-- key id + label + scope + last_used_at, never the cleartext.
--
-- `scope` is a free-form string ("chat" today; future: "chat,admin",
-- "embeddings", etc.). We deliberately keep this textual rather than a
-- typed enum so adding a new scope at server layer doesn't require an
-- admin-DB schema migration. The auth middleware (when wired) splits
-- on comma and matches against the route's required scope.
--
-- `revoked_at` is `NULL` for active rows; populated to a unix-millis
-- stamp once an admin revokes the key. Revoked rows stay in the table
-- so audit trails survive — callers filter on `revoked_at IS NULL` to
-- get the active set.
CREATE TABLE IF NOT EXISTS tenant_api_keys (
    key_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    scope         TEXT NOT NULL,
    label         TEXT,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at_ms INTEGER NOT NULL,
    last_used_at_ms INTEGER,
    revoked_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_active
    ON tenant_api_keys(tenant_id) WHERE revoked_at_ms IS NULL;
CREATE INDEX IF NOT EXISTS idx_api_keys_token_hash
    ON tenant_api_keys(token_hash);
"""


# ---------------------------------------------------------------------------
# Row dataclasses — mirror the Rust struct surface 1:1.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantRow:
    """One row from ``tenants``. ``deleted_at`` is non-None only on
    soft-deleted rows (reserved for Wave 2+); active rows have
    ``None``."""

    tenant_id: TenantId
    display_name: str
    created_at: int
    deleted_at: int | None


@dataclass(frozen=True)
class AdminRow:
    """One row from ``tenant_admins``. ``password_hash`` is the full
    argon2id ``$argon2id$...`` encoded string — never a raw password."""

    tenant_id: TenantId
    username: str
    password_hash: str
    created_at: int


@dataclass(frozen=True)
class FederationPeer:
    """One row from ``tenant_federation_peers`` (Phase 4 W2 B3 iter 1).

    Reads as: tenant ``peer_tenant_id`` accepts federated proposals
    **from** tenant ``source_tenant_id``. The opt-in is asymmetric —
    A accepting from B does not imply B accepts from A.
    """

    peer_tenant_id: TenantId
    source_tenant_id: TenantId
    accepted_at_ms: int
    accepted_by: str | None


@dataclass(frozen=True)
class ApiKeyRow:
    """One row from ``tenant_api_keys`` (Phase 4 W3 C4 iter 2).

    ``token_hash`` is the hex-encoded sha256 of the cleartext token —
    **never** the cleartext itself. The cleartext is returned once
    from :meth:`AdminDb.mint_api_key` and never persisted anywhere we
    can read back; subsequent listings are hash-only.
    """

    key_id: str
    tenant_id: TenantId
    username: str
    scope: str
    label: str | None
    token_hash: str
    created_at_ms: int
    last_used_at_ms: int | None
    revoked_at_ms: int | None


@dataclass(frozen=True)
class MintedApiKey:
    """Result of :meth:`AdminDb.mint_api_key`. Carries the cleartext
    bearer token in ``token`` — surface it to the operator immediately
    and drop the struct; the row in the DB only retains the sha256
    hash."""

    row: ApiKeyRow
    token: str


# ---------------------------------------------------------------------------
# Error hierarchy — mirrors `AdminDbError` variants.
# ---------------------------------------------------------------------------


class AdminDbError(RuntimeError):
    """Base class for admin-DB failures."""


class AdminDbConnectError(AdminDbError):
    """Connection open or schema-apply failed."""

    def __init__(self, db_path: Path, source: BaseException) -> None:
        self.db_path = db_path
        self.source = source
        super().__init__(f"connect / apply schema {db_path}: {source}")


class TenantExistsError(AdminDbError):
    """``create_tenant`` rejected because the slug already exists."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"tenant {tenant_id!r} already exists")


class AdminExistsError(AdminDbError):
    """``add_admin`` rejected because the ``(tenant, username)`` pair
    already exists."""

    def __init__(self, tenant: str, username: str) -> None:
        self.tenant = tenant
        self.username = username
        super().__init__(f"admin {username!r} already exists for tenant {tenant!r}")


# ---------------------------------------------------------------------------
# AdminDb — thin CRUD wrapper.
# ---------------------------------------------------------------------------


class AdminDb:
    """Thin CRUD wrapper over the ``tenants.sqlite`` admin DB.

    Holds a single :class:`aiosqlite.Connection` opened at
    :meth:`open`. Construct via :meth:`AdminDb.open` (the ``__init__``
    is internal so callers don't accidentally hand it a pre-opened
    handle that hasn't had the schema applied).
    """

    def __init__(self, conn: aiosqlite.Connection, db_path: Path) -> None:
        # Internal — call :meth:`open` instead.
        self._conn = conn
        self._db_path = db_path

    # ---- lifecycle -------------------------------------------------------------

    @classmethod
    async def open(cls, path: Path | str) -> AdminDb:
        """Open (or create) the admin DB at ``path``. Applies
        :data:`SCHEMA_SQL` idempotently. WAL +
        ``synchronous=NORMAL`` + ``foreign_keys=ON`` matches the rest
        of the corlinman SQLite stores.
        """
        db_path = Path(path)
        try:
            # Ensure parent dir exists — tests pass ``tempdir / "x.sqlite"``
            # but production may point at a fresh data dir.
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(db_path))
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.executescript(SCHEMA_SQL)
            await conn.commit()
        except BaseException as exc:
            raise AdminDbConnectError(db_path, exc) from exc
        return cls(conn, db_path)

    async def close(self) -> None:
        """Close the underlying connection. Idempotent — a second
        ``close()`` after the connection is already gone is silently
        swallowed (best-effort drain on shutdown)."""
        with contextlib.suppress(Exception):
            await self._conn.close()

    def connection(self) -> aiosqlite.Connection:
        """Borrow the underlying connection. Useful for tests; production
        code should prefer the typed methods below."""
        return self._conn

    def db_path(self) -> Path:
        """The path the wrapper was opened against."""
        return self._db_path

    # ---- tenants ---------------------------------------------------------------

    async def create_tenant(self, tenant_id: TenantId, display_name: str, created_at: int) -> None:
        """INSERT a new tenant row. Rejects duplicates with
        :class:`TenantExistsError` rather than letting the UNIQUE
        constraint surface as a generic sqlite error."""
        try:
            await self._conn.execute(
                "INSERT INTO tenants (tenant_id, display_name, created_at) VALUES (?, ?, ?)",
                (tenant_id.as_str(), display_name, created_at),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError as exc:
            # sqlite3 surfaces "UNIQUE constraint failed: tenants.tenant_id"
            # as IntegrityError; we translate to the typed error so
            # callers can branch without parsing strings.
            if "UNIQUE" in str(exc):
                raise TenantExistsError(tenant_id.as_str()) from exc
            raise

    async def add_admin(
        self,
        tenant_id: TenantId,
        username: str,
        password_hash: str,
        created_at: int,
    ) -> None:
        """INSERT a new admin row, scoped to a tenant. Rejects duplicate
        ``(tenant, username)`` with :class:`AdminExistsError`."""
        try:
            await self._conn.execute(
                "INSERT INTO tenant_admins "
                "(tenant_id, username, password_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (tenant_id.as_str(), username, password_hash, created_at),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError as exc:
            msg = str(exc)
            # PRIMARY KEY violation on (tenant_id, username) → typed err.
            # FOREIGN KEY violation (tenant doesn't exist) → re-raise.
            if "UNIQUE" in msg or "PRIMARY KEY" in msg:
                raise AdminExistsError(tenant_id.as_str(), username) from exc
            raise

    async def list_active(self) -> list[TenantRow]:
        """All active tenants, ordered by ``tenant_id`` for stable
        output."""
        rows: list[TenantRow] = []
        async with self._conn.execute(
            "SELECT tenant_id, display_name, created_at, deleted_at "
            "FROM tenants WHERE deleted_at IS NULL "
            "ORDER BY tenant_id ASC"
        ) as cursor:
            async for r in cursor:
                slug = str(r[0])
                try:
                    tenant_id = TenantId.new(slug)
                except TenantIdError as exc:
                    raise AdminDbError(f"invalid tenant_id {slug!r} stored in DB: {exc}") from exc
                rows.append(
                    TenantRow(
                        tenant_id=tenant_id,
                        display_name=str(r[1]),
                        created_at=int(r[2]),
                        deleted_at=(int(r[3]) if r[3] is not None else None),
                    )
                )
        return rows

    async def get(self, tenant_id: TenantId) -> TenantRow | None:
        """Fetch a single tenant by slug. Returns ``None`` when no such
        row exists rather than raising — soft-deleted rows are
        returned as-is so the operator can see why a tenant they
        expect is "missing"."""
        async with self._conn.execute(
            "SELECT tenant_id, display_name, created_at, deleted_at "
            "FROM tenants WHERE tenant_id = ?",
            (tenant_id.as_str(),),
        ) as cursor:
            r = await cursor.fetchone()
        if r is None:
            return None
        return TenantRow(
            tenant_id=tenant_id,
            display_name=str(r[1]),
            created_at=int(r[2]),
            deleted_at=(int(r[3]) if r[3] is not None else None),
        )

    async def list_admins(self, tenant_id: TenantId) -> list[AdminRow]:
        """Admins for a tenant, ordered by username."""
        rows: list[AdminRow] = []
        async with self._conn.execute(
            "SELECT tenant_id, username, password_hash, created_at "
            "FROM tenant_admins WHERE tenant_id = ? ORDER BY username ASC",
            (tenant_id.as_str(),),
        ) as cursor:
            async for r in cursor:
                rows.append(
                    AdminRow(
                        tenant_id=tenant_id,
                        username=str(r[1]),
                        password_hash=str(r[2]),
                        created_at=int(r[3]),
                    )
                )
        return rows

    # ---- federation peers ------------------------------------------------------

    async def add_federation_peer(self, peer: TenantId, source: TenantId, accepted_by: str) -> None:
        """Register that ``peer`` accepts federated proposals from
        ``source``. ``accepted_at_ms`` is sampled from the wall clock
        at insert time so callers don't have to thread a clock —
        federation opt-in is an operator action, not a replayable
        signal. Idempotent via the composite primary key: adding the
        same ``(peer, source)`` pair twice is a no-op at the row
        level (the existing row's timestamp / ``accepted_by`` are
        preserved). Callers that want last-writer-wins semantics
        should :meth:`remove_federation_peer` first.
        """
        accepted_at_ms = _unix_now_ms()
        await self._conn.execute(
            "INSERT OR IGNORE INTO tenant_federation_peers "
            "(peer_tenant_id, source_tenant_id, accepted_at_ms, accepted_by) "
            "VALUES (?, ?, ?, ?)",
            (peer.as_str(), source.as_str(), accepted_at_ms, accepted_by),
        )
        await self._conn.commit()

    async def remove_federation_peer(self, peer: TenantId, source: TenantId) -> bool:
        """Revoke a federation opt-in. Returns ``True`` when a row was
        actually deleted, ``False`` when no matching row existed (so
        callers can distinguish idempotent revoke vs operator typo
        without a separate existence check)."""
        cursor = await self._conn.execute(
            "DELETE FROM tenant_federation_peers WHERE peer_tenant_id = ? AND source_tenant_id = ?",
            (peer.as_str(), source.as_str()),
        )
        # `rowcount` is set on DELETE before commit in sqlite3 — capture
        # before the await on commit() invalidates the cursor state.
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        return affected > 0

    async def list_federation_sources_for(self, peer: TenantId) -> list[FederationPeer]:
        """ "What does tenant ``peer`` accept from?" — returns every row
        where this tenant is the receiving side, ordered by
        ``source_tenant_id`` for stable output."""
        return await self._fetch_federation(
            "SELECT peer_tenant_id, source_tenant_id, accepted_at_ms, accepted_by "
            "FROM tenant_federation_peers "
            "WHERE peer_tenant_id = ? "
            "ORDER BY source_tenant_id ASC",
            (peer.as_str(),),
        )

    async def list_federation_peers_of(self, source: TenantId) -> list[FederationPeer]:
        """ "Who accepts from tenant ``source``?" — returns every row
        where this tenant is the publishing side, ordered by
        ``peer_tenant_id`` for stable output. Used at rebroadcast time
        to fan a source apply out to interested peers (driven by the
        ``idx_federation_peers_source`` index)."""
        return await self._fetch_federation(
            "SELECT peer_tenant_id, source_tenant_id, accepted_at_ms, accepted_by "
            "FROM tenant_federation_peers "
            "WHERE source_tenant_id = ? "
            "ORDER BY peer_tenant_id ASC",
            (source.as_str(),),
        )

    async def _fetch_federation(self, sql: str, params: tuple[object, ...]) -> list[FederationPeer]:
        rows: list[FederationPeer] = []
        async with self._conn.execute(sql, params) as cursor:
            async for r in cursor:
                peer_slug = str(r[0])
                source_slug = str(r[1])
                try:
                    peer_tenant_id = TenantId.new(peer_slug)
                    source_tenant_id = TenantId.new(source_slug)
                except TenantIdError as exc:
                    raise AdminDbError(f"invalid federation tenant id stored in DB: {exc}") from exc
                rows.append(
                    FederationPeer(
                        peer_tenant_id=peer_tenant_id,
                        source_tenant_id=source_tenant_id,
                        accepted_at_ms=int(r[2]),
                        accepted_by=(str(r[3]) if r[3] is not None else None),
                    )
                )
        return rows

    # ---- api keys --------------------------------------------------------------

    async def mint_api_key(
        self,
        tenant_id: TenantId,
        username: str,
        scope: str,
        label: str | None,
    ) -> MintedApiKey:
        """Mint a new bearer token for ``(tenant, username, scope)``.

        Generates a cryptographically random cleartext (``ck_`` prefix +
        two :func:`uuid.uuid4` blobs concatenated → 67-char total
        token), stores its sha256 hash, and returns the
        :class:`MintedApiKey` envelope. The cleartext lives only in
        the return value — once the caller drops it, recovery is
        impossible (modulo the hash inversion problem). Callers must
        surface it to the operator immediately.

        ``key_id`` is a separate uuid so the caller has a stable
        handle for :meth:`revoke_api_key` / :meth:`list_api_keys`
        without ever needing to re-display the cleartext token.
        """
        key_id = uuid.uuid4().hex
        # ``uuid4().hex`` is 32 hex chars; concatenating two yields 64
        # hex chars after the `ck_` prefix → 67-char total, matching
        # the Rust ``Uuid::new_v4().simple()`` pair shape byte-for-byte.
        token = f"ck_{uuid.uuid4().hex}{uuid.uuid4().hex}"
        token_hash = hash_api_key_token(token)
        created_at_ms = _unix_now_ms()

        await self._conn.execute(
            "INSERT INTO tenant_api_keys "
            "(key_id, tenant_id, username, scope, label, token_hash, "
            " created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                key_id,
                tenant_id.as_str(),
                username,
                scope,
                label,
                token_hash,
                created_at_ms,
            ),
        )
        await self._conn.commit()

        return MintedApiKey(
            row=ApiKeyRow(
                key_id=key_id,
                tenant_id=tenant_id,
                username=username,
                scope=scope,
                label=label,
                token_hash=token_hash,
                created_at_ms=created_at_ms,
                last_used_at_ms=None,
                revoked_at_ms=None,
            ),
            token=token,
        )

    async def list_api_keys(self, tenant_id: TenantId) -> list[ApiKeyRow]:
        """List active (``revoked_at_ms IS NULL``) keys for a tenant,
        ordered by ``created_at_ms DESC`` so the UI's "most recent
        first" view is natural. Revoked rows stay in the table for
        audit but are excluded here; callers that need the full set
        should query the connection directly."""
        rows: list[ApiKeyRow] = []
        async with self._conn.execute(
            "SELECT key_id, tenant_id, username, scope, label, token_hash, "
            "       created_at_ms, last_used_at_ms, revoked_at_ms "
            "FROM tenant_api_keys "
            "WHERE tenant_id = ? AND revoked_at_ms IS NULL "
            "ORDER BY created_at_ms DESC",
            (tenant_id.as_str(),),
        ) as cursor:
            async for r in cursor:
                slug = str(r[1])
                try:
                    row_tenant_id = TenantId.new(slug)
                except TenantIdError as exc:
                    raise AdminDbError(f"invalid tenant_id {slug!r} stored in DB: {exc}") from exc
                rows.append(
                    ApiKeyRow(
                        key_id=str(r[0]),
                        tenant_id=row_tenant_id,
                        username=str(r[2]),
                        scope=str(r[3]),
                        label=(str(r[4]) if r[4] is not None else None),
                        token_hash=str(r[5]),
                        created_at_ms=int(r[6]),
                        last_used_at_ms=(int(r[7]) if r[7] is not None else None),
                        revoked_at_ms=(int(r[8]) if r[8] is not None else None),
                    )
                )
        return rows

    async def revoke_api_key(self, key_id: str) -> bool:
        """Revoke a key by ``key_id``. Returns ``True`` when a row was
        actually flipped (active → revoked); ``False`` when the row
        was already revoked or doesn't exist. Idempotent."""
        now = _unix_now_ms()
        cursor = await self._conn.execute(
            "UPDATE tenant_api_keys SET revoked_at_ms = ? "
            "WHERE key_id = ? AND revoked_at_ms IS NULL",
            (now, key_id),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        return affected > 0

    async def verify_api_key(self, token: str) -> ApiKeyRow | None:
        """Verify a cleartext token. Returns the matching active row
        when the hash matches and ``revoked_at_ms IS NULL``; ``None``
        otherwise. Constant-time comparison is **not** required at
        this layer because we look up by hash directly — the SQL
        index makes the match an O(1) hash equality on indexed bytes.

        Updates ``last_used_at_ms`` on hit so the UI's "last used"
        column stays fresh. The bump is best-effort; a failure logs
        nothing here (the Rust side uses :mod:`tracing`; the Python
        side intentionally stays silent because the operator's chat
        request shouldn't fail on a stats column either way).
        """
        token_hash = hash_api_key_token(token)
        async with self._conn.execute(
            "SELECT key_id, tenant_id, username, scope, label, token_hash, "
            "       created_at_ms, last_used_at_ms, revoked_at_ms "
            "FROM tenant_api_keys "
            "WHERE token_hash = ? AND revoked_at_ms IS NULL",
            (token_hash,),
        ) as cursor:
            r = await cursor.fetchone()

        if r is None:
            return None

        slug = str(r[1])
        try:
            row_tenant_id = TenantId.new(slug)
        except TenantIdError as exc:
            raise AdminDbError(f"invalid tenant_id {slug!r} stored in DB: {exc}") from exc
        key_id = str(r[0])

        # Best-effort `last_used_at_ms` bump. Failure here is swallowed
        # — the operator's chat request shouldn't fail on a stats
        # column. (The Rust side surfaces a `tracing::warn!`; the
        # Python port intentionally stays silent because we don't want
        # to wire a logger dependency into this thin CRUD wrapper.)
        now = _unix_now_ms()
        with contextlib.suppress(Exception):
            await self._conn.execute(
                "UPDATE tenant_api_keys SET last_used_at_ms = ? WHERE key_id = ?",
                (now, key_id),
            )
            await self._conn.commit()

        return ApiKeyRow(
            key_id=key_id,
            tenant_id=row_tenant_id,
            username=str(r[2]),
            scope=str(r[3]),
            label=(str(r[4]) if r[4] is not None else None),
            token_hash=str(r[5]),
            created_at_ms=int(r[6]),
            last_used_at_ms=now,
            revoked_at_ms=(int(r[8]) if r[8] is not None else None),
        )


# ---------------------------------------------------------------------------
# Free helpers.
# ---------------------------------------------------------------------------


def hash_api_key_token(token: str) -> str:
    """Hash an api-key cleartext to its hex-encoded sha256 digest.

    Public for an auth middleware that wants to pre-hash tokens before
    any DB call — verifying a token is then a simple equality check
    over an indexed column.

    Pinned by test against a known SHA-256 vector so a stray hashing
    implementation swap surfaces immediately.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "SCHEMA_SQL",
    "AdminDb",
    "AdminDbConnectError",
    "AdminDbError",
    "AdminExistsError",
    "AdminRow",
    "ApiKeyRow",
    "FederationPeer",
    "MintedApiKey",
    "TenantExistsError",
    "TenantRow",
    "hash_api_key_token",
]
