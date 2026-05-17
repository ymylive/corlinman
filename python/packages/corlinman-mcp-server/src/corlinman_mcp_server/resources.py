"""``resources`` capability adapter — read-only surface over memory
hosts + skill bodies.

Mirrors the Rust ``adapters::resources`` module 1:1. URI schemes:

============================================  =================  ====  ====
URI scheme                                    Source             List  Read
============================================  =================  ====  ====
``corlinman://memory/<host>/<id>``            ``MemoryHost``     yes   yes
``corlinman://skill/<name>``                  ``SkillRegistry``  yes   yes
``corlinman://persona/<user_id>/snapshot``    pluggable          yes   yes
============================================  =================  ====  ====
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

import structlog

from .adapters import CapabilityAdapter, SessionContext
from .bridges import (
    MemoryHost,
    MemoryQuery,
    NullPersonaProvider,
    PersonaSnapshotProvider,
    SkillRegistry,
)
from .errors import (
    McpInternalError,
    McpInvalidParamsError,
    McpMethodNotFoundError,
)
from .types import (
    JsonValue,
    Resource,
    ResourcesListParams,
    ResourcesListResult,
    ResourcesReadParams,
    ResourcesReadResult,
    TextResourceContent,
)

log = structlog.get_logger(__name__)

METHOD_LIST: str = "resources/list"
METHOD_READ: str = "resources/read"

DEFAULT_PAGE_SIZE: int = 100
"""Default cursor page size."""

DEFAULT_MEMORY_LIST_LIMIT: int = 50
"""Soft cap on memory hits returned per host during enumeration."""


# ---------------------------------------------------------------------
# Parsed URI
# ---------------------------------------------------------------------


@dataclass
class _MemoryUri:
    host: str
    id: str


@dataclass
class _SkillUri:
    name: str


@dataclass
class _PersonaUri:
    user_id: str


_ParsedUri = _MemoryUri | _SkillUri | _PersonaUri


def _parse_uri(uri: str) -> _ParsedUri | None:
    """Parse a ``corlinman://...`` URI. Returns ``None`` for unknown
    shapes."""
    prefix = "corlinman://"
    if not uri.startswith(prefix):
        return None
    rest = uri[len(prefix):]

    if rest.startswith("memory/"):
        after = rest[len("memory/"):]
        if "/" not in after:
            return None
        host, id_ = after.split("/", 1)
        if not host or not id_:
            return None
        return _MemoryUri(host=host, id=id_)

    if rest.startswith("skill/"):
        name = rest[len("skill/"):]
        if not name:
            return None
        return _SkillUri(name=name)

    if rest.startswith("persona/"):
        after = rest[len("persona/"):]
        if "/" not in after:
            return None
        user_id, tail = after.split("/", 1)
        if not user_id or tail != "snapshot":
            return None
        return _PersonaUri(user_id=user_id)

    return None


def _parsed_scheme(parsed: _ParsedUri) -> str:
    if isinstance(parsed, _MemoryUri):
        return "memory"
    if isinstance(parsed, _SkillUri):
        return "skill"
    return "persona"


def _short_preview(s: str) -> str | None:
    """First 80 chars of ``s``, with a trailing ellipsis when
    truncated. Mirrors Rust ``short_preview``."""
    trimmed = s.strip()
    if not trimmed:
        return None
    out_chars: list[str] = []
    for i, ch in enumerate(trimmed):
        if i >= 80:
            out_chars.append("…")
            return "".join(out_chars)
        out_chars.append(ch)
    return "".join(out_chars)


# ---------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------


class ResourcesAdapter:
    """Adapter that maps a set of memory hosts + the skill registry +
    (optionally) a persona provider onto MCP's ``resources/*`` surface.

    Mirrors the Rust ``ResourcesAdapter`` 1:1.
    """

    def __init__(
        self,
        memory_hosts: dict[str, MemoryHost],
        skills: SkillRegistry,
    ) -> None:
        # Sorted on insertion for stable iteration order — matches the
        # Rust ``BTreeMap`` semantics.
        self._memory_hosts: dict[str, MemoryHost] = {
            k: memory_hosts[k] for k in sorted(memory_hosts.keys())
        }
        self._skills: SkillRegistry = skills
        self._persona: PersonaSnapshotProvider = NullPersonaProvider()
        self._page_size: int = DEFAULT_PAGE_SIZE
        self._memory_list_limit: int = DEFAULT_MEMORY_LIST_LIMIT

    def with_persona(self, persona: PersonaSnapshotProvider) -> ResourcesAdapter:
        self._persona = persona
        return self

    def with_page_size(self, n: int) -> ResourcesAdapter:
        self._page_size = max(1, n)
        return self

    def with_memory_list_limit(self, n: int) -> ResourcesAdapter:
        self._memory_list_limit = max(1, n)
        return self

    # ------------------------------------------------------------------
    # CapabilityAdapter protocol
    # ------------------------------------------------------------------

    def capability_name(self) -> str:
        return "resources"

    async def handle(
        self,
        method: str,
        params: JsonValue,
        ctx: SessionContext,
    ) -> JsonValue:
        if method == METHOD_LIST:
            if params is None:
                parsed_params = ResourcesListParams(cursor=None)
            else:
                try:
                    parsed_params = ResourcesListParams.model_validate(params)
                except Exception as e:
                    raise McpInvalidParamsError(
                        f"resources/list: bad params: {e}"
                    ) from e
            list_result = await self.list_resources(parsed_params, ctx)
            return list_result.model_dump()
        if method == METHOD_READ:
            try:
                parsed_read = ResourcesReadParams.model_validate(params or {})
            except Exception as e:
                raise McpInvalidParamsError(
                    f"resources/read: bad params: {e}"
                ) from e
            read_result = await self.read_resource(parsed_read, ctx)
            return read_result.model_dump()
        raise McpMethodNotFoundError(method)

    # ------------------------------------------------------------------
    # resources/list
    # ------------------------------------------------------------------

    async def list_resources(
        self,
        params: ResourcesListParams,
        ctx: SessionContext,
    ) -> ResourcesListResult:
        """Enumerate all visible resources, then page-slice."""
        all_resources: list[Resource] = []

        # 1) Memory hosts (alphabetical thanks to sorted dict).
        if ctx.allows_resource_scheme("memory"):
            for name, host in self._memory_hosts.items():
                probe = MemoryQuery(
                    text="*",
                    top_k=self._memory_list_limit,
                    filters=[],
                    namespace=None,
                )
                try:
                    hits = await host.query(probe)
                except Exception:  # noqa: BLE001 — degraded host shouldn't blow up the list
                    hits = []
                for hit in hits:
                    all_resources.append(
                        Resource(
                            uri=f"corlinman://memory/{name}/{hit.id}",
                            name=f"memory:{name}:{hit.id}",
                            description=_short_preview(hit.content),
                            mimeType="text/plain",
                        )
                    )

        # 2) Skills.
        if ctx.allows_resource_scheme("skill"):
            for skill in self._iter_skills():
                all_resources.append(
                    Resource(
                        uri=f"corlinman://skill/{skill.name}",
                        name=f"skill:{skill.name}",
                        description=(skill.description or None) or None,
                        mimeType="text/markdown",
                    )
                )

        # 3) Persona snapshots.
        if ctx.allows_resource_scheme("persona"):
            try:
                ids = await self._persona.list_user_ids()
            except Exception:  # noqa: BLE001 — fail-soft same as memory
                ids = []
            for uid in ids:
                all_resources.append(
                    Resource(
                        uri=f"corlinman://persona/{uid}/snapshot",
                        name=f"persona:{uid}",
                        description=f"trait snapshot for {uid}",
                        mimeType="application/json",
                    )
                )

        all_resources.sort(key=lambda r: r.uri)

        # Cursor parse.
        cursor = params.cursor
        if cursor is None or cursor == "":
            offset = 0
        else:
            try:
                offset = int(cursor)
                if offset < 0:
                    raise ValueError("negative")
            except (TypeError, ValueError) as e:
                raise McpInvalidParamsError(
                    f"invalid resources cursor '{cursor}'",
                    data={"cursor": cursor},
                ) from e

        total = len(all_resources)
        end = min(offset + self._page_size, total)
        page = all_resources[offset:end] if offset < total else []
        next_cursor: str | None = str(end) if end < total else None
        return ResourcesListResult(resources=page, nextCursor=next_cursor)

    # ------------------------------------------------------------------
    # resources/read
    # ------------------------------------------------------------------

    async def read_resource(
        self,
        params: ResourcesReadParams,
        ctx: SessionContext,
    ) -> ResourcesReadResult:
        parsed = _parse_uri(params.uri)
        if parsed is None:
            raise McpInvalidParamsError(
                f"not a corlinman resource URI: '{params.uri}'",
                data={"uri": params.uri},
            )

        scheme = _parsed_scheme(parsed)
        if not ctx.allows_resource_scheme(scheme):
            raise McpInvalidParamsError(
                f"resource scheme '{scheme}' not allowed by this token",
                data={"uri": params.uri},
            )

        if isinstance(parsed, _MemoryUri):
            host = self._memory_hosts.get(parsed.host)
            if host is None:
                raise McpInvalidParamsError(
                    f"unknown memory host '{parsed.host}'",
                    data={"uri": params.uri},
                )
            try:
                hit = await host.get(parsed.id)
            except Exception as e:
                raise McpInternalError(f"memory host get: {e}") from e
            if hit is None:
                raise McpInvalidParamsError(
                    f"unknown memory id '{parsed.id}'",
                    data={"uri": params.uri},
                )
            return ResourcesReadResult(
                contents=[
                    TextResourceContent(
                        uri=params.uri,
                        mimeType="text/plain",
                        text=hit.content,
                    )
                ]
            )

        if isinstance(parsed, _SkillUri):
            skill = self._skills.get(parsed.name)
            if skill is None:
                raise McpInvalidParamsError(
                    f"unknown skill '{parsed.name}'",
                    data={"uri": params.uri},
                )
            return ResourcesReadResult(
                contents=[
                    TextResourceContent(
                        uri=params.uri,
                        mimeType="text/markdown",
                        text=skill.body_markdown,
                    )
                ]
            )

        # Persona
        try:
            snap = await self._persona.read_snapshot(parsed.user_id)
        except Exception as e:
            raise McpInternalError(f"persona snapshot: {e}") from e
        if snap is None:
            raise McpInvalidParamsError(
                f"unknown persona user '{parsed.user_id}'",
                data={"uri": params.uri},
            )
        try:
            text = json.dumps(snap, ensure_ascii=False)
        except (TypeError, ValueError):
            text = "{}"
        return ResourcesReadResult(
            contents=[
                TextResourceContent(
                    uri=params.uri,
                    mimeType="application/json",
                    text=text,
                )
            ]
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _iter_skills(self) -> Iterable:
        """Iterate the skill registry tolerantly. The registry can be
        any object exposing ``__iter__`` or an explicit ``iter()`` method
        (matching :class:`corlinman_skills_registry.SkillRegistry`)."""
        if hasattr(self._skills, "iter") and callable(self._skills.iter):
            return self._skills.iter()
        return iter(self._skills)


__all__ = [
    "DEFAULT_MEMORY_LIST_LIMIT",
    "DEFAULT_PAGE_SIZE",
    "METHOD_LIST",
    "METHOD_READ",
    "ResourcesAdapter",
]
