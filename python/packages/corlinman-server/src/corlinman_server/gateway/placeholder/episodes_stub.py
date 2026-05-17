"""``{{episodes.*}}`` resolver — Phase 4 W4 D1 iter 7.

Python port of ``rust/crates/corlinman-gateway/src/placeholder/episodes.rs``.

The Python sibling :mod:`corlinman_episodes` ships the *writer* path —
schema, distillation pipeline, runner, CLI — but the *read-side*
resolver lives next to the gateway's ``AppState`` so the per-tenant
``aiosqlite`` pool is reused across the chat / admin surfaces. We port
the logic verbatim (rather than re-export from ``corlinman-episodes``)
to keep the resolver's pool cache local and to mirror the Rust crate
boundary 1:1.

Tokens supported (matches the Rust impl):

* ``{{episodes.last_24h}}`` — top-N by ``importance_score`` over the
  last 24 h.
* ``{{episodes.last_week}}`` — last 7 d.
* ``{{episodes.last_month}}`` — last 30 d.
* ``{{episodes.recent}}`` — last N by ``ended_at`` regardless of score.
* ``{{episodes.kind(<k>)}}`` — filter by ``EpisodeKind``.
* ``{{episodes.about_id(<id>)}}`` — single episode by id.
* ``{{episodes.about(<tag>)}}`` — round-trips the literal token until
  the ``corlinman-tagmemo`` integration lands.

Unknown tokens (typos / future variants) round-trip the literal
``{{episodes.<key>}}`` so the prompt doesn't lose information — same
contract as the Rust impl's ``literal_token`` fallback.

Tenant isolation: ``ctx.metadata["tenant_id"]`` (or
``ctx.metadata.get("tenant_id")``) carries the per-render tenant id.
Missing/empty → ``"default"``, matching the rest of the per-tenant
SQLite layout. Each render opens or reuses one pool per tenant.

``last_referenced_at`` stamping: every render that returns rows fires
a batched ``UPDATE … WHERE id IN (?)`` so the cold-archive sweeps know
what to demote. A failure here is *non-fatal* (we log + continue) so a
transient write error doesn't surface as a chat-rendering 500.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import aiosqlite
import structlog

from corlinman_server.tenancy import TenantId, TenantIdError, tenant_db_path

logger = structlog.get_logger(__name__)

#: Default top-N when the operator hasn't overridden via config. Mirrors
#: ``EpisodesConfig.max_episodes_per_query`` on the Python side.
DEFAULT_TOP_N: Final[int] = 5

#: Char cap on rendered ``summary_text`` per row.
SUMMARY_CHAR_CAP: Final[int] = 240

#: Metadata key the gateway middleware stamps on every render. Kept as
#: a const so the test harness + middleware can't drift.
TENANT_METADATA_KEY: Final[str] = "tenant_id"

#: Default tenant slug — matches ``TenantId.legacy_default``.
DEFAULT_TENANT_SLUG: Final[str] = "default"

#: Mirrors ``EpisodeKind.values()`` from ``corlinman_episodes`` and the
#: Rust ``VALID_KINDS`` constant. A typo or future addition surfaces
#: as an ``parse_call_form`` rejection → literal round-trip.
VALID_KINDS: Final[tuple[str, ...]] = (
    "conversation",
    "evolution",
    "incident",
    "onboarding",
    "operator",
)

_TENANT_FALLBACK_RE = re.compile(r"\A[a-z][a-z0-9-]{0,62}\Z")


# ---------------------------------------------------------------------------
# Row DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EpisodeBrief:
    """Minimal row projection used by the render path.

    The resolver only needs ``id`` (for the reference-stamp UPDATE) and
    ``summary_text`` to render bullets; everything else is reachable
    via ``about_id`` if the operator needs it.
    """

    id: str
    summary_text: str


# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedToken:
    kind: str  # 'window' | 'recent' | 'kind' | 'about_tag' | 'about_id'
    window_seconds: int = 0
    value: str = ""


def _parse_token(raw: str) -> _ParsedToken | None:
    key = raw.strip()
    if key == "last_24h":
        return _ParsedToken("window", window_seconds=24 * 3600)
    if key == "last_week":
        return _ParsedToken("window", window_seconds=7 * 24 * 3600)
    if key == "last_month":
        return _ParsedToken("window", window_seconds=30 * 24 * 3600)
    if key == "recent":
        return _ParsedToken("recent")
    return _parse_call_form(key)


def _parse_call_form(key: str) -> _ParsedToken | None:
    """Recognise ``name(arg)`` shapes — ``kind(...)``, ``about_id(...)``,
    ``about(...)``. Whitespace tolerated between paren and arg."""
    if "(" not in key or not key.endswith(")"):
        return None
    name, rest = key.split("(", 1)
    arg = rest[:-1].strip()
    if not arg:
        return None
    name = name.strip()
    if name == "kind":
        if arg in VALID_KINDS:
            return _ParsedToken("kind", value=arg)
        return None
    if name == "about":
        return _ParsedToken("about_tag", value=arg)
    if name == "about_id":
        return _ParsedToken("about_id", value=arg)
    return None


def _literal_token(key: str) -> str:
    """Build the literal ``{{episodes.<key>}}`` round-trip."""
    return f"{{{{episodes.{key}}}}}"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class EpisodesResolver:
    """Dynamic resolver bound to the gateway's per-tenant data layout.

    The resolver holds a process-wide cache of ``aiosqlite.Connection``
    objects keyed on tenant id; the cache is guarded by an
    :class:`asyncio.Lock` so concurrent first-renders of the same tenant
    don't open two connections.

    Use one resolver per gateway. The Rust impl uses
    ``max_connections=1`` to keep WAL visibility races at bay; we use
    one connection per tenant for the same reason.
    """

    __slots__ = (
        "_root",
        "_top_n",
        "_pools",
        "_pools_lock",
        "_fixed_now_ms",
    )

    def __init__(
        self,
        data_dir: Path | str,
        *,
        top_n: int = DEFAULT_TOP_N,
        fixed_now_ms: int | None = None,
    ) -> None:
        self._root = Path(data_dir)
        self._top_n = max(1, int(top_n))
        self._pools: dict[str, aiosqlite.Connection] = {}
        self._pools_lock = asyncio.Lock()
        # Optional clock override for tests — when set, the rolling
        # windows anchor against this fixed unix-ms instead of
        # ``time.time``.
        self._fixed_now_ms = fixed_now_ms

    @property
    def data_dir(self) -> Path:
        return self._root

    @property
    def top_n(self) -> int:
        return self._top_n

    def with_top_n(self, top_n: int) -> "EpisodesResolver":
        return EpisodesResolver(
            self._root, top_n=top_n, fixed_now_ms=self._fixed_now_ms
        )

    def with_fixed_now_ms(self, now_ms: int) -> "EpisodesResolver":
        return EpisodesResolver(
            self._root, top_n=self._top_n, fixed_now_ms=now_ms
        )

    async def close(self) -> None:
        """Close every cached connection. Safe to call multiple times."""
        async with self._pools_lock:
            for conn in self._pools.values():
                try:
                    await conn.close()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "episodes_resolver.close_failed", error=str(exc)
                    )
            self._pools.clear()

    # ---- internals -------------------------------------------------------

    def _now_ms(self) -> int:
        if self._fixed_now_ms is not None:
            return self._fixed_now_ms
        return int(time.time() * 1000)

    def _episodes_path_for(self, tenant: str) -> Path:
        """Resolve the per-tenant DB path, validating the slug.

        Raises :class:`RuntimeError` on invalid tenant ids — the engine
        catches and surfaces as ``resolver:<msg>``. Mirrors the Rust
        ``PlaceholderError::Resolver`` shape.
        """
        try:
            tid = TenantId.new(tenant)
        except TenantIdError as exc:
            # Fallback regex check for safety even if tenancy package
            # surface ever drifts. Should never trigger in practice.
            if _TENANT_FALLBACK_RE.fullmatch(tenant) is None:
                raise RuntimeError(
                    f"episodes resolver: invalid tenant id {tenant!r}: {exc}"
                ) from exc
            raise RuntimeError(
                f"episodes resolver: invalid tenant id {tenant!r}: {exc}"
            ) from exc
        return tenant_db_path(self._root, tid, "episodes")

    async def _connection_for(self, tenant: str) -> aiosqlite.Connection:
        """Lazily open (or reuse) the per-tenant aiosqlite connection.

        ``create_if_missing(False)`` — the Python writer creates the
        file; the reader never should. Callers that hit a missing file
        short-circuit *before* this is called.
        """
        async with self._pools_lock:
            cached = self._pools.get(tenant)
            if cached is not None:
                return cached
            path = self._episodes_path_for(tenant)
            # ``aiosqlite.connect`` would create the file; the reader
            # contract is read-only-of-existing, so we re-check + bail.
            if not path.exists():
                raise RuntimeError(
                    f"episodes resolver: db file does not exist: {path}"
                )
            try:
                conn = await aiosqlite.connect(
                    str(path),
                    isolation_level=None,  # autocommit — matches WAL contract
                )
                # WAL + NORMAL sync mirrors the Rust pool's options.
                await conn.execute("PRAGMA journal_mode = WAL")
                await conn.execute("PRAGMA synchronous = NORMAL")
            except Exception as exc:
                raise RuntimeError(
                    f"episodes resolver: open pool {path}: {exc}"
                ) from exc
            self._pools[tenant] = conn
            return conn

    # ---- resolve --------------------------------------------------------

    async def resolve(self, key: str, ctx: Any | None = None) -> str:
        """Dispatch ``key`` (the part after ``episodes.``) to the matching
        query strategy.

        Unknown keys round-trip the literal token. A missing DB returns
        empty for known tokens and the literal for unknown ones —
        matches the Rust contract so a fresh tenant doesn't have to
        pre-create the file.
        """
        tenant = _tenant_from_ctx(ctx)

        # Single-tenant happy path: no DB, no rows, no error.
        try:
            path = self._episodes_path_for(tenant)
        except RuntimeError:
            raise

        if not path.exists():
            return (
                "" if _parse_token(key) is not None else _literal_token(key)
            )

        parsed = _parse_token(key)
        if parsed is None:
            return _literal_token(key)

        conn = await self._connection_for(tenant)
        now_ms = self._now_ms()

        if parsed.kind == "window":
            cutoff = now_ms - parsed.window_seconds * 1000
            rows = await _select_top_by_importance(
                conn, tenant, cutoff, self._top_n
            )
            await _stamp_referenced(conn, rows, now_ms)
            return _render_bullets(rows)

        if parsed.kind == "recent":
            rows = await _select_recent(conn, tenant, self._top_n)
            await _stamp_referenced(conn, rows, now_ms)
            return _render_bullets(rows)

        if parsed.kind == "kind":
            rows = await _select_by_kind(
                conn, tenant, parsed.value, self._top_n
            )
            await _stamp_referenced(conn, rows, now_ms)
            return _render_bullets(rows)

        if parsed.kind == "about_id":
            row = await _select_by_id(conn, tenant, parsed.value)
            if row is None:
                # Missing-id renders empty rather than the literal —
                # operators that wrote ``about_id(...)`` expected *some*
                # episode; "no rows" is the right shape.
                return ""
            await _stamp_referenced(conn, [row], now_ms)
            return _truncate_summary(row.summary_text)

        if parsed.kind == "about_tag":
            # Tag join needs ``corlinman-tagmemo`` integration —
            # round-trip the literal until then.
            return _literal_token(key)

        # Should be unreachable but stay defensive — unknown parsed kind
        # treats as literal round-trip.
        return _literal_token(key)  # pragma: no cover

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return (
            f"EpisodesResolver(root={self._root!r}, top_n={self._top_n}, "
            f"fixed_now_ms={self._fixed_now_ms!r})"
        )


def _tenant_from_ctx(ctx: Any | None) -> str:
    """Extract the tenant id from a placeholder context.

    Accepts:

    * ``None`` → default tenant.
    * any object with ``metadata`` mapping attribute.
    * a bare ``Mapping`` (used by tests for convenience).
    """
    if ctx is None:
        return DEFAULT_TENANT_SLUG
    metadata = getattr(ctx, "metadata", None)
    if metadata is None and isinstance(ctx, dict):
        metadata = ctx
    if metadata is None:
        return DEFAULT_TENANT_SLUG
    try:
        value = metadata.get(TENANT_METADATA_KEY)  # type: ignore[union-attr]
    except AttributeError:
        return DEFAULT_TENANT_SLUG
    if not value:
        return DEFAULT_TENANT_SLUG
    return str(value)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def _select_top_by_importance(
    conn: aiosqlite.Connection,
    tenant: str,
    cutoff_ms: int,
    limit: int,
) -> list[EpisodeBrief]:
    try:
        async with conn.execute(
            """SELECT id, summary_text FROM episodes
                 WHERE tenant_id = ? AND ended_at >= ?
                 ORDER BY importance_score DESC, ended_at DESC
                 LIMIT ?""",
            (tenant, cutoff_ms, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        raise RuntimeError(f"episodes resolver: sqlite: {exc}") from exc
    return [EpisodeBrief(id=str(r[0]), summary_text=str(r[1])) for r in rows]


async def _select_recent(
    conn: aiosqlite.Connection, tenant: str, limit: int
) -> list[EpisodeBrief]:
    try:
        async with conn.execute(
            """SELECT id, summary_text FROM episodes
                 WHERE tenant_id = ?
                 ORDER BY ended_at DESC, id DESC
                 LIMIT ?""",
            (tenant, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        raise RuntimeError(f"episodes resolver: sqlite: {exc}") from exc
    return [EpisodeBrief(id=str(r[0]), summary_text=str(r[1])) for r in rows]


async def _select_by_kind(
    conn: aiosqlite.Connection,
    tenant: str,
    kind: str,
    limit: int,
) -> list[EpisodeBrief]:
    try:
        async with conn.execute(
            """SELECT id, summary_text FROM episodes
                 WHERE tenant_id = ? AND kind = ?
                 ORDER BY ended_at DESC, id DESC
                 LIMIT ?""",
            (tenant, kind, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        raise RuntimeError(f"episodes resolver: sqlite: {exc}") from exc
    return [EpisodeBrief(id=str(r[0]), summary_text=str(r[1])) for r in rows]


async def _select_by_id(
    conn: aiosqlite.Connection, tenant: str, id_: str
) -> EpisodeBrief | None:
    try:
        async with conn.execute(
            """SELECT id, summary_text FROM episodes
                 WHERE tenant_id = ? AND id = ?
                 LIMIT 1""",
            (tenant, id_),
        ) as cursor:
            row = await cursor.fetchone()
    except aiosqlite.Error as exc:
        raise RuntimeError(f"episodes resolver: sqlite: {exc}") from exc
    if row is None:
        return None
    return EpisodeBrief(id=str(row[0]), summary_text=str(row[1]))


async def _stamp_referenced(
    conn: aiosqlite.Connection, rows: list[EpisodeBrief], now_ms: int
) -> None:
    """Best-effort ``last_referenced_at`` stamp.

    Never raises — a write-side error here is a cold-archive imprecision,
    not a chat-rendering error. Logged at warn so operators see drift.
    """
    if not rows:
        return
    placeholders = ",".join("?" for _ in rows)
    sql = (
        f"UPDATE episodes SET last_referenced_at = ? WHERE id IN ({placeholders})"
    )
    params: tuple[Any, ...] = (now_ms, *(r.id for r in rows))
    try:
        await conn.execute(sql, params)
    except aiosqlite.Error as exc:
        logger.warning(
            "episodes_resolver.stamp_failed",
            row_count=len(rows),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _render_bullets(rows: list[EpisodeBrief]) -> str:
    if not rows:
        return ""
    parts: list[str] = []
    for row in rows:
        parts.append(f"- {_truncate_summary(row.summary_text)}")
    return "\n".join(parts)


def _truncate_summary(text: str) -> str:
    """Char-aware truncation — splits on a Unicode codepoint boundary."""
    if len(text) <= SUMMARY_CHAR_CAP:
        return text
    return text[:SUMMARY_CHAR_CAP] + "…"


__all__ = [
    "DEFAULT_TENANT_SLUG",
    "DEFAULT_TOP_N",
    "EpisodeBrief",
    "EpisodesResolver",
    "SUMMARY_CHAR_CAP",
    "TENANT_METADATA_KEY",
    "VALID_KINDS",
]
