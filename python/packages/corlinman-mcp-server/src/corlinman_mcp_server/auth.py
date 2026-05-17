"""Pre-upgrade auth + per-token ACL.

Mirrors the Rust ``server::auth`` module. :func:`resolve_token` is the
pre-upgrade entry point: given a query-string ``token`` value, it walks
the configured ACL list and returns the matching :class:`TokenAcl`. The
transport uses this to decide pre-upgrade 401 vs WS upgrade, and to
stamp the resolved ACL onto the per-connection :class:`SessionContext`
so adapters consult the same ACL on every method call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters import SessionContext

DEFAULT_TENANT_ID: str = "default"
"""Default tenant id when a token is configured without ``tenant_id``."""


@dataclass
class TokenAcl:
    """One accepted bearer-token + the per-capability allowlist that
    bounds what the holder can do on the wire.

    Empty allowlists fail closed (no method allowed). ``["*"]`` fails
    open for that one capability. Globs are the same single-``*`` shape
    implemented in :func:`corlinman_mcp_server.adapters.glob_match`.
    """

    token: str
    """Opaque bearer string. Compared byte-for-byte; no hashing in C1."""

    label: str = "permissive"
    """Free-form label for logging / metrics."""

    tools_allowlist: list[str] = field(default_factory=list)
    """Glob patterns against ``<plugin>:<tool>``."""

    resources_allowed: list[str] = field(default_factory=list)
    """URI-scheme prefixes (``"memory"``, ``"skill"``, ``"persona"``)."""

    prompts_allowed: list[str] = field(default_factory=list)
    """Skill-name globs surfaced as MCP prompts."""

    tenant_id: str | None = None
    """Tenant id this token's memory reads route to. ``None`` →
    fallback to :data:`DEFAULT_TENANT_ID`."""

    @classmethod
    def permissive(cls, token: str) -> TokenAcl:
        """Build a permissive ACL — every capability set to ``["*"]``.
        Convenient for tests; production tokens should narrow this.
        """
        return cls(
            token=token,
            label="permissive",
            tools_allowlist=["*"],
            resources_allowed=["*"],
            prompts_allowed=["*"],
            tenant_id=None,
        )

    def effective_tenant(self) -> str:
        """Resolve the effective tenant id, applying the
        :data:`DEFAULT_TENANT_ID` fallback (empty strings count as
        missing, matching the Rust impl)."""
        if self.tenant_id and self.tenant_id != "":
            return self.tenant_id
        return DEFAULT_TENANT_ID

    def to_session_context(self) -> SessionContext:
        """Build the per-session :class:`SessionContext` this token
        grants."""
        return SessionContext(
            tools_allowlist=list(self.tools_allowlist),
            resources_allowed=list(self.resources_allowed),
            prompts_allowed=list(self.prompts_allowed),
            tenant_id=self.effective_tenant(),
        )


def resolve_token(acls: list[TokenAcl], presented: str) -> TokenAcl | None:
    """Look up the :class:`TokenAcl` matching ``presented``. Empty
    ``acls`` fails closed (no token resolves)."""
    if not presented:
        return None
    for acl in acls:
        if acl.token == presented:
            return acl
    return None


__all__ = [
    "DEFAULT_TENANT_ID",
    "TokenAcl",
    "resolve_token",
]
