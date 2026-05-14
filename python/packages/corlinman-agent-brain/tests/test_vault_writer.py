"""Tests for corlinman_agent_brain.vault_writer module.

Covers:
- _safe_filename: kebab-case, truncation, special chars, unicode, fallback
- _resolve_vault_path: scope/kind directory mapping
- VaultWriter.write_node: create, update, skip (idempotent), dry_run
- VaultWriter.update_node: create if missing, update, skip
- VaultWriter.write_draft: draft action
- VaultWriter.write_conflict: inbox routing
- WriteResult structure
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.models import (
    KnowledgeNode,
    KnowledgeNodeFrontmatter,
    MemoryKind,
    NodeScope,
    NodeStatus,
    RiskLevel,
)
from corlinman_agent_brain.vault_writer import (
    VaultWriter,
    WriteResult,
    _resolve_vault_path,
    _safe_filename,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> CuratorConfig:
    return CuratorConfig()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def writer(vault_root: Path, config: CuratorConfig) -> VaultWriter:
    return VaultWriter(vault_root, config)


def _make_node(
    *,
    node_id: str = "node-001",
    title: str = "Test Node Title",
    kind: MemoryKind = MemoryKind.DECISION,
    scope: NodeScope = NodeScope.AGENT,
    agent_id: str = "agent-x",
    tags: list[str] | None = None,
) -> KnowledgeNode:
    if tags is None:
        tags = ["testing"]
    return KnowledgeNode(
        node_id=node_id,
        title=title,
        path="",
        kind=kind,
        frontmatter=KnowledgeNodeFrontmatter(
            id=node_id,
            tenant_id="tenant-a",
            agent_id=agent_id,
            scope=scope,
            kind=kind,
            status=NodeStatus.ACTIVE,
            confidence=0.9,
            risk=RiskLevel.LOW,
            source_session_id="sess-001",
            created_from="session_curator",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            tags=tags,
        ),
        summary="This is a test node summary.",
        key_facts=["Fact one", "Fact two"],
        decisions=["Decision A"],
        evidence_sources=["session turn 3"],
    )


# ---------------------------------------------------------------------------
# Tests: _safe_filename
# ---------------------------------------------------------------------------


class TestSafeFilename:
    def test_simple_title(self) -> None:
        assert _safe_filename("Hello World") == "hello-world"

    def test_special_characters_removed(self) -> None:
        assert _safe_filename("Use pytest! (v2.0)") == "use-pytest-v2-0"

    def test_path_traversal_stripped(self) -> None:
        result = _safe_filename("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result
        assert "\\" not in result

    def test_kebab_case(self) -> None:
        assert _safe_filename("CamelCase Title Here") == "camelcase-title-here"

    def test_truncates_to_80_chars(self) -> None:
        long_title = "word " * 30
        result = _safe_filename(long_title)
        assert len(result) <= 80

    def test_truncates_at_hyphen_boundary(self) -> None:
        long_title = "a " * 50
        result = _safe_filename(long_title)
        assert len(result) <= 80
        assert not result.endswith("-")

    def test_empty_string_returns_untitled(self) -> None:
        assert _safe_filename("") == "untitled"

    def test_only_special_chars_returns_untitled(self) -> None:
        assert _safe_filename("!@#$%^&*()") == "untitled"

    def test_unicode_normalized(self) -> None:
        result = _safe_filename("caf\xe9 r\xe9sum\xe9")
        assert result == "cafe-resume"

    def test_numbers_preserved(self) -> None:
        assert _safe_filename("Version 3.2.1") == "version-3-2-1"

    def test_leading_trailing_hyphens_stripped(self) -> None:
        result = _safe_filename("---hello---")
        assert result == "hello"

    def test_multiple_spaces_collapsed(self) -> None:
        result = _safe_filename("hello    world")
        assert result == "hello-world"

    def test_mixed_case_lowered(self) -> None:
        assert _safe_filename("MyProject Setup") == "myproject-setup"


# ---------------------------------------------------------------------------
# Tests: _resolve_vault_path
# ---------------------------------------------------------------------------


class TestResolveVaultPath:
    def test_global_scope_decision(self, vault_root: Path) -> None:
        node = _make_node(scope=NodeScope.GLOBAL, kind=MemoryKind.DECISION)
        path = _resolve_vault_path(node, vault_root)
        assert path == vault_root / "global" / "decisions"

    def test_global_scope_concept(self, vault_root: Path) -> None:
        node = _make_node(scope=NodeScope.GLOBAL, kind=MemoryKind.CONCEPT)
        path = _resolve_vault_path(node, vault_root)
        assert path == vault_root / "global" / "concepts"

    def test_agent_scope(self, vault_root: Path) -> None:
        node = _make_node(scope=NodeScope.AGENT, agent_id="agent-x", kind=MemoryKind.DECISION)
        path = _resolve_vault_path(node, vault_root)
        assert path == vault_root / "agents" / "agent-x" / "decisions"

    def test_project_scope(self, vault_root: Path) -> None:
        node = _make_node(scope=NodeScope.PROJECT, agent_id="agent-y", kind=MemoryKind.PROJECT_CONTEXT)
        path = _resolve_vault_path(node, vault_root)
        assert path == vault_root / "agents" / "agent-y" / "projects"

    def test_all_kind_folders(self, vault_root: Path) -> None:
        expected_map = {
            MemoryKind.PROJECT_CONTEXT: "projects",
            MemoryKind.USER_PREFERENCE: "preferences",
            MemoryKind.DECISION: "decisions",
            MemoryKind.TASK_STATE: "tasks",
            MemoryKind.AGENT_PERSONA: "persona",
            MemoryKind.CONCEPT: "concepts",
            MemoryKind.RELATIONSHIP: "relationships",
            MemoryKind.CONFLICT: "inbox",
        }
        for kind, folder in expected_map.items():
            node = _make_node(scope=NodeScope.GLOBAL, kind=kind)
            path = _resolve_vault_path(node, vault_root)
            assert path.name == folder

    def test_missing_agent_id_uses_unknown(self, vault_root: Path) -> None:
        node = _make_node(scope=NodeScope.AGENT, agent_id="")
        path = _resolve_vault_path(node, vault_root)
        assert "unknown-agent" in str(path)


# ---------------------------------------------------------------------------
# Tests: VaultWriter.write_node
# ---------------------------------------------------------------------------


class TestWriteNode:
    def test_creates_new_file(self, writer: VaultWriter, vault_root: Path) -> None:
        node = _make_node(title="Brand New Node")
        result = writer.write_node(node)
        assert result.action == "created"
        assert result.dry_run is False
        assert result.previous_content is None
        assert result.path.exists()
        assert result.path.name == "brand-new-node.md"
        content = result.path.read_text(encoding="utf-8")
        assert "Brand New Node" in content

    def test_updates_existing_different_content(self, writer: VaultWriter, vault_root: Path) -> None:
        node = _make_node(title="Updatable Node")
        result1 = writer.write_node(node)
        assert result1.action == "created"

        node2 = _make_node(title="Updatable Node")
        node2.summary = "Updated summary content."
        result2 = writer.write_node(node2)
        assert result2.action == "updated"
        assert result2.previous_content is not None
        assert "This is a test node summary" in result2.previous_content

    def test_skips_identical_content(self, writer: VaultWriter) -> None:
        node = _make_node(title="Idempotent Node")
        result1 = writer.write_node(node)
        assert result1.action == "created"

        result2 = writer.write_node(node)
        assert result2.action == "skipped"
        assert result2.previous_content is not None

    def test_dry_run_does_not_write(self, writer: VaultWriter, vault_root: Path) -> None:
        node = _make_node(title="Dry Run Node")
        result = writer.write_node(node, dry_run=True)
        assert result.action == "created"
        assert result.dry_run is True
        assert not result.path.exists()

    def test_creates_parent_directories(self, writer: VaultWriter) -> None:
        node = _make_node(scope=NodeScope.AGENT, agent_id="deep-agent", kind=MemoryKind.CONCEPT)
        result = writer.write_node(node)
        assert result.action == "created"
        assert result.path.parent.exists()

    def test_write_result_path_has_md_extension(self, writer: VaultWriter) -> None:
        node = _make_node(title="Extension Test")
        result = writer.write_node(node)
        assert result.path.suffix == ".md"


# ---------------------------------------------------------------------------
# Tests: VaultWriter.update_node
# ---------------------------------------------------------------------------


class TestUpdateNode:
    def test_creates_if_not_exists(self, writer: VaultWriter) -> None:
        node = _make_node(title="Update New")
        result = writer.update_node(node)
        assert result.action == "updated"
        assert result.path.exists()

    def test_updates_existing(self, writer: VaultWriter) -> None:
        node = _make_node(title="Update Existing")
        writer.write_node(node)

        node2 = _make_node(title="Update Existing")
        node2.summary = "Changed summary."
        result = writer.update_node(node2)
        assert result.action == "updated"
        assert result.previous_content is not None

    def test_skips_identical(self, writer: VaultWriter) -> None:
        node = _make_node(title="Update Skip")
        writer.write_node(node)
        result = writer.update_node(node)
        assert result.action == "skipped"

    def test_dry_run(self, writer: VaultWriter) -> None:
        node = _make_node(title="Update Dry")
        result = writer.update_node(node, dry_run=True)
        assert result.action == "updated"
        assert result.dry_run is True
        assert not result.path.exists()


# ---------------------------------------------------------------------------
# Tests: VaultWriter.write_draft
# ---------------------------------------------------------------------------


class TestWriteDraft:
    def test_writes_with_draft_action(self, writer: VaultWriter) -> None:
        node = _make_node(title="Draft Node")
        result = writer.write_draft(node)
        assert result.action == "draft"
        assert result.path.exists()

    def test_skips_identical_draft(self, writer: VaultWriter) -> None:
        node = _make_node(title="Draft Skip")
        writer.write_draft(node)
        result = writer.write_draft(node)
        assert result.action == "skipped"

    def test_dry_run_draft(self, writer: VaultWriter) -> None:
        node = _make_node(title="Draft Dry")
        result = writer.write_draft(node, dry_run=True)
        assert result.action == "draft"
        assert result.dry_run is True
        assert not result.path.exists()

    def test_overwrites_existing_different_content(self, writer: VaultWriter) -> None:
        node = _make_node(title="Draft Overwrite")
        writer.write_draft(node)

        node2 = _make_node(title="Draft Overwrite")
        node2.summary = "New draft content."
        result = writer.write_draft(node2)
        assert result.action == "draft"
        assert result.previous_content is not None


# ---------------------------------------------------------------------------
# Tests: VaultWriter.write_conflict
# ---------------------------------------------------------------------------


class TestWriteConflict:
    def test_writes_to_inbox(self, writer: VaultWriter, vault_root: Path) -> None:
        node = _make_node(title="Conflict Node")
        result = writer.write_conflict(node)
        assert result.action == "conflict"
        assert result.path.exists()
        assert "inbox" in str(result.path)
        assert result.path.parent == vault_root / "inbox"

    def test_conflict_filename(self, writer: VaultWriter) -> None:
        node = _make_node(title="My Conflict")
        result = writer.write_conflict(node)
        assert result.path.name == "my-conflict.md"

    def test_skips_identical_conflict(self, writer: VaultWriter) -> None:
        node = _make_node(title="Conflict Skip")
        writer.write_conflict(node)
        result = writer.write_conflict(node)
        assert result.action == "skipped"

    def test_dry_run_conflict(self, writer: VaultWriter) -> None:
        node = _make_node(title="Conflict Dry")
        result = writer.write_conflict(node, dry_run=True)
        assert result.action == "conflict"
        assert result.dry_run is True
        assert not result.path.exists()

    def test_conflict_ignores_node_scope(self, writer: VaultWriter, vault_root: Path) -> None:
        """Conflicts always go to inbox regardless of node scope/kind."""
        node = _make_node(
            title="Global Conflict",
            scope=NodeScope.GLOBAL,
            kind=MemoryKind.CONCEPT,
        )
        result = writer.write_conflict(node)
        assert result.path.parent == vault_root / "inbox"


# ---------------------------------------------------------------------------
# Tests: WriteResult structure
# ---------------------------------------------------------------------------


class TestWriteResult:
    def test_dataclass_fields(self) -> None:
        result = WriteResult(
            path=Path("/tmp/test.md"),
            action="created",
            dry_run=False,
            previous_content=None,
        )
        assert result.path == Path("/tmp/test.md")
        assert result.action == "created"
        assert result.dry_run is False
        assert result.previous_content is None

    def test_with_previous_content(self) -> None:
        result = WriteResult(
            path=Path("/tmp/test.md"),
            action="updated",
            dry_run=False,
            previous_content="old content here",
        )
        assert result.previous_content == "old content here"


# ---------------------------------------------------------------------------
# Tests: VaultWriter.vault_root property
# ---------------------------------------------------------------------------


class TestVaultWriterInit:
    def test_vault_root_property(self, vault_root: Path, config: CuratorConfig) -> None:
        writer = VaultWriter(vault_root, config)
        assert writer.vault_root == vault_root

    def test_different_vault_roots(self, tmp_path: Path, config: CuratorConfig) -> None:
        root1 = tmp_path / "vault1"
        root2 = tmp_path / "vault2"
        w1 = VaultWriter(root1, config)
        w2 = VaultWriter(root2, config)
        assert w1.vault_root != w2.vault_root


# ---------------------------------------------------------------------------
# Tests: Integration - full write cycle
# ---------------------------------------------------------------------------


class TestWriteCycleIntegration:
    def test_create_then_update_then_skip(self, writer: VaultWriter) -> None:
        node = _make_node(title="Lifecycle Node")

        # Create
        r1 = writer.write_node(node)
        assert r1.action == "created"

        # Update with different content
        node.summary = "Updated lifecycle summary."
        r2 = writer.write_node(node)
        assert r2.action == "updated"

        # Skip with same content
        r3 = writer.write_node(node)
        assert r3.action == "skipped"

    def test_multiple_nodes_different_kinds(self, writer: VaultWriter) -> None:
        kinds = [MemoryKind.DECISION, MemoryKind.CONCEPT, MemoryKind.USER_PREFERENCE]
        results = []
        for i, kind in enumerate(kinds):
            node = _make_node(
                node_id=f"node-{i}",
                title=f"Node Kind {kind.value}",
                kind=kind,
            )
            results.append(writer.write_node(node))

        assert all(r.action == "created" for r in results)
        dirs = {r.path.parent for r in results}
        assert len(dirs) == 3

    def test_global_and_agent_nodes_separate(self, writer: VaultWriter, vault_root: Path) -> None:
        global_node = _make_node(title="Global Decision", scope=NodeScope.GLOBAL)
        agent_node = _make_node(title="Agent Decision", scope=NodeScope.AGENT, agent_id="my-agent")

        r1 = writer.write_node(global_node)
        r2 = writer.write_node(agent_node)

        assert "global" in str(r1.path)
        assert "agents" in str(r2.path)
        assert "my-agent" in str(r2.path)
