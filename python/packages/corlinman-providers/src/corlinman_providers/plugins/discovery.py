"""Manifest-first plugin discovery (manifest-only scan, no code exec).

Python port of ``rust/crates/corlinman-plugins/src/discovery.rs``. The discovery
phase is intentionally side-effect free: it walks each configured directory
looking for ``plugin-manifest.toml``, parses each file, and returns
``(manifest, origin, path)`` records. The registry layer applies origin-ranked
dedup and reports diagnostics for bad manifests.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

import structlog

from .manifest import MANIFEST_FILENAME, ManifestParseError, PluginManifest, parse_manifest_file

log = structlog.get_logger(__name__)


class Origin(IntEnum):
    """Where a discovered manifest lives.

    Higher variants override lower ones at dedup time: bundled defaults
    first, user config overrides last. Order matches
    ``rust/crates/corlinman-plugins/src/discovery.rs::Origin``.
    """

    BUNDLED = 0
    GLOBAL = 1
    WORKSPACE = 2
    CONFIG = 3

    def as_str(self) -> str:
        return self.name.lower()

    @property
    def rank(self) -> int:
        return int(self.value)


@dataclass(frozen=True)
class SearchRoot:
    """A pinned search root + its origin label.

    Order within :func:`discover` does not matter — the registry re-sorts by
    :class:`Origin` rank before resolving.
    """

    path: Path
    origin: Origin

    def __post_init__(self) -> None:
        # Coerce string paths so callers can pass either.
        object.__setattr__(self, "path", Path(self.path))


@dataclass
class DiscoveredPlugin:
    """One hit from :func:`discover`."""

    manifest: PluginManifest
    origin: Origin
    manifest_path: Path

    def plugin_dir(self) -> Path:
        """Directory containing ``plugin-manifest.toml``. Runtime launches
        child processes with this as ``cwd``.
        """
        return self.manifest_path.parent


@dataclass
class DiscoveryDiagnostic:
    """A diagnostic emitted when a manifest fails to parse or a name is
    ambiguous."""

    path: Path
    origin: Origin
    message: str


@dataclass
class DiscoveryResult:
    """Aggregate result of :func:`discover` — analogous to the Rust
    ``(Vec<DiscoveredPlugin>, Vec<DiscoveryDiagnostic>)`` tuple but named for
    keyword access from Python call sites.
    """

    plugins: list[DiscoveredPlugin] = field(default_factory=list)
    diagnostics: list[DiscoveryDiagnostic] = field(default_factory=list)


def _walk_for_manifests(root: Path, max_depth: int = 3) -> Iterable[Path]:
    """Walk ``root`` up to ``max_depth`` levels and yield paths to
    ``plugin-manifest.toml`` files. Symlinks are NOT followed (matches the
    Rust ``follow_links(false)`` walker).
    """
    if not root.exists():
        return

    root_depth = len(root.parts)
    for current_dir, dirs, files in os.walk(root, followlinks=False):
        cur_path = Path(current_dir)
        depth = len(cur_path.parts) - root_depth
        if depth >= max_depth:
            # Prevent descending further by clearing the directory list.
            dirs.clear()

        for name in files:
            if name == MANIFEST_FILENAME:
                yield cur_path / name


def discover(roots: list[SearchRoot]) -> DiscoveryResult:
    """Walk ``roots`` looking for ``*/plugin-manifest.toml``.

    Bad manifests are captured as diagnostics and skipped — discovery never
    aborts because one plugin is broken. Mirrors
    ``rust/crates/corlinman-plugins/src/discovery.rs::discover``.
    """
    result = DiscoveryResult()

    for root in roots:
        if not root.path.exists():
            log.debug(
                "plugins.discovery.root_missing",
                path=str(root.path),
                origin=root.origin.as_str(),
            )
            continue

        for manifest_path in _walk_for_manifests(root.path):
            try:
                manifest = parse_manifest_file(manifest_path)
            except ManifestParseError as err:
                log.warning(
                    "plugins.discovery.parse_failed",
                    path=str(manifest_path),
                    origin=root.origin.as_str(),
                    error=str(err),
                )
                result.diagnostics.append(
                    DiscoveryDiagnostic(
                        path=manifest_path,
                        origin=root.origin,
                        message=str(err),
                    )
                )
                continue

            result.plugins.append(
                DiscoveredPlugin(
                    manifest=manifest,
                    origin=root.origin,
                    manifest_path=manifest_path,
                )
            )

    return result


def roots_from_env_var(var: str, origin: Origin) -> list[SearchRoot]:
    """Parse ``CORLINMAN_PLUGIN_DIRS``-style colon-separated paths into a
    list of search roots. Empty entries are dropped.
    """
    raw = os.environ.get(var)
    if not raw:
        return []
    return [
        SearchRoot(path=Path(p.strip()), origin=origin)
        for p in raw.split(":")
        if p.strip()
    ]


__all__ = [
    "DiscoveredPlugin",
    "DiscoveryDiagnostic",
    "DiscoveryResult",
    "Origin",
    "SearchRoot",
    "discover",
    "roots_from_env_var",
]
