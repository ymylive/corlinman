"""corlinman deterministic session replay (Python port).

Python sibling of the Rust crate ``corlinman-replay``. Loads a session
by key from a per-tenant ``sessions.sqlite`` and reconstructs a
structured transcript ready for diff / dump.

Top-level surface:

* :func:`replay` -- one-shot read + transcript build
* :func:`iter_replay_messages` -- async-iterator variant for long
  sessions (streams from SQLite one row at a time)
* :func:`list_sessions` -- enumerate sessions for the admin roster
* :func:`replay_from_messages` -- build the wire shape from an
  already-loaded message list (lets callers reuse this crate's
  serialisation against a non-tenant SQLite layout)

Tenant slug type is local to this package
(:class:`~corlinman_replay.tenant.TenantId`) so we do not couple to
any sibling package's own tenant definition -- the brief explicitly
calls out using a local NewType to avoid cross-package coupling.
"""

from __future__ import annotations

from corlinman_replay.replay import (
    ReplayError,
    ReplayMessage,
    ReplayMode,
    ReplayOutput,
    ReplaySummary,
    SessionListRow,
    SessionNotFoundError,
    StoreLoadError,
    StoreOpenError,
    iter_replay_messages,
    list_sessions,
    replay,
    replay_from_messages,
    sessions_db_path,
)
from corlinman_replay.session_store import (
    CorlinmanError,
    SCHEMA_SQL,
    SessionMessage,
    SessionRole,
    SessionSummary,
    SqliteSessionStore,
)
from corlinman_replay.tenant import (
    DEFAULT_TENANT_ID,
    TENANT_SLUG_REGEX_STR,
    TenantId,
    TenantIdError,
    tenant_db_path,
    tenant_root_dir,
)

__all__ = [
    "CorlinmanError",
    "DEFAULT_TENANT_ID",
    "SCHEMA_SQL",
    "ReplayError",
    "ReplayMessage",
    "ReplayMode",
    "ReplayOutput",
    "ReplaySummary",
    "SessionListRow",
    "SessionMessage",
    "SessionNotFoundError",
    "SessionRole",
    "SessionSummary",
    "SqliteSessionStore",
    "StoreLoadError",
    "StoreOpenError",
    "TENANT_SLUG_REGEX_STR",
    "TenantId",
    "TenantIdError",
    "iter_replay_messages",
    "list_sessions",
    "replay",
    "replay_from_messages",
    "sessions_db_path",
    "tenant_db_path",
    "tenant_root_dir",
]
