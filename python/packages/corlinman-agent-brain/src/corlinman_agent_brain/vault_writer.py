"""Markdown Vault Writer for the Memory Curator system.

Writes KnowledgeNode instances to an Obsidian-compatible Markdown vault
with proper directory structure, safe filenames, and dry-run support.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.models import KnowledgeNode, MemoryKind, NodeScope
from corlinman_agent_brain.serialization import node_to_markdown

# ---------------------------------------------------------------------------
# WriteResult
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    """Outcome of a vault write operation."""

    path: Path
    action: str  # "created" | "updated" | "draft" | "conflict" | "skipped"
    dry_run: bool
    previous_content: str | None = None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

#: Maps MemoryKind to the subdirectory name inside the vault.
_KIND_FOLDER_MAP: dict[MemoryKind, str] = {
    MemoryKind.PROJECT_CONTEXT: "projects",
    MemoryKind.USER_PREFERENCE: "preferences",
    MemoryKind.DECISION: "decisions",
    MemoryKind.TASK_STATE: "tasks",
    MemoryKind.AGENT_PERSONA: "persona",
    MemoryKind.CONCEPT: "concepts",
    MemoryKind.RELATIONSHIP: "relationships",
    MemoryKind.CONFLICT: "inbox",
}


def _safe_filename(title: str) -> str:
    """Sanitize a title into a filesystem-safe kebab-case filename.

    - Normalizes unicode to ASCII where possible
    - Strips special characters (no path traversal)
    - Converts to kebab-case
    - Truncates to max 80 characters
    - Returns a non-empty string (falls back to "untitled")
    """
    # Normalize unicode -> ASCII approximation
    normalized = unicodedata.normalize("NFKD", title)
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii")

    # Lowercase and replace non-alphanumeric with hyphens
    cleaned = re.sub(r"[^a-z0-9]+", "-", ascii_str.lower())

    # Strip leading/trailing hyphens
    cleaned = cleaned.strip("-")

    # Truncate to 80 chars (break at hyphen boundary if possible)
    if len(cleaned) > 80:
        truncated = cleaned[:80]
        cleaned = truncated.rsplit("-", 1)[0] if "-" in truncated else truncated

    return cleaned if cleaned else "untitled"


def _resolve_vault_path(node: KnowledgeNode, vault_root: Path) -> Path:
    """Determine the correct subdirectory for a node based on scope and kind.

    Directory structure:
        - global nodes  -> vault_root/global/{kind_folder}/
        - agent nodes   -> vault_root/agents/{agent_id}/{kind_folder}/
        - project nodes -> vault_root/agents/{agent_id}/{kind_folder}/
    """
    kind_folder = _KIND_FOLDER_MAP.get(node.kind, "misc")

    if node.frontmatter.scope == NodeScope.GLOBAL:
        return vault_root / "global" / kind_folder
    else:
        agent_id = node.frontmatter.agent_id or "unknown-agent"
        return vault_root / "agents" / agent_id / kind_folder


def _render_node_body(node: KnowledgeNode) -> str:
    """Render the full Markdown document for a KnowledgeNode.

    Uses node_to_markdown from serialization which already handles
    frontmatter + body rendering.
    """
    return node_to_markdown(node)

# ---------------------------------------------------------------------------
# VaultWriter
# ---------------------------------------------------------------------------


class VaultWriter:
    """Writes KnowledgeNode instances to a Markdown vault on disk.

    Supports dry-run mode for safe testing, saves previous content for
    rollback, and ensures idempotent writes.
    """

    def __init__(self, vault_root: Path, config: CuratorConfig) -> None:
        self._vault_root = vault_root
        self._config = config

    @property
    def vault_root(self) -> Path:
        """Root directory of the vault."""
        return self._vault_root

    def write_node(self, node: KnowledgeNode, *, dry_run: bool = False) -> WriteResult:
        """Write a finalized node to the vault.

        If the file already exists with identical content, the write is
        skipped (idempotent). If it exists with different content, it is
        updated.
        """
        target_path = self._target_path(node)
        content = _render_node_body(node)

        # Check existing
        previous_content: str | None = None
        if target_path.exists():
            previous_content = target_path.read_text(encoding="utf-8")
            if previous_content == content:
                return WriteResult(
                    path=target_path,
                    action="skipped",
                    dry_run=dry_run,
                    previous_content=previous_content,
                )
            # Content differs -> update
            return self._do_write(
                target_path, content, action="updated",
                dry_run=dry_run, previous_content=previous_content,
            )

        return self._do_write(
            target_path, content, action="created",
            dry_run=dry_run, previous_content=None,
        )

    def update_node(self, node: KnowledgeNode, *, dry_run: bool = False) -> WriteResult:
        """Update an existing node in the vault.

        If the file does not exist, it is created. Saves previous content
        for rollback.
        """
        target_path = self._target_path(node)
        content = _render_node_body(node)

        previous_content: str | None = None
        if target_path.exists():
            previous_content = target_path.read_text(encoding="utf-8")
            if previous_content == content:
                return WriteResult(
                    path=target_path,
                    action="skipped",
                    dry_run=dry_run,
                    previous_content=previous_content,
                )

        return self._do_write(
            target_path, content, action="updated",
            dry_run=dry_run, previous_content=previous_content,
        )

    def write_draft(self, node: KnowledgeNode, *, dry_run: bool = False) -> WriteResult:
        """Write a node as a draft (placed in the same location but marked as draft action)."""
        target_path = self._target_path(node)
        content = _render_node_body(node)

        previous_content: str | None = None
        if target_path.exists():
            previous_content = target_path.read_text(encoding="utf-8")
            if previous_content == content:
                return WriteResult(
                    path=target_path,
                    action="skipped",
                    dry_run=dry_run,
                    previous_content=previous_content,
                )

        return self._do_write(
            target_path, content, action="draft",
            dry_run=dry_run, previous_content=previous_content,
        )

    def write_conflict(self, node: KnowledgeNode, *, dry_run: bool = False) -> WriteResult:
        """Write a conflicting node to the inbox for manual resolution."""
        # Conflicts always go to the inbox folder
        inbox_dir = self._vault_root / "inbox"
        filename = _safe_filename(node.title) + ".md"
        target_path = inbox_dir / filename
        content = _render_node_body(node)

        previous_content: str | None = None
        if target_path.exists():
            previous_content = target_path.read_text(encoding="utf-8")
            if previous_content == content:
                return WriteResult(
                    path=target_path,
                    action="skipped",
                    dry_run=dry_run,
                    previous_content=previous_content,
                )

        return self._do_write(
            target_path, content, action="conflict",
            dry_run=dry_run, previous_content=previous_content,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _target_path(self, node: KnowledgeNode) -> Path:
        """Compute the full file path for a node."""
        directory = _resolve_vault_path(node, self._vault_root)
        filename = _safe_filename(node.title) + ".md"
        return directory / filename

    def _do_write(
        self,
        path: Path,
        content: str,
        *,
        action: str,
        dry_run: bool,
        previous_content: str | None,
    ) -> WriteResult:
        """Perform the actual file write (or skip if dry_run)."""
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        return WriteResult(
            path=path,
            action=action,
            dry_run=dry_run,
            previous_content=previous_content,
        )

