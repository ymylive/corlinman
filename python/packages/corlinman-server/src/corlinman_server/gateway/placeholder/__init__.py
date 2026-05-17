"""Gateway-owned placeholder resolvers (``{{namespace.key}}``).

Python port of ``rust/crates/corlinman-gateway/src/placeholder/``.

The Rust module hosts ``DynamicResolver`` impls that need gateway-owned
state (per-tenant SQLite handles) so the resolvers live next to the
gateway's ``AppState`` rather than up in ``corlinman-core``. The Python
port follows the same layout: resolvers for ``{{memory.*}}`` and
``{{episodes.*}}`` ship here and delegate to the Python siblings
(``corlinman-memory-host``, ``corlinman-episodes``) where appropriate.

What ships
----------

* :class:`MemoryResolver` — wraps a :class:`MemoryHost` (Python sibling)
  to render ``{{memory.<query>}}``. The Rust version's
  ``DEFAULT_MEMORY_NAMESPACE`` / ``DEFAULT_TOP_K`` constants are
  preserved.
* :class:`EpisodesResolver` — opens per-tenant ``episodes.sqlite`` via
  :func:`corlinman_server.tenancy.tenant_db_path` and serves the
  ``last_24h`` / ``last_week`` / ``last_month`` / ``recent`` /
  ``kind(...)`` / ``about_id(...)`` token set. Logic is ported
  verbatim — the Python siblings cover the *writer* but not the
  read-side resolver, so re-implementing the read path here is the
  minimal cross-package surface.
"""

from __future__ import annotations

from corlinman_server.gateway.placeholder.episodes_stub import (
    DEFAULT_TENANT_SLUG,
    DEFAULT_TOP_N,
    SUMMARY_CHAR_CAP,
    TENANT_METADATA_KEY,
    VALID_KINDS,
    EpisodeBrief,
    EpisodesResolver,
)
from corlinman_server.gateway.placeholder.memory_stub import (
    DEFAULT_MEMORY_NAMESPACE,
    DEFAULT_TOP_K,
    MemoryResolver,
)

__all__ = [
    "DEFAULT_MEMORY_NAMESPACE",
    "DEFAULT_TENANT_SLUG",
    "DEFAULT_TOP_K",
    "DEFAULT_TOP_N",
    "EpisodeBrief",
    "EpisodesResolver",
    "MemoryResolver",
    "SUMMARY_CHAR_CAP",
    "TENANT_METADATA_KEY",
    "VALID_KINDS",
]
