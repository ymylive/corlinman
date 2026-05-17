"""Protocol seams the capability adapters bridge against.

The Rust crate depends directly on the workspace types
``PluginRegistry``, ``PluginRuntime``, ``MemoryHost`` and
``SkillRegistry``. The Python plane's equivalents are split across
:mod:`corlinman_skills_registry` (W1, available) and an in-progress
memory host package (W2). To keep this package usable *now*, regardless
of which dependency has landed, every external surface this server
needs is declared here as a :pep:`544` :class:`~typing.Protocol`.

Concrete wiring layers (e.g. the gateway integration) supply objects
matching these shapes. Adapter unit tests stub the protocols
in-process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Protocol, runtime_checkable

from .types import JsonValue


# ---------------------------------------------------------------------
# Plugin manifest / registry / runtime
# ---------------------------------------------------------------------


@dataclass
class PluginTool:
    """One tool entry on a plugin manifest. Mirrors the Rust
    ``corlinman_plugins::manifest::Tool`` shape."""

    name: str
    description: str = ""
    parameters: JsonValue = field(default_factory=dict)


@dataclass
class PluginEntryPoint:
    """``[entry_point]`` block on a plugin manifest."""

    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class PluginCommunication:
    """``[communication]`` block on a plugin manifest."""

    timeout_ms: int | None = None


@dataclass
class PluginCapabilities:
    """``[capabilities]`` block on a plugin manifest."""

    tools: list[PluginTool] = field(default_factory=list)
    disable_model_invocation: bool = False


@dataclass
class PluginManifest:
    """Minimal plugin manifest shape the MCP server consults.

    Only the fields the tools adapter reads are modelled; concrete
    implementations may carry richer payloads.
    """

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    entry_point: PluginEntryPoint = field(default_factory=PluginEntryPoint)
    communication: PluginCommunication = field(default_factory=PluginCommunication)
    capabilities: PluginCapabilities = field(default_factory=PluginCapabilities)


@dataclass
class PluginEntry:
    """One entry in the :class:`PluginRegistry`."""

    manifest: PluginManifest
    manifest_path: Path = field(default_factory=Path)

    def plugin_dir(self) -> Path:
        """The directory containing the plugin manifest. Used by the
        tools adapter as the runtime's ``cwd``."""
        return self.manifest_path.parent if self.manifest_path != Path() else Path(".")


@runtime_checkable
class PluginRegistry(Protocol):
    """Read-only view over discovered plugins. Mirrors the slice of
    the Rust ``PluginRegistry`` surface the MCP server consumes."""

    def list(self) -> Iterable[PluginEntry]:  # pragma: no cover — Protocol
        """Yield every registered plugin entry."""
        ...

    def get(self, name: str) -> PluginEntry | None:  # pragma: no cover — Protocol
        """Look up a plugin by manifest ``name``."""
        ...


@dataclass
class PluginInput:
    """Per-call input handed to :meth:`PluginRuntime.execute`."""

    plugin: str
    tool: str
    args_json: bytes
    call_id: str
    session_key: str = "mcp"
    trace_id: str = ""
    cwd: Path = field(default_factory=Path)
    env: dict[str, str] = field(default_factory=dict)
    deadline_ms: int | None = None


@dataclass
class PluginOutputSuccess:
    """Successful tool invocation."""

    content: bytes
    duration_ms: int = 0


@dataclass
class PluginOutputError:
    """Tool returned an error (runtime failure, not infrastructure)."""

    code: int
    message: str
    duration_ms: int = 0


@dataclass
class PluginOutputAcceptedForLater:
    """Tool kicked off an async task; ``task_id`` returned."""

    task_id: str
    duration_ms: int = 0


PluginOutput = PluginOutputSuccess | PluginOutputError | PluginOutputAcceptedForLater


@runtime_checkable
class ProgressSink(Protocol):
    """Per-call progress sink. Mirrors the Rust
    ``corlinman_plugins::runtime::ProgressSink`` trait."""

    async def emit(self, message: str, fraction: float | None) -> None: ...


class CancellationToken:
    """Minimal cancellation token. Mirrors the slice of
    ``tokio_util::sync::CancellationToken`` the adapters use."""

    __slots__ = ("_cancelled", "_parent")

    def __init__(self, parent: CancellationToken | None = None) -> None:
        self._cancelled: bool = False
        self._parent: CancellationToken | None = parent

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        if self._cancelled:
            return True
        if self._parent is not None and self._parent.is_cancelled():
            return True
        return False

    def child_token(self) -> CancellationToken:
        return CancellationToken(parent=self)


@runtime_checkable
class PluginRuntime(Protocol):
    """The runtime that actually executes a plugin tool. Mirrors the
    Rust ``corlinman_plugins::runtime::PluginRuntime`` trait."""

    async def execute(
        self,
        input: PluginInput,
        progress: ProgressSink | None,
        cancel: CancellationToken,
    ) -> PluginOutput:  # pragma: no cover — Protocol
        ...

    def kind(self) -> str:  # pragma: no cover — Protocol
        ...


# ---------------------------------------------------------------------
# Memory host
# ---------------------------------------------------------------------


@dataclass
class MemoryQuery:
    """Per-call query handed to :meth:`MemoryHost.query`."""

    text: str
    top_k: int = 10
    filters: list[JsonValue] = field(default_factory=list)
    namespace: str | None = None


@dataclass
class MemoryDoc:
    """A document to upsert into a memory host."""

    id: str | None = None
    content: str = ""
    metadata: JsonValue = field(default_factory=dict)


@dataclass
class MemoryHit:
    """One hit returned from :meth:`MemoryHost.query` or :meth:`MemoryHost.get`."""

    id: str
    content: str
    score: float = 0.0
    source: str = ""
    metadata: JsonValue = None


@runtime_checkable
class MemoryHost(Protocol):
    """Read/write surface over one memory backend. The MCP resources
    adapter only consumes :meth:`name`, :meth:`query` and :meth:`get`;
    the other methods are part of the wider contract."""

    def name(self) -> str: ...

    async def query(self, req: MemoryQuery) -> list[MemoryHit]: ...

    async def upsert(self, doc: MemoryDoc) -> str: ...

    async def delete(self, id: str) -> None: ...

    async def get(self, id: str) -> MemoryHit | None: ...


# ---------------------------------------------------------------------
# Persona snapshot provider
# ---------------------------------------------------------------------


@runtime_checkable
class PersonaSnapshotProvider(Protocol):
    """Pluggable persona-snapshot reader. ``PersonaStore`` lives in
    the Python tier; the protocol gives us a forward-compatible seam
    so a real provider can be wired without touching the adapter."""

    async def list_user_ids(self) -> list[str]: ...

    async def read_snapshot(self, user_id: str) -> JsonValue | None: ...


class NullPersonaProvider:
    """No-op persona provider. Surfaces an empty list and never finds a
    snapshot. Default when the gateway hasn't wired a real provider."""

    async def list_user_ids(self) -> list[str]:
        return []

    async def read_snapshot(self, user_id: str) -> JsonValue | None:
        return None


# ---------------------------------------------------------------------
# Skill registry — minimal protocol so this package doesn't hard-require
# corlinman-skills-registry to be importable for type checking.
# ---------------------------------------------------------------------


@runtime_checkable
class SkillEntry(Protocol):
    """Slice of the :class:`corlinman_skills_registry.Skill` shape the
    MCP adapters consume."""

    name: str
    description: str
    body_markdown: str


@runtime_checkable
class SkillRegistry(Protocol):
    """Slice of the :class:`corlinman_skills_registry.SkillRegistry`
    surface the MCP adapters consume."""

    def get(self, name: str) -> SkillEntry | None: ...

    def __iter__(self) -> Iterable[SkillEntry]: ...


# Synonym so callers can pass a callable in place of a registry when
# convenient.
SkillRegistryLike = SkillRegistry | Callable[[], Awaitable[list[SkillEntry]]]


__all__ = [
    "CancellationToken",
    "MemoryDoc",
    "MemoryHit",
    "MemoryHost",
    "MemoryQuery",
    "NullPersonaProvider",
    "PersonaSnapshotProvider",
    "PluginCapabilities",
    "PluginCommunication",
    "PluginEntry",
    "PluginEntryPoint",
    "PluginInput",
    "PluginManifest",
    "PluginOutput",
    "PluginOutputAcceptedForLater",
    "PluginOutputError",
    "PluginOutputSuccess",
    "PluginRegistry",
    "PluginRuntime",
    "PluginTool",
    "ProgressSink",
    "SkillEntry",
    "SkillRegistry",
    "SkillRegistryLike",
]
