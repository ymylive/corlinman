"""Data models for Agent Brain Memory Curator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MemoryKind(StrEnum):
    """Type of knowledge a memory node represents."""

    PROJECT_CONTEXT = "project_context"
    USER_PREFERENCE = "user_preference"
    AGENT_PERSONA = "agent_persona"
    DECISION = "decision"
    TASK_STATE = "task_state"
    CONCEPT = "concept"
    RELATIONSHIP = "relationship"
    CONFLICT = "conflict"


class RiskLevel(StrEnum):
    """Risk classification for a memory candidate."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class NodeStatus(StrEnum):
    """Lifecycle status of a knowledge node."""

    DRAFT = "draft"
    APPROVED = "approved"
    ACTIVE = "active"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    CONFLICT = "conflict"


class NodeScope(StrEnum):
    """Visibility scope of a knowledge node."""

    GLOBAL = "global"
    AGENT = "agent"
    PROJECT = "project"


class LinkAction(StrEnum):
    """What to do when a candidate relates to an existing node."""

    UPDATE_EXISTING = "update_existing"
    MERGE_INTO_EXISTING = "merge_into_existing"
    CREATE_NEW = "create_new"
    CREATE_AND_LINK = "create_and_link"
    SEND_TO_REVIEW = "send_to_review"


class WritePolicy(StrEnum):
    """How aggressively the curator writes to the vault."""

    DRAFT_FIRST = "draft_first"
    SEMI_AUTO = "semi_auto"
    AUTO = "auto"


class CuratorRunStatus(StrEnum):
    """Status of a curator run."""

    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED_EMPTY = "skipped_empty"


# ---------------------------------------------------------------------------
# SessionBundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleMessage:
    """A single message within a session bundle."""

    seq: int
    role: str
    content: str
    ts_ms: int
    tool_call_id: str | None = None
    tool_calls: list[dict] | None = None


@dataclass
class SessionBundle:
    """Aggregated session material ready for curator processing."""

    session_id: str
    tenant_id: str
    user_id: str
    agent_id: str
    messages: list[BundleMessage] = field(default_factory=list)
    started_at_ms: int = 0
    ended_at_ms: int = 0
    episode_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MemoryCandidate
# ---------------------------------------------------------------------------


@dataclass
class MemoryCandidate:
    """A candidate piece of knowledge extracted from a session."""

    candidate_id: str
    topic: str
    kind: MemoryKind
    summary: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.5
    risk: RiskLevel = RiskLevel.LOW
    source_session_id: str = ""
    source_episode_id: str = ""
    agent_id: str = ""
    tenant_id: str = ""
    tags: list[str] = field(default_factory=list)
    discard: bool = False
    discard_reason: str = ""


# ---------------------------------------------------------------------------
# KnowledgeNode
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeNodeFrontmatter:
    """YAML frontmatter metadata for a knowledge node file."""

    id: str
    tenant_id: str
    agent_id: str
    scope: NodeScope
    kind: MemoryKind
    status: NodeStatus
    confidence: float
    risk: RiskLevel
    source_session_id: str = ""
    source_episode_id: str = ""
    created_from: str = "session_curator"
    created_at: str = ""
    updated_at: str = ""
    links: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class KnowledgeNode:
    """A complete knowledge node with frontmatter and content."""

    node_id: str
    title: str
    path: str
    kind: MemoryKind
    frontmatter: KnowledgeNodeFrontmatter
    summary: str = ""
    key_facts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    evidence_sources: list[str] = field(default_factory=list)
    related_nodes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LinkPlan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkPlanEntry:
    """A single linking decision for one candidate."""

    candidate_id: str
    action: LinkAction
    target_node_id: str | None = None
    similarity_score: float = 0.0
    reason: str = ""


@dataclass
class LinkPlan:
    """Collection of linking decisions for a curator run."""

    entries: list[LinkPlanEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CuratorRun
# ---------------------------------------------------------------------------


@dataclass
class CuratorRun:
    """Record of a single curator execution."""

    run_id: str
    tenant_id: str
    agent_id: str
    session_id: str
    status: CuratorRunStatus = CuratorRunStatus.RUNNING
    started_at_ms: int = 0
    finished_at_ms: int = 0
    candidates_total: int = 0
    candidates_auto_written: int = 0
    candidates_drafted: int = 0
    candidates_discarded: int = 0
    nodes_created: list[str] = field(default_factory=list)
    nodes_updated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    decision_log: list[str] = field(default_factory=list)


