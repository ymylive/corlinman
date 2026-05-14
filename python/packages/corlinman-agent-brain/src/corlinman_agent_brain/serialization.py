"""Serialization helpers for Memory Curator data models.

Provides dict/JSON round-trip and YAML frontmatter generation so that
models can be persisted to SQLite, transmitted over IPC, and written
into Obsidian-compatible Markdown files.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from corlinman_agent_brain.models import (
    KnowledgeNode,
    KnowledgeNodeFrontmatter,
    MemoryKind,
    NodeScope,
    NodeStatus,
    RiskLevel,
)

# ---------------------------------------------------------------------------
# Generic dataclass <-> dict
# ---------------------------------------------------------------------------


def to_dict(obj: Any) -> dict[str, Any]:
    """Recursively convert a dataclass instance to a plain dict.

    StrEnum values are stored as their string value so the output is
    JSON-serializable without custom encoders.
    """
    if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
        raise TypeError(f"Expected a dataclass instance, got {type(obj)}")

    result: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        result[f.name] = _serialize_value(value)
    return result


def _serialize_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_dict(value)
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# YAML frontmatter
# ---------------------------------------------------------------------------


def frontmatter_to_yaml(fm: KnowledgeNodeFrontmatter) -> str:
    """Render a KnowledgeNodeFrontmatter as a YAML frontmatter block.

    Returns the string including the opening and closing --- fences.
    """
    lines: list[str] = ["---"]
    lines.append(f"id: {fm.id}")
    lines.append(f"tenant_id: {fm.tenant_id}")
    lines.append(f"agent_id: {fm.agent_id}")
    lines.append(f"scope: {fm.scope.value}")
    lines.append(f"kind: {fm.kind.value}")
    lines.append(f"status: {fm.status.value}")
    lines.append(f"confidence: {fm.confidence}")
    lines.append(f"risk: {fm.risk.value}")

    # Source provenance
    if fm.source_session_id:
        lines.append(f"source_session_id: {fm.source_session_id}")
    if fm.source_episode_id:
        lines.append(f"source_episode_id: {fm.source_episode_id}")
    lines.append(f"created_from: {fm.created_from}")

    # Timestamps
    lines.append(f"created_at: {fm.created_at}")
    lines.append(f"updated_at: {fm.updated_at}")

    # Links
    if fm.links:
        lines.append("links:")
        for link in fm.links:
            lines.append(f'  - "{link}"')

    # Tags
    if fm.tags:
        lines.append("tags:")
        for tag in fm.tags:
            lines.append(f"  - {tag}")

    lines.append("---")
    return "\n".join(lines) + "\n"


def frontmatter_from_dict(data: dict[str, Any]) -> KnowledgeNodeFrontmatter:
    """Parse a frontmatter dict (from YAML) into a KnowledgeNodeFrontmatter."""
    return KnowledgeNodeFrontmatter(
        id=data["id"],
        tenant_id=data.get("tenant_id", "default"),
        agent_id=data.get("agent_id", ""),
        scope=NodeScope(data.get("scope", "agent")),
        kind=MemoryKind(data["kind"]),
        status=NodeStatus(data.get("status", "draft")),
        confidence=float(data.get("confidence", 0.5)),
        risk=RiskLevel(data.get("risk", "low")),
        source_session_id=data.get("source_session_id", ""),
        source_episode_id=data.get("source_episode_id", ""),
        created_from=data.get("created_from", "session_curator"),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        links=data.get("links", []),
        tags=data.get("tags", []),
    )


# ---------------------------------------------------------------------------
# KnowledgeNode -> Markdown
# ---------------------------------------------------------------------------


def node_to_markdown(node: KnowledgeNode) -> str:
    """Render a KnowledgeNode as a complete Markdown document."""
    parts: list[str] = []

    # Frontmatter
    parts.append(frontmatter_to_yaml(node.frontmatter))

    # Title
    parts.append(f"# {node.title}\n")

    # Summary
    parts.append("## Summary\n")
    parts.append(f"{node.summary}\n" if node.summary else "")

    # Key facts
    if node.key_facts:
        parts.append("## Key Facts\n")
        for fact in node.key_facts:
            parts.append(f"- {fact}")
        parts.append("")

    # Decisions
    if node.decisions:
        parts.append("## Decisions\n")
        for dec in node.decisions:
            parts.append(f"- {dec}")
        parts.append("")

    # Evidence sources
    if node.evidence_sources:
        parts.append("## Evidence Sources\n")
        for src in node.evidence_sources:
            parts.append(f"- {src}")
        parts.append("")

    # Related nodes
    if node.related_nodes:
        parts.append("## Related Nodes\n")
        for rel in node.related_nodes:
            parts.append(f"- [[{rel}]]")
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
