"""Plugin registry: deduped, origin-ranked view of discovered manifests.

Python port of ``rust/crates/corlinman-plugins/src/registry.rs``. The registry
is async-safe (writes serialised through an :class:`asyncio.Lock`); reads
return owned copies of the entry so callers may safely hold them across
``await`` points without risking inconsistent state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import (
    DiscoveredPlugin,
    Origin,
    SearchRoot,
    discover,
)
from .manifest import PluginManifest


@dataclass
class PluginEntry:
    """One resolved plugin entry, keyed by ``manifest.name`` in the registry."""

    manifest: PluginManifest
    origin: Origin
    manifest_path: Path
    #: Number of competing manifests this entry shadowed.
    shadowed_count: int = 0

    def plugin_dir(self) -> Path:
        return self.manifest_path.parent


@dataclass
class ParseErrorDiagnostic:
    """A manifest failed to parse."""

    path: Path
    origin: Origin
    message: str


@dataclass
class NameCollisionDiagnostic:
    """Two manifests claim the same plugin name; ``loser`` was dropped."""

    name: str
    winner: Path
    winner_origin: Origin
    loser: Path
    loser_origin: Origin


Diagnostic = ParseErrorDiagnostic | NameCollisionDiagnostic


def _resolve(
    plugins: list[DiscoveredPlugin],
) -> tuple[dict[str, PluginEntry], list[Diagnostic]]:
    """Apply origin-rank dedup.

    Higher rank wins. On equal rank, the manifest discovered first wins.
    Mirrors the Rust ``resolve`` helper byte-for-byte semantically.
    """
    # Descending rank ensures the first insertion for any given name is the
    # eventual winner; subsequent dupes register as losers.
    plugins_sorted = sorted(plugins, key=lambda p: p.origin.rank, reverse=True)

    out: dict[str, PluginEntry] = {}
    diags: list[Diagnostic] = []

    for p in plugins_sorted:
        name = p.manifest.name
        existing = out.get(name)
        if existing is not None:
            existing.shadowed_count += 1
            diags.append(
                NameCollisionDiagnostic(
                    name=name,
                    winner=existing.manifest_path,
                    winner_origin=existing.origin,
                    loser=p.manifest_path,
                    loser_origin=p.origin,
                )
            )
            continue

        out[name] = PluginEntry(
            manifest=p.manifest,
            origin=p.origin,
            manifest_path=p.manifest_path,
            shadowed_count=0,
        )

    return out, diags


@dataclass
class PluginRegistry:
    """Async-safe in-memory plugin registry.

    Reader contract: :meth:`list` / :meth:`get` / :meth:`diagnostics` return
    owned copies under a brief lock. Writers (:meth:`upsert`, :meth:`remove`,
    :meth:`set_diagnostics`) take a write lock and release before returning.
    """

    _entries: dict[str, PluginEntry] = field(default_factory=dict)
    _diagnostics: list[Diagnostic] = field(default_factory=list)
    _roots: list[SearchRoot] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # -- Construction --

    @classmethod
    def from_roots(cls, roots: list[SearchRoot]) -> PluginRegistry:
        """Build a registry by running discovery eagerly over ``roots``."""
        result = discover(roots)
        entries, dedup_diags = _resolve(result.plugins)

        diagnostics: list[Diagnostic] = [
            ParseErrorDiagnostic(path=d.path, origin=d.origin, message=d.message)
            for d in result.diagnostics
        ]
        diagnostics.extend(dedup_diags)

        return cls(
            _entries=entries,
            _diagnostics=diagnostics,
            _roots=list(roots),
        )

    # -- Readers (sync; brief copy under nothing — Python's GIL serialises
    #    dict reads, and writers always take the lock first) --

    def list(self) -> list[PluginEntry]:
        """All registered plugins, sorted alphabetically by name."""
        return sorted(self._entries.values(), key=lambda e: e.manifest.name)

    def get(self, name: str) -> PluginEntry | None:
        return self._entries.get(name)

    def diagnostics(self) -> list[Diagnostic]:
        return list(self._diagnostics)

    def roots(self) -> list[SearchRoot]:
        return list(self._roots)

    def __len__(self) -> int:
        return len(self._entries)

    def is_empty(self) -> bool:
        return not self._entries

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._entries

    # -- Writers (async; serialised) --

    async def upsert(self, entry: PluginEntry) -> None:
        async with self._lock:
            self._entries[entry.manifest.name] = entry

    async def remove(self, name: str) -> PluginEntry | None:
        async with self._lock:
            return self._entries.pop(name, None)

    async def set_diagnostics(self, diags: list[Diagnostic]) -> None:
        async with self._lock:
            self._diagnostics = list(diags)


__all__ = [
    "Diagnostic",
    "NameCollisionDiagnostic",
    "ParseErrorDiagnostic",
    "PluginEntry",
    "PluginRegistry",
]
