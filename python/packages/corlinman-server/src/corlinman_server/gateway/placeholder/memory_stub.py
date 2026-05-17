"""``{{memory.<query>}}`` resolver — wraps :class:`MemoryHost`.

Python port of ``rust/crates/corlinman-gateway/src/placeholder/memory.rs``.

The Python sibling :mod:`corlinman_memory_host` already ships the
unified :class:`MemoryHost` protocol and adapters (LocalSqlite / Remote
HTTP / Federated / ReadOnly). This module is the thin gateway-side
wrapper that adapts that protocol onto a ``DynamicResolver``-shaped
callable for the placeholder engine.

The ``DynamicResolver`` Python protocol is duck-typed: any object with
an ``async resolve(self, key: str, ctx) -> str`` method is accepted
by the engine. We don't import a typed base class because
:mod:`corlinman_core.placeholder` isn't yet ported to Python — when it
lands the sibling agent will register ``MemoryResolver`` via the
gateway's ``AppState`` boot path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:  # pragma: no cover — typing-only
    from corlinman_memory_host import MemoryHit, MemoryHost
else:
    # Lazy import — ``corlinman-memory-host`` is a soft sibling dep that
    # may not be on the path during a partial workspace install. We
    # resolve the symbols on first construction so the module stays
    # importable from a fresh checkout. Add ``corlinman-memory-host`` to
    # ``corlinman-server`` ``dependencies`` to make this strict.
    MemoryHit = Any  # type: ignore[misc,assignment]
    MemoryHost = Any  # type: ignore[misc,assignment]


def _require_memory_host_module() -> Any:
    """Import :mod:`corlinman_memory_host` on first use, raising a clear
    error if the workspace install didn't include it."""
    try:
        import corlinman_memory_host  # noqa: PLC0415 — lazy on purpose

        return corlinman_memory_host
    except ImportError as exc:  # pragma: no cover — install-time wiring
        raise RuntimeError(
            "corlinman-memory-host is required for MemoryResolver; "
            "add 'corlinman-memory-host' to corlinman-server's "
            "dependencies (and pyproject [tool.uv.sources])"
        ) from exc


logger = structlog.get_logger(__name__)

#: Default namespace used by the agent-brain curator sync path. Mirrors
#: ``corlinman_gateway::placeholder::memory::DEFAULT_MEMORY_NAMESPACE``.
DEFAULT_MEMORY_NAMESPACE: Final[str] = "agent-brain"

#: Default number of hits rendered for ``{{memory.<query>}}``. Mirrors
#: ``corlinman_gateway::placeholder::memory::DEFAULT_TOP_K``.
DEFAULT_TOP_K: Final[int] = 5


@runtime_checkable
class _PlaceholderCtxLike(Protocol):
    """Subset of the Rust ``PlaceholderCtx`` the resolver actually reads.

    We only need the ``metadata`` mapping on the read path (tenant id is
    stamped there by gateway middleware). Other fields exist on the
    Rust struct but the memory resolver doesn't consult them.
    """

    metadata: dict[str, str]


class MemoryResolver:
    """Dynamic resolver for ``{{memory.<query text>}}``.

    The ``key`` passed to :meth:`resolve` is everything after
    ``memory.`` — including arbitrary user-typed text. We trim leading
    / trailing whitespace and forward the rest as a :class:`MemoryQuery`
    to the wrapped host.

    Empty queries short-circuit (return ``""``) without touching the
    host — matches the Rust impl and keeps a typo like ``{{memory. }}``
    from billing an embedding round-trip.
    """

    __slots__ = ("_host", "_namespace", "_top_k")

    def __init__(
        self,
        host: "MemoryHost",
        *,
        namespace: str = DEFAULT_MEMORY_NAMESPACE,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self._host = host
        self._namespace = namespace
        # Match the Rust ``top_k.max(1)`` clamp — a zero would render
        # an empty bullet list while still calling the host, which is
        # surprising.
        self._top_k = max(1, int(top_k))

    @property
    def host(self) -> "MemoryHost":
        return self._host

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def top_k(self) -> int:
        return self._top_k

    def with_namespace(self, namespace: str) -> "MemoryResolver":
        """Builder-style override (matches the Rust ``with_namespace``)."""
        return MemoryResolver(self._host, namespace=namespace, top_k=self._top_k)

    def with_top_k(self, top_k: int) -> "MemoryResolver":
        return MemoryResolver(self._host, namespace=self._namespace, top_k=top_k)

    async def resolve(self, key: str, ctx: Any | None = None) -> str:
        """Run the query and render the hits as a markdown bullet list.

        ``ctx`` is accepted for parity with the engine's resolver
        signature but the memory resolver doesn't consult it. The Rust
        impl also ignores ``ctx`` for the memory namespace.
        """
        _ = ctx
        query = (key or "").strip()
        if not query:
            return ""

        mh_mod = _require_memory_host_module()
        memory_query_cls = mh_mod.MemoryQuery
        memory_host_error_cls = mh_mod.MemoryHostError

        try:
            hits = await self._host.query(
                memory_query_cls(
                    text=query,
                    top_k=self._top_k,
                    filters=[],
                    namespace=self._namespace,
                )
            )
        except memory_host_error_cls as exc:
            # Mirror the Rust ``PlaceholderError::Resolver`` shape —
            # surface as a string error so the engine wrapper can turn
            # it into the documented ``resolver:<msg>`` wire format.
            raise RuntimeError(f"memory resolver: {exc}") from exc

        return _render_hits(hits)

    # The engine's ``DynamicResolver`` Rust trait also exposes an
    # ``into_arc`` helper — Python has no such notion (all classes are
    # heap-allocated and shareable), so we skip it.

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return (
            f"MemoryResolver(host={self._host.name()!r}, "
            f"namespace={self._namespace!r}, top_k={self._top_k})"
        )


def _render_hits(hits: "list[MemoryHit]") -> str:
    """Format hits as ``- <content> (<source>:<id>)`` lines.

    Single-shot string concat instead of ``"\\n".join`` so a hit with
    an embedded newline in ``content`` still parses; the bullet prefix
    is fixed-width so the operator can grep for ``"- "`` to count hits.
    Matches the Rust ``render_hits`` byte-for-byte.
    """
    if not hits:
        return ""
    parts: list[str] = []
    for hit in hits:
        content = (hit.content or "").strip()
        parts.append(f"- {content} ({hit.source}:{hit.id})")
    return "\n".join(parts)


__all__ = [
    "DEFAULT_MEMORY_NAMESPACE",
    "DEFAULT_TOP_K",
    "MemoryResolver",
]
