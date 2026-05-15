"""Tests for the agent-brain curator runner and CLI entrypoint."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.index_sync import (
    IndexSyncClient,
    SyncResult,
    hit_to_knowledge_node,
    node_to_memory_doc,
)
from corlinman_agent_brain.link_planner import plan_links
from corlinman_agent_brain.models import (
    KnowledgeNode,
    KnowledgeNodeFrontmatter,
    LinkAction,
    MemoryCandidate,
    MemoryKind,
    NodeScope,
    NodeStatus,
    RiskLevel,
)
from corlinman_agent_brain.runner import CuratorPipeline, NullRetrievalProvider, curate_session
from corlinman_agent_brain.vault_writer import VaultWriter


@pytest.fixture
def sessions_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "sessions.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions ("
        "session_key TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, "
        "content TEXT, ts INTEGER, tenant_id TEXT DEFAULT 'default', "
        "agent_id TEXT DEFAULT '')"
    )
    rows = [
        (
            "sess-1",
            1,
            "user",
            "Remember that this project uses PostgreSQL for storage.",
            1000,
            "default",
            "agent-x",
        ),
        (
            "sess-1",
            2,
            "assistant",
            "Noted: PostgreSQL is the storage backend.",
            2000,
            "default",
            "agent-x",
        ),
        (
            "sess-1",
            3,
            "user",
            "Also prefer Alembic migrations for database changes.",
            3000,
            "default",
            "agent-x",
        ),
        (
            "sess-1",
            4,
            "assistant",
            "I will use Alembic for schema migrations.",
            4000,
            "default",
            "agent-x",
        ),
    ]
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


class StubTransport:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json_body: dict, headers: dict[str, str]) -> tuple[int, dict]:
        self.posts.append((url, json_body))
        if url.endswith("/query"):
            return 200, {"hits": []}
        if url.endswith("/upsert"):
            return 200, {"id": "remote-1"}
        raise AssertionError(f"unexpected POST {url}")

    async def delete(self, url: str, *, headers: dict[str, str]) -> tuple[int, dict]:
        return 204, {}

    async def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, dict]:
        return 200, {}


class RecordingSync:
    def __init__(self) -> None:
        self.nodes = []

    async def upsert_node(self, node):
        self.nodes.append(node)
        return SyncResult(node_id=node.node_id, action="upserted", remote_id="remote")


def _node(
    *,
    node_id: str = "kn-existing",
    title: str = "Project database backend",
    kind: MemoryKind = MemoryKind.PROJECT_CONTEXT,
    tags: list[str] | None = None,
    links: list[str] | None = None,
    related_nodes: list[str] | None = None,
    summary: str = "The project uses PostgreSQL.",
    confidence: float = 0.9,
) -> KnowledgeNode:
    return KnowledgeNode(
        node_id=node_id,
        title=title,
        path="",
        kind=kind,
        frontmatter=KnowledgeNodeFrontmatter(
            id=node_id,
            tenant_id="default",
            agent_id="agent-x",
            scope=NodeScope.AGENT,
            kind=kind,
            status=NodeStatus.ACTIVE,
            confidence=confidence,
            risk=RiskLevel.LOW,
            source_session_id="sess-old",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            links=links or [],
            tags=tags or ["project", "database"],
        ),
        summary=summary,
        key_facts=[summary],
        related_nodes=related_nodes or [],
    )


class FixedRetrieval:
    def __init__(self, *nodes: KnowledgeNode) -> None:
        self.nodes = list(nodes)

    async def __call__(self, query: str, *, limit: int = 5) -> list[KnowledgeNode]:
        return self.nodes[:limit]


async def extraction_provider(*, prompt: str) -> str:
    assert "PostgreSQL" in prompt
    return json.dumps(
        [
            {
                "topic": "Project database backend",
                "kind": "project_context",
                "summary": "The project uses PostgreSQL for storage.",
                "evidence": ["project uses PostgreSQL"],
                "confidence": 0.91,
                "tags": ["project", "database"],
                "discard": False,
                "discard_reason": "",
            }
        ]
    )


async def linked_extraction_provider(*, prompt: str) -> str:
    return json.dumps(
        [
            {
                "topic": "Project database migrations",
                "kind": "concept",
                "summary": "Database changes should use Alembic migrations.",
                "evidence": ["prefer Alembic migrations"],
                "confidence": 0.91,
                "tags": ["project", "database"],
                "discard": False,
                "discard_reason": "",
            }
        ]
    )


async def low_confidence_update_provider(*, prompt: str) -> str:
    return json.dumps(
        [
            {
                "topic": "Project database backend",
                "kind": "project_context",
                "summary": "The project might use a different database backend.",
                "evidence": ["uncertain database note"],
                "confidence": 0.4,
                "tags": ["project", "database"],
                "discard": False,
                "discard_reason": "",
            }
        ]
    )


async def high_confidence_update_provider(*, prompt: str) -> str:
    return json.dumps(
        [
            {
                "topic": "Project database backend",
                "kind": "project_context",
                "summary": "The project uses PostgreSQL with Alembic migrations.",
                "evidence": ["confirmed database stack"],
                "confidence": 0.95,
                "tags": ["project", "database", "alembic"],
                "discard": False,
                "discard_reason": "",
            }
        ]
    )


async def high_risk_update_provider(*, prompt: str) -> str:
    return json.dumps(
        [
            {
                "topic": "Project database backend",
                "kind": "project_context",
                "summary": "The project database password is password=supersecret.",
                "evidence": ["password=supersecret"],
                "confidence": 0.95,
                "tags": ["project", "database"],
                "discard": False,
                "discard_reason": "",
            }
        ]
    )


@pytest.mark.asyncio
async def test_curate_session_writes_vault_and_syncs_index(
    tmp_path: Path, sessions_db: Path
) -> None:
    sync = RecordingSync()
    report = await curate_session(
        session_id="sess-1",
        sessions_db=sessions_db,
        vault_root=tmp_path / "vault",
        config=CuratorConfig(),
        extraction_provider=extraction_provider,
        retrieval_provider=NullRetrievalProvider(),
        sync_client=sync,
    )

    assert report.run.status == "ok"
    assert report.candidates_total == 1
    assert report.nodes_written == 1
    assert report.nodes_synced == 1
    assert sync.nodes[0].frontmatter.status == "active"
    written = list((tmp_path / "vault").rglob("*.md"))
    assert len(written) == 1
    assert "PostgreSQL" in written[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_index_sync_client_is_callable_for_link_planner() -> None:
    client = IndexSyncClient(StubTransport())  # type: ignore[arg-type]
    candidate = MemoryCandidate(
        candidate_id="mc-1",
        topic="Project database backend",
        kind=MemoryKind.PROJECT_CONTEXT,
        summary="The project uses PostgreSQL.",
    )

    plan = await plan_links([candidate], client, CuratorConfig())

    assert plan.entries[0].action == LinkAction.CREATE_NEW


def test_node_to_memory_doc_includes_link_graph_metadata() -> None:
    node = _node(
        node_id="kn-a",
        title="Node A",
        links=["kn-b"],
        related_nodes=["Node B"],
    )

    doc = node_to_memory_doc(node)

    assert doc["metadata"]["node_id"] == "kn-a"
    assert doc["metadata"]["links"] == ["kn-b"]
    assert doc["metadata"]["related_nodes"] == ["Node B"]


def test_hit_to_knowledge_node_restores_link_graph_metadata() -> None:
    node = hit_to_knowledge_node(
        {
            "id": "remote-1",
            "content": "Node A\nSummary",
            "score": 1.0,
            "metadata": {
                "node_id": "kn-a",
                "title": "Node A",
                "kind": "concept",
                "scope": "agent",
                "status": "active",
                "risk": "low",
                "tenant_id": "default",
                "agent_id": "agent-x",
                "tags": ["memory"],
                "links": ["kn-b"],
                "related_nodes": ["Node B"],
            },
        }
    )

    assert node.node_id == "kn-a"
    assert node.frontmatter.links == ["kn-b"]
    assert node.related_nodes == ["Node B"]


def test_cli_module_imports_and_builds_parser() -> None:
    from corlinman_agent_brain.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "curate-session",
            "--session-id",
            "sess-1",
            "--sessions-db",
            "sessions.sqlite",
            "--vault-root",
            "vault",
        ]
    )
    assert args.command == "curate-session"


@pytest.mark.asyncio
async def test_pipeline_reports_missing_session(tmp_path: Path) -> None:
    pipeline = CuratorPipeline(
        config=CuratorConfig(),
        vault_root=tmp_path / "vault",
        extraction_provider=extraction_provider,
        retrieval_provider=NullRetrievalProvider(),
    )

    report = await pipeline.curate_session(
        session_id="missing",
        sessions_db=tmp_path / "missing.sqlite",
    )

    assert report.run.status == "skipped_empty"
    assert report.candidates_total == 0


@pytest.mark.asyncio
async def test_create_and_link_writes_new_node_and_backlink(
    tmp_path: Path, sessions_db: Path
) -> None:
    vault_root = tmp_path / "vault"
    config = CuratorConfig(write_policy="auto")
    existing = _node()
    VaultWriter(vault_root, config).write_node(existing)
    sync = RecordingSync()

    report = await curate_session(
        session_id="sess-1",
        sessions_db=sessions_db,
        vault_root=vault_root,
        config=config,
        extraction_provider=linked_extraction_provider,
        retrieval_provider=FixedRetrieval(existing),
        sync_client=sync,
    )

    assert report.run.status == "ok"
    assert report.run.nodes_updated == ["kn-existing"]
    assert len(report.run.nodes_created) == 1
    new_path = vault_root / "agents" / "agent-x" / "concepts" / "project-database-migrations.md"
    existing_path = vault_root / "agents" / "agent-x" / "projects" / "project-database-backend.md"
    new_content = new_path.read_text(encoding="utf-8")
    existing_content = existing_path.read_text(encoding="utf-8")
    assert '  - "kn-existing"' in new_content
    assert "- [[Project database backend]]" in new_content
    assert report.run.nodes_created[0] in existing_content
    assert "- [[Project database migrations]]" in existing_content
    assert {node.node_id for node in sync.nodes} == {"kn-existing", report.run.nodes_created[0]}


@pytest.mark.asyncio
async def test_low_confidence_update_goes_to_review_without_touching_target(
    tmp_path: Path, sessions_db: Path
) -> None:
    vault_root = tmp_path / "vault"
    config = CuratorConfig(write_policy="semi_auto")
    existing = _node()
    existing_result = VaultWriter(vault_root, config).write_node(existing)
    before = existing_result.path.read_text(encoding="utf-8")

    report = await curate_session(
        session_id="sess-1",
        sessions_db=sessions_db,
        vault_root=vault_root,
        config=config,
        extraction_provider=low_confidence_update_provider,
        retrieval_provider=FixedRetrieval(existing),
    )

    assert report.run.status == "ok"
    assert report.run.nodes_updated == []
    assert existing_result.path.read_text(encoding="utf-8") == before
    inbox_files = list((vault_root / "inbox").glob("*.md"))
    assert len(inbox_files) == 1
    assert "might use a different database backend" in inbox_files[0].read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_high_confidence_update_modifies_existing_node(
    tmp_path: Path, sessions_db: Path
) -> None:
    vault_root = tmp_path / "vault"
    config = CuratorConfig(write_policy="semi_auto")
    existing = _node()
    existing_path = VaultWriter(vault_root, config).write_node(existing).path

    report = await curate_session(
        session_id="sess-1",
        sessions_db=sessions_db,
        vault_root=vault_root,
        config=config,
        extraction_provider=high_confidence_update_provider,
        retrieval_provider=FixedRetrieval(existing),
    )

    assert report.run.status == "ok"
    assert report.run.nodes_created == []
    assert report.run.nodes_updated == ["kn-existing"]
    content = existing_path.read_text(encoding="utf-8")
    assert "The project uses PostgreSQL with Alembic migrations." in content
    assert "  - alembic" in content


@pytest.mark.asyncio
async def test_high_risk_update_goes_to_review_without_touching_target(
    tmp_path: Path, sessions_db: Path
) -> None:
    vault_root = tmp_path / "vault"
    config = CuratorConfig(write_policy="semi_auto")
    existing = _node()
    existing_result = VaultWriter(vault_root, config).write_node(existing)
    before = existing_result.path.read_text(encoding="utf-8")

    report = await curate_session(
        session_id="sess-1",
        sessions_db=sessions_db,
        vault_root=vault_root,
        config=config,
        extraction_provider=high_risk_update_provider,
        retrieval_provider=FixedRetrieval(existing),
    )

    assert report.run.status == "ok"
    assert report.run.nodes_updated == []
    assert existing_result.path.read_text(encoding="utf-8") == before
    inbox_files = list((vault_root / "inbox").glob("*.md"))
    assert len(inbox_files) == 1
    assert "database password" in inbox_files[0].read_text(encoding="utf-8")
