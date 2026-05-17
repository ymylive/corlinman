"""Core data types shared by every :class:`MemoryHost` adapter.

Python port of the top-level types defined in
``rust/crates/corlinman-memory-host/src/lib.rs``. The serde
``#[serde(tag = "kind", rename_all = "snake_case")]`` shape used by
``MemoryFilter`` is preserved by :func:`MemoryFilter.to_json` /
:func:`MemoryFilter.from_json` so wire compatibility with the Rust
service is byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MemoryHostError(Exception):
    """Base class for every error raised by a :class:`MemoryHost` adapter.

    Rust uses ``anyhow::Result`` and ``anyhow::Error`` for the whole
    surface; the Python port collapses that to a single concrete
    exception type so callers can ``except MemoryHostError`` and still
    introspect ``__cause__`` for transport-level failures (mirroring
    ``anyhow``'s context chain).
    """


# ---------------------------------------------------------------------------
# Query / Hit / Doc / Filter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryFilter:
    """Structured filter predicate pushed down to a host.

    Rust uses a ``non_exhaustive`` enum with three variants
    (``TagEq`` / ``TagIn`` / ``CreatedAfter``). Python collapses that
    onto a single dataclass with a discriminator field — adapters
    should ``match`` on ``kind`` and treat unknown future variants as
    "no-op / log and skip", same as the Rust contract.
    """

    kind: str
    # Per-variant fields. Set only the ones relevant to ``kind``.
    tag: str | None = None
    value: str | None = None
    values: tuple[str, ...] | None = None
    unix: int | None = None

    @classmethod
    def tag_eq(cls, tag: str, value: str) -> MemoryFilter:
        return cls(kind="tag_eq", tag=tag, value=value)

    @classmethod
    def tag_in(cls, tag: str, values: list[str] | tuple[str, ...]) -> MemoryFilter:
        return cls(kind="tag_in", tag=tag, values=tuple(values))

    @classmethod
    def created_after(cls, unix: int) -> MemoryFilter:
        return cls(kind="created_after", unix=unix)

    def to_json(self) -> dict[str, Any]:
        """Encode to the same ``{"kind": "...", ...}`` JSON the Rust
        ``serde`` derives produce."""
        out: dict[str, Any] = {"kind": self.kind}
        if self.kind == "tag_eq":
            out["tag"] = self.tag
            out["value"] = self.value
        elif self.kind == "tag_in":
            out["tag"] = self.tag
            out["values"] = list(self.values or ())
        elif self.kind == "created_after":
            out["unix"] = self.unix
        else:
            # Future variant — emit any non-None fields verbatim so a
            # round-trip through JSON stays lossless.
            if self.tag is not None:
                out["tag"] = self.tag
            if self.value is not None:
                out["value"] = self.value
            if self.values is not None:
                out["values"] = list(self.values)
            if self.unix is not None:
                out["unix"] = self.unix
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> MemoryFilter:
        kind = raw.get("kind")
        if not isinstance(kind, str):
            raise MemoryHostError("MemoryFilter.from_json: missing 'kind'")
        if kind == "tag_eq":
            return cls.tag_eq(str(raw.get("tag", "")), str(raw.get("value", "")))
        if kind == "tag_in":
            values = raw.get("values") or []
            if not isinstance(values, list):
                raise MemoryHostError("MemoryFilter.from_json: 'values' must be list")
            return cls.tag_in(str(raw.get("tag", "")), [str(v) for v in values])
        if kind == "created_after":
            unix = raw.get("unix", 0)
            return cls.created_after(int(unix))
        # Forward-compat: accept unknown variant, store fields opaquely.
        return cls(
            kind=kind,
            tag=raw.get("tag"),
            value=raw.get("value"),
            values=tuple(raw["values"]) if isinstance(raw.get("values"), list) else None,
            unix=raw.get("unix"),
        )


@dataclass
class MemoryQuery:
    """Query into a memory host.

    Wire shape mirrors the Rust ``MemoryQuery`` (``#[serde(default)]``
    on ``filters`` and ``namespace``)."""

    text: str
    top_k: int
    filters: list[MemoryFilter] = field(default_factory=list)
    namespace: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text, "top_k": self.top_k}
        if self.filters:
            out["filters"] = [f.to_json() for f in self.filters]
        if self.namespace is not None:
            out["namespace"] = self.namespace
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> MemoryQuery:
        if not isinstance(raw, dict):
            raise MemoryHostError("MemoryQuery.from_json: expected object")
        filters_raw = raw.get("filters") or []
        filters = [MemoryFilter.from_json(f) for f in filters_raw]
        return cls(
            text=str(raw.get("text", "")),
            top_k=int(raw.get("top_k", 0)),
            filters=filters,
            namespace=raw.get("namespace"),
        )


@dataclass
class MemoryHit:
    """A single hit returned by a memory host.

    ``source`` is set to the originating :meth:`MemoryHost.name`."""

    id: str
    content: str
    score: float
    source: str
    metadata: Any = None  # arbitrary JSON value; default Null

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "score": self.score,
            "source": self.source,
            "metadata": self.metadata,
        }


@dataclass
class MemoryDoc:
    """A document to upsert into a memory host."""

    content: str
    metadata: Any = None
    namespace: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"content": self.content}
        # Rust uses ``#[serde(default)]`` so absent fields are emitted as
        # their defaults (Null / None). Match by emitting them
        # unconditionally — keeps the wire shape predictable for the
        # ``RemoteHttpHost`` mock servers.
        out["metadata"] = self.metadata
        out["namespace"] = self.namespace
        return out


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class _HealthKind(Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True)
class HealthStatus:
    """Lightweight health signal surfaced by :meth:`MemoryHost.health`.

    Use the class methods :meth:`ok`, :meth:`degraded`, :meth:`down` to
    construct the three variants the Rust enum surfaces."""

    kind: _HealthKind
    detail: str = ""

    @classmethod
    def ok(cls) -> HealthStatus:
        return cls(_HealthKind.OK, "")

    @classmethod
    def degraded(cls, detail: str) -> HealthStatus:
        return cls(_HealthKind.DEGRADED, detail)

    @classmethod
    def down(cls, detail: str) -> HealthStatus:
        return cls(_HealthKind.DOWN, detail)

    def is_ok(self) -> bool:
        return self.kind is _HealthKind.OK

    def is_degraded(self) -> bool:
        return self.kind is _HealthKind.DEGRADED

    def is_down(self) -> bool:
        return self.kind is _HealthKind.DOWN

    def __repr__(self) -> str:
        if self.kind is _HealthKind.OK:
            return "HealthStatus.ok()"
        return f"HealthStatus.{self.kind.value}({self.detail!r})"


__all__ = [
    "HealthStatus",
    "MemoryDoc",
    "MemoryFilter",
    "MemoryHit",
    "MemoryHostError",
    "MemoryQuery",
]
