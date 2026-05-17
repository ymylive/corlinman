"""Capability-adapter substrate: :class:`SessionContext`, glob matcher,
and the :class:`CapabilityAdapter` :pep:`544` :class:`~typing.Protocol`.

Concrete adapter implementations live in :mod:`tools`,
:mod:`resources` and :mod:`prompts`. The dispatcher in :mod:`dispatch`
holds a dict of capability name → adapter and routes by method prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import McpError
from .types import JsonValue


@dataclass
class SessionContext:
    """Per-session view passed into every adapter call.

    Mirrors the Rust ``SessionContext`` struct field-for-field. The
    iter-8 fields (allowlists + ``tenant_id``) are all that the C1
    adapters consume.
    """

    tools_allowlist: list[str] = field(default_factory=list)
    """Glob patterns the token is allowed to invoke under ``tools/*``.
    Empty list → no tools allowed (fail-closed). ``["*"]`` → all tools
    allowed (fail-open)."""

    resources_allowed: list[str] = field(default_factory=list)
    """URI-scheme prefixes the token may read under ``resources/*``."""

    prompts_allowed: list[str] = field(default_factory=list)
    """Skill-name globs the token may surface as prompts."""

    tenant_id: str | None = None
    """Tenant id passed through to memory-host queries. ``None``
    defaults to the workspace's default tenant in the auth layer."""

    @classmethod
    def permissive(cls) -> SessionContext:
        """A context that allows everything. Used by dispatcher tests
        that aren't exercising the ACL."""
        return cls(
            tools_allowlist=["*"],
            resources_allowed=["*"],
            prompts_allowed=["*"],
            tenant_id=None,
        )

    @staticmethod
    def allows(allowlist: list[str], name: str) -> bool:
        """Test if ``name`` is allowed under ``allowlist``. Empty
        allowlist → always denied (fail-closed). ``*`` matches anything;
        ``prefix.*`` matches anything starting with ``prefix.`` (etc.).
        """
        if not allowlist:
            return False
        return any(glob_match(p, name) for p in allowlist)

    def allows_tool(self, name: str) -> bool:
        return SessionContext.allows(self.tools_allowlist, name)

    def allows_resource_scheme(self, scheme: str) -> bool:
        return SessionContext.allows(self.resources_allowed, scheme)

    def allows_prompt(self, name: str) -> bool:
        return SessionContext.allows(self.prompts_allowed, name)


def glob_match(pattern: str, name: str) -> bool:
    """Tiny glob matcher: ``*`` is the only wildcard, matches any run
    of characters (including the empty string). No character classes,
    no ``?``. Mirrors the Rust ``glob_match`` exactly so the same
    allowlist patterns work in either implementation.
    """
    # Fast paths.
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == name

    parts = pattern.split("*")
    cursor = 0
    last = len(parts) - 1
    for i, piece in enumerate(parts):
        if piece == "":
            continue
        if i == 0:
            if not name[cursor:].startswith(piece):
                return False
            cursor += len(piece)
        elif i == last and not pattern.endswith("*"):
            if not name[cursor:].endswith(piece):
                return False
            # Anchored at the end — done.
            return len(name) >= cursor + len(piece)
        else:
            rel = name[cursor:].find(piece)
            if rel < 0:
                return False
            cursor += rel + len(piece)
    return True


@runtime_checkable
class CapabilityAdapter(Protocol):
    """One capability family's worth of MCP method routing.

    The dispatcher holds a ``dict[str, CapabilityAdapter]`` keyed by
    :meth:`capability_name`. Methods starting with
    ``<capability_name>/`` are routed to that adapter.

    Implementations are async — the dispatcher awaits :meth:`handle`
    directly. Adapters parse ``params`` themselves so they can emit
    precise :class:`~corlinman_mcp_server.errors.McpInvalidParamsError`
    payloads.
    """

    def capability_name(self) -> str:  # pragma: no cover — Protocol
        """Capability family this adapter handles. One of ``"tools"``,
        ``"resources"``, ``"prompts"``."""
        ...

    async def handle(
        self,
        method: str,
        params: JsonValue,
        ctx: SessionContext,
    ) -> JsonValue:  # pragma: no cover — Protocol
        """Handle one method call. Raises an :class:`McpError` subclass
        on failure; returns the JSON-RPC ``result`` value on success."""
        ...


__all__ = [
    "CapabilityAdapter",
    "McpError",
    "SessionContext",
    "glob_match",
]
