"""Tests for the agent-brain curator runner and CLI entrypoint."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.index_sync import IndexSyncClient, SyncResult
from corlinman_agent_brain.link_planner import plan_links
from corlinman_agent_brain.models import LinkAction, MemoryCandidate, MemoryKind
from corlinman_agent_brain.runner import CuratorPipeline, NullRetrievalProvider, curate_session


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
