"""Tests for corlinman_agent_brain models and serialization.

Covers:
- Dataclass instantiation and defaults
- Enum string values
- to_dict round-trip serialization
- YAML frontmatter generation and parsing
- KnowledgeNode -> Markdown rendering
- Edge cases (empty lists, nested dataclasses)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from corlinman_agent_brain.models import (
    BundleMessage,
    CuratorRun,
    CuratorRunStatus,
    KnowledgeNode,
    KnowledgeNodeFrontmatter,
    LinkAction,
    LinkPlan,
    LinkPlanEntry,
    MemoryCandidate,
    MemoryKind,
    NodeScope,
    NodeStatus,
    RiskLevel,
    SessionBundle,
    WritePolicy,
)
from corlinman_agent_brain.serialization import (
    frontmatter_from_dict,
    frontmatter_to_yaml,
    node_to_markdown,
    now_iso,
    to_dict,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_frontmatter() -> KnowledgeNodeFrontmatter:
    return KnowledgeNodeFrontmatter(
        id="node-001",
        tenant_id="tenant-a",
        agent_id="agent-x",
        scope=NodeScope.AGENT,
        kind=MemoryKind.DECISION,
        status=NodeStatus.ACTIVE,
        confidence=0.85,
        risk=RiskLevel.LOW,
        source_session_id="sess-123",
        source_episode_id="ep-456",
        created_from="session_curator",
        created_at="2024-01-15T10:00:00Z",
        updated_at="2024-01-15T10:05:00Z",
        links=["node-002", "node-003"],
        tags=["architecture", "rust"],
    )


@pytest.fixture
def sample_node(sample_frontmatter: KnowledgeNodeFrontmatter) -> KnowledgeNode:
    return KnowledgeNode(
        node_id="node-001",
        title="Use Rust for MemoryHost",
        path="decisions/use-rust-memoryhost.md",
        kind=MemoryKind.DECISION,
        frontmatter=sample_frontmatter,
        summary="Decided to implement MemoryHost in Rust for performance.",
        key_facts=["Rust provides zero-cost abstractions",
                   "FFI with Python via PyO3"],
        decisions=["MemoryHost will be a Rust crate"],
        evidence_sources=["session sess-123 turn 5"],
        related_nodes=["node-002"],
    )


@pytest.fixture
def sample_candidate() -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id="cand-001",
        topic="Rust MemoryHost",
        kind=MemoryKind.DECISION,
        summary="User decided to use Rust for MemoryHost.",
        evidence=["turn 5: user said use Rust"],
        confidence=0.9,
        risk=RiskLevel.LOW,
        source_session_id="sess-123",
        agent_id="agent-x",
        tenant_id="tenant-a",
        tags=["rust", "architecture"],
    )


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestEnums:
    def test_memory_kind_values(self) -> None:
        assert MemoryKind.PROJECT_CONTEXT == "project_context"
        assert MemoryKind.DECISION == "decision"
        assert MemoryKind.CONFLICT == "conflict"

    def test_risk_level_values(self) -> None:
        assert RiskLevel.LOW == "low"
        assert RiskLevel.BLOCKED == "blocked"

    def test_node_status_values(self) -> None:
        assert NodeStatus.DRAFT == "draft"
        assert NodeStatus.ARCHIVED == "archived"

    def test_node_scope_values(self) -> None:
        assert NodeScope.GLOBAL == "global"
        assert NodeScope.AGENT == "agent"
        assert NodeScope.PROJECT == "project"

    def test_link_action_values(self) -> None:
        assert LinkAction.UPDATE_EXISTING == "update_existing"
        assert LinkAction.CREATE_AND_LINK == "create_and_link"

    def test_write_policy_values(self) -> None:
        assert WritePolicy.DRAFT_FIRST == "draft_first"
        assert WritePolicy.AUTO == "auto"

    def test_curator_run_status_values(self) -> None:
        assert CuratorRunStatus.OK == "ok"
        assert CuratorRunStatus.SKIPPED_EMPTY == "skipped_empty"


# ---------------------------------------------------------------------------
# Model instantiation tests
# ---------------------------------------------------------------------------


class TestModels:
    def test_bundle_message_frozen(self) -> None:
        msg = BundleMessage(seq=1, role="user", content="hello", ts_ms=1000)
        with pytest.raises(FrozenInstanceError):
            msg.seq = 2  # type: ignore[misc]

    def test_session_bundle_defaults(self) -> None:
        bundle = SessionBundle(
            session_id="s1",
            tenant_id="t1",
            user_id="u1",
            agent_id="a1",
        )
        assert bundle.messages == []
        assert bundle.started_at_ms == 0
        assert bundle.episode_ids == []

    def test_memory_candidate_defaults(self) -> None:
        cand = MemoryCandidate(
            candidate_id="c1",
            topic="test",
            kind=MemoryKind.CONCEPT,
            summary="A concept.",
        )
        assert cand.confidence == 0.5
        assert cand.risk == RiskLevel.LOW
        assert cand.evidence == []
        assert cand.discard is False

    def test_link_plan_entry_frozen(self) -> None:
        entry = LinkPlanEntry(
            candidate_id="c1",
            action=LinkAction.CREATE_NEW,
        )
        with pytest.raises(FrozenInstanceError):
            entry.candidate_id = "c2"  # type: ignore[misc]

    def test_link_plan_defaults(self) -> None:
        plan = LinkPlan()
        assert plan.entries == []

    def test_curator_run_defaults(self) -> None:
        run = CuratorRun(
            run_id="r1",
            tenant_id="t1",
            agent_id="a1",
            session_id="s1",
        )
        assert run.status == CuratorRunStatus.RUNNING
        assert run.candidates_total == 0
        assert run.nodes_created == []
        assert run.errors == []


# ---------------------------------------------------------------------------
# Serialization: to_dict
# ---------------------------------------------------------------------------


class TestToDict:
    def test_simple_dataclass(self) -> None:
        msg = BundleMessage(seq=1, role="user", content="hi", ts_ms=100)
        d = to_dict(msg)
        assert d == {
            "seq": 1,
            "role": "user",
            "content": "hi",
            "ts_ms": 100,
            "tool_call_id": None,
            "tool_calls": None,
        }

    def test_enum_serialized_as_string(self, sample_candidate: MemoryCandidate) -> None:
        d = to_dict(sample_candidate)
        assert d["kind"] == "decision"
        assert d["risk"] == "low"

    def test_nested_dataclass(self, sample_node: KnowledgeNode) -> None:
        d = to_dict(sample_node)
        assert isinstance(d["frontmatter"], dict)
        assert d["frontmatter"]["scope"] == "agent"
        assert d["frontmatter"]["kind"] == "decision"

    def test_list_of_strings_preserved(self, sample_candidate: MemoryCandidate) -> None:
        d = to_dict(sample_candidate)
        assert d["tags"] == ["rust", "architecture"]

    def test_rejects_non_dataclass(self) -> None:
        with pytest.raises(TypeError):
            to_dict("not a dataclass")

    def test_rejects_class_type(self) -> None:
        with pytest.raises(TypeError):
            to_dict(BundleMessage)


# ---------------------------------------------------------------------------
# Serialization: frontmatter YAML
# ---------------------------------------------------------------------------


class TestFrontmatterYaml:
    def test_yaml_has_fences(self, sample_frontmatter: KnowledgeNodeFrontmatter) -> None:
        yaml = frontmatter_to_yaml(sample_frontmatter)
        lines = yaml.strip().split("\n")
        assert lines[0] == "---"
        assert lines[-1] == "---"

    def test_yaml_contains_fields(self, sample_frontmatter: KnowledgeNodeFrontmatter) -> None:
        yaml = frontmatter_to_yaml(sample_frontmatter)
        assert "id: node-001" in yaml
        assert "scope: agent" in yaml
        assert "kind: decision" in yaml
        assert "confidence: 0.85" in yaml
        assert "risk: low" in yaml

    def test_yaml_links_rendered(self, sample_frontmatter: KnowledgeNodeFrontmatter) -> None:
        yaml = frontmatter_to_yaml(sample_frontmatter)
        assert "links:" in yaml
        assert '"node-002"' in yaml
        assert '"node-003"' in yaml

    def test_yaml_tags_rendered(self, sample_frontmatter: KnowledgeNodeFrontmatter) -> None:
        yaml = frontmatter_to_yaml(sample_frontmatter)
        assert "tags:" in yaml
        assert "  - architecture" in yaml
        assert "  - rust" in yaml

    def test_yaml_omits_empty_links(self) -> None:
        fm = KnowledgeNodeFrontmatter(
            id="n1",
            tenant_id="t1",
            agent_id="a1",
            scope=NodeScope.GLOBAL,
            kind=MemoryKind.CONCEPT,
            status=NodeStatus.DRAFT,
            confidence=0.5,
            risk=RiskLevel.LOW,
        )
        yaml = frontmatter_to_yaml(fm)
        assert "links:" not in yaml
        assert "tags:" not in yaml


# ---------------------------------------------------------------------------
# Serialization: frontmatter_from_dict
# ---------------------------------------------------------------------------


class TestFrontmatterFromDict:
    def test_round_trip(self, sample_frontmatter: KnowledgeNodeFrontmatter) -> None:
        data = {
            "id": "node-001",
            "tenant_id": "tenant-a",
            "agent_id": "agent-x",
            "scope": "agent",
            "kind": "decision",
            "status": "active",
            "confidence": 0.85,
            "risk": "low",
            "source_session_id": "sess-123",
            "source_episode_id": "ep-456",
            "created_from": "session_curator",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:05:00Z",
            "links": ["node-002", "node-003"],
            "tags": ["architecture", "rust"],
        }
        fm = frontmatter_from_dict(data)
        assert fm.id == "node-001"
        assert fm.scope == NodeScope.AGENT
        assert fm.kind == MemoryKind.DECISION
        assert fm.status == NodeStatus.ACTIVE
        assert fm.confidence == 0.85
        assert fm.links == ["node-002", "node-003"]

    def test_defaults_applied(self) -> None:
        data = {"id": "n1", "kind": "concept"}
        fm = frontmatter_from_dict(data)
        assert fm.tenant_id == "default"
        assert fm.scope == NodeScope.AGENT
        assert fm.status == NodeStatus.DRAFT
        assert fm.confidence == 0.5
        assert fm.risk == RiskLevel.LOW


# ---------------------------------------------------------------------------
# Serialization: node_to_markdown
# ---------------------------------------------------------------------------


class TestNodeToMarkdown:
    def test_contains_title(self, sample_node: KnowledgeNode) -> None:
        md = node_to_markdown(sample_node)
        assert "# Use Rust for MemoryHost" in md

    def test_contains_summary(self, sample_node: KnowledgeNode) -> None:
        md = node_to_markdown(sample_node)
        assert "## Summary" in md
        assert "Decided to implement MemoryHost in Rust" in md

    def test_contains_key_facts(self, sample_node: KnowledgeNode) -> None:
        md = node_to_markdown(sample_node)
        assert "## Key Facts" in md
        assert "- Rust provides zero-cost abstractions" in md

    def test_contains_decisions(self, sample_node: KnowledgeNode) -> None:
        md = node_to_markdown(sample_node)
        assert "## Decisions" in md
        assert "- MemoryHost will be a Rust crate" in md

    def test_contains_evidence(self, sample_node: KnowledgeNode) -> None:
        md = node_to_markdown(sample_node)
        assert "## Evidence Sources" in md

    def test_contains_related_nodes_wikilinks(self, sample_node: KnowledgeNode) -> None:
        md = node_to_markdown(sample_node)
        assert "## Related Nodes" in md
        assert "- [[node-002]]" in md

    def test_frontmatter_at_top(self, sample_node: KnowledgeNode) -> None:
        md = node_to_markdown(sample_node)
        assert md.startswith("---\n")

    def test_empty_sections_omitted(self, sample_frontmatter: KnowledgeNodeFrontmatter) -> None:
        node = KnowledgeNode(
            node_id="n1",
            title="Minimal",
            path="minimal.md",
            kind=MemoryKind.CONCEPT,
            frontmatter=sample_frontmatter,
        )
        md = node_to_markdown(node)
        assert "## Key Facts" not in md
        assert "## Decisions" not in md
        assert "## Evidence Sources" not in md
        assert "## Related Nodes" not in md


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


class TestTimestamp:
    def test_now_iso_format(self) -> None:
        ts = now_iso()
        assert ts.endswith("Z")
        assert "T" in ts
        # Should be parseable
        from datetime import datetime

        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
