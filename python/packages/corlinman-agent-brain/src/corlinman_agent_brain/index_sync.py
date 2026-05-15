"""Index Sync - bridge between the Python Memory Curator and the Rust MemoryHost.

Speaks the same JSON protocol as corlinman-memory-host's RemoteHttpHost:

    POST   {base}/query   -> {"hits": [...]}
    POST   {base}/upsert  -> {"id": "..."}
    DELETE {base}/docs/{id}
    GET    {base}/health  -> 2xx

This module converts KnowledgeNode instances into MemoryDoc payloads
and syncs them to the vector index so they become retrievable via
hybrid search. It also implements the RetrievalProvider protocol
from link_planner, enabling the curator to query existing nodes
during link planning.

Design principles:
- Protocol-based: tests inject a stub transport, production uses HTTP.
- Retry with backoff for transient failures.
- Batch upsert for efficiency (sequential with rate limiting).
- Idempotent: re-upserting the same node is safe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote as url_quote

import httpx

from corlinman_agent_brain.models import (
    KnowledgeNode,
    KnowledgeNodeFrontmatter,
    MemoryKind,
    NodeScope,
    NodeStatus,
    RiskLevel,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport protocol (injectable for testing)
# ---------------------------------------------------------------------------


class HttpTransport(Protocol):
    """Async HTTP transport abstraction.

    Production uses aiohttp/httpx; tests inject a stub.
    """

    async def post(
        self, url: str, *, json_body: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        """POST JSON, return (status_code, response_json)."""
        ...


class HttpxTransport:
    """Production transport backed by ``httpx.AsyncClient``."""

    def __init__(self, *, timeout_ms: int = 5000) -> None:
        self._client = httpx.AsyncClient(timeout=timeout_ms / 1000.0)

    async def post(
        self, url: str, *, json_body: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        resp = await self._client.post(url, json=json_body, headers=headers)
        return resp.status_code, _json_or_empty(resp)

    async def delete(
        self, url: str, *, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        resp = await self._client.delete(url, headers=headers)
        return resp.status_code, _json_or_empty(resp)

    async def get(
        self, url: str, *, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        resp = await self._client.get(url, headers=headers)
        return resp.status_code, _json_or_empty(resp)

    async def close(self) -> None:
        await self._client.aclose()


def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {"error": resp.text}
    return data if isinstance(data, dict) else {"value": data}

    async def delete(
        self, url: str, *, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        """DELETE, return (status_code, response_json)."""
        ...

    async def get(
        self, url: str, *, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        """GET, return (status_code, response_json)."""
        ...


# ---------------------------------------------------------------------------
# Sync configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexSyncConfig:
    """Configuration for the index sync client."""

    base_url: str = "http://127.0.0.1:9820/memory"
    bearer_token: str = ""
    namespace: str = "agent-brain"
    max_retries: int = 3
    retry_base_delay_ms: int = 200
    batch_delay_ms: int = 50
    timeout_ms: int = 5000


# ---------------------------------------------------------------------------
# Sync result types
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """Outcome of a single index sync operation."""

    node_id: str
    action: str  # "upserted" | "deleted" | "skipped" | "failed"
    remote_id: str = ""
    error: str = ""


@dataclass
class BatchSyncReport:
    """Summary of a batch sync operation."""

    total: int = 0
    upserted: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[SyncResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Node -> MemoryDoc conversion
# ---------------------------------------------------------------------------


def node_to_memory_doc(node: KnowledgeNode) -> dict[str, Any]:
    """Convert a KnowledgeNode into the MemoryDoc JSON format.

    Content is built from title + summary + key_facts for maximum
    retrieval relevance. Metadata carries the structured fields for
    filtering (kind, scope, status, tags, etc).
    """
    content_parts: list[str] = [node.title]
    if node.summary:
        content_parts.append(node.summary)
    for fact in node.key_facts:
        content_parts.append(fact)
    for decision in node.decisions:
        content_parts.append(decision)
    content = "\n".join(content_parts)

    fm = node.frontmatter
    metadata: dict[str, Any] = {
        "node_id": node.node_id,
        "title": node.title,
        "kind": fm.kind.value,
        "scope": fm.scope.value,
        "status": fm.status.value,
        "confidence": fm.confidence,
        "risk": fm.risk.value,
        "tenant_id": fm.tenant_id,
        "agent_id": fm.agent_id,
        "tags": fm.tags,
        "source_session_id": fm.source_session_id,
        "created_at": fm.created_at,
        "updated_at": fm.updated_at,
    }

    return {
        "content": content,
        "metadata": metadata,
        "namespace": "agent-brain",
    }


# ---------------------------------------------------------------------------
# Query result -> KnowledgeNode (lightweight reconstruction)
# ---------------------------------------------------------------------------


def hit_to_knowledge_node(hit: dict[str, Any]) -> KnowledgeNode:
    """Reconstruct a lightweight KnowledgeNode from a MemoryHit response."""
    meta = hit.get("metadata", {})

    try:
        kind = MemoryKind(meta.get("kind", "concept"))
    except ValueError:
        kind = MemoryKind.CONCEPT

    try:
        scope = NodeScope(meta.get("scope", "agent"))
    except ValueError:
        scope = NodeScope.AGENT

    try:
        status = NodeStatus(meta.get("status", "active"))
    except ValueError:
        status = NodeStatus.ACTIVE

    try:
        risk = RiskLevel(meta.get("risk", "low"))
    except ValueError:
        risk = RiskLevel.LOW

    node_id = meta.get("node_id", hit.get("id", ""))
    title = meta.get("title", "")
    tags = meta.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    frontmatter = KnowledgeNodeFrontmatter(
        id=node_id,
        tenant_id=meta.get("tenant_id", "default"),
        agent_id=meta.get("agent_id", ""),
        scope=scope,
        kind=kind,
        status=status,
        confidence=float(meta.get("confidence", 0.5)),
        risk=risk,
        source_session_id=meta.get("source_session_id", ""),
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        tags=tags,
    )

    return KnowledgeNode(
        node_id=node_id,
        title=title,
        path="",
        kind=kind,
        frontmatter=frontmatter,
        summary=hit.get("content", ""),
    )


# ---------------------------------------------------------------------------
# IndexSyncClient
# ---------------------------------------------------------------------------


class IndexSyncClient:
    """Async client for syncing KnowledgeNodes to the Rust MemoryHost.

    Implements upsert, delete, query, and health check operations
    against the RemoteHttpHost JSON protocol.
    """

    def __init__(
        self,
        transport: HttpTransport,
        config: IndexSyncConfig | None = None,
    ) -> None:
        self._transport = transport
        self._config = config or IndexSyncConfig()

    @property
    def base_url(self) -> str:
        return self._config.base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        """Build request headers including auth if configured."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._config.bearer_token:
            headers["Authorization"] = f"Bearer {self._config.bearer_token}"
        return headers

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    async def upsert_node(self, node: KnowledgeNode) -> SyncResult:
        """Upsert a single KnowledgeNode into the vector index."""
        doc = node_to_memory_doc(node)
        url = f"{self.base_url}/upsert"

        for attempt in range(self._config.max_retries):
            try:
                status, body = await self._transport.post(
                    url, json_body=doc, headers=self._headers()
                )
                if 200 <= status < 300:
                    remote_id = body.get("id", "")
                    logger.debug(
                        "Upserted node %s -> remote_id=%s",
                        node.node_id, remote_id,
                    )
                    return SyncResult(
                        node_id=node.node_id,
                        action="upserted",
                        remote_id=remote_id,
                    )
                elif status >= 500:
                    logger.warning(
                        "Upsert %s got %d (attempt %d/%d)",
                        node.node_id, status,
                        attempt + 1, self._config.max_retries,
                    )
                    await self._backoff(attempt)
                else:
                    error_msg = body.get("error", f"HTTP {status}")
                    logger.error("Upsert %s failed: %s", node.node_id, error_msg)
                    return SyncResult(
                        node_id=node.node_id,
                        action="failed",
                        error=error_msg,
                    )
            except Exception as exc:
                logger.warning(
                    "Upsert %s transport error (attempt %d/%d): %s",
                    node.node_id, attempt + 1, self._config.max_retries, exc,
                )
                if attempt < self._config.max_retries - 1:
                    await self._backoff(attempt)
                else:
                    return SyncResult(
                        node_id=node.node_id,
                        action="failed",
                        error=str(exc),
                    )

        return SyncResult(
            node_id=node.node_id,
            action="failed",
            error="Max retries exceeded",
        )

    async def upsert_batch(self, nodes: list[KnowledgeNode]) -> BatchSyncReport:
        """Upsert multiple nodes sequentially with inter-request delay."""
        report = BatchSyncReport(total=len(nodes))

        for node in nodes:
            result = await self.upsert_node(node)
            report.results.append(result)

            if result.action == "upserted":
                report.upserted += 1
            elif result.action == "failed":
                report.failed += 1
                report.errors.append(f"{node.node_id}: {result.error}")
            elif result.action == "skipped":
                report.skipped += 1

            if self._config.batch_delay_ms > 0:
                await asyncio.sleep(self._config.batch_delay_ms / 1000.0)

        return report

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_node(self, node_id: str) -> SyncResult:
        """Delete a node from the vector index by its ID."""
        url = f"{self.base_url}/docs/{url_quote(node_id, safe='')}"

        for attempt in range(self._config.max_retries):
            try:
                status, body = await self._transport.delete(
                    url, headers=self._headers()
                )
                if 200 <= status < 300:
                    logger.debug("Deleted node %s from index", node_id)
                    return SyncResult(node_id=node_id, action="deleted")
                elif status == 404:
                    logger.debug("Node %s already deleted", node_id)
                    return SyncResult(node_id=node_id, action="deleted")
                elif status >= 500:
                    logger.warning(
                        "Delete %s got %d (attempt %d/%d)",
                        node_id, status, attempt + 1, self._config.max_retries,
                    )
                    await self._backoff(attempt)
                else:
                    error_msg = body.get("error", f"HTTP {status}")
                    return SyncResult(
                        node_id=node_id, action="failed", error=error_msg
                    )
            except Exception as exc:
                logger.warning(
                    "Delete %s transport error (attempt %d/%d): %s",
                    node_id, attempt + 1, self._config.max_retries, exc,
                )
                if attempt < self._config.max_retries - 1:
                    await self._backoff(attempt)
                else:
                    return SyncResult(
                        node_id=node_id, action="failed", error=str(exc)
                    )

        return SyncResult(
            node_id=node_id, action="failed", error="Max retries exceeded"
        )

    async def delete_batch(self, node_ids: list[str]) -> BatchSyncReport:
        """Delete multiple nodes from the index."""
        report = BatchSyncReport(total=len(node_ids))

        for node_id in node_ids:
            result = await self.delete_node(node_id)
            report.results.append(result)

            if result.action == "deleted":
                report.deleted += 1
            elif result.action == "failed":
                report.failed += 1
                report.errors.append(f"{node_id}: {result.error}")

            if self._config.batch_delay_ms > 0:
                await asyncio.sleep(self._config.batch_delay_ms / 1000.0)

        return report

    # ------------------------------------------------------------------
    # Query (implements RetrievalProvider contract)
    # ------------------------------------------------------------------

    async def __call__(self, query: str, *, limit: int = 5) -> list[KnowledgeNode]:
        """Delegate to :meth:`query` so this client satisfies ``RetrievalProvider``."""
        return await self.query(query, limit=limit)

    async def query(self, text: str, *, limit: int = 5) -> list[KnowledgeNode]:
        """Query the vector index for nodes similar to the given text.

        This method satisfies the RetrievalProvider protocol from
        link_planner, so IndexSyncClient can be injected directly
        as the retrieval provider during link planning.
        """
        url = f"{self.base_url}/query"
        query_body: dict[str, Any] = {
            "text": text,
            "top_k": limit,
            "filters": [],
            "namespace": self._config.namespace,
        }

        try:
            status, body = await self._transport.post(
                url, json_body=query_body, headers=self._headers()
            )
            if 200 <= status < 300:
                hits = body.get("hits", [])
                nodes: list[KnowledgeNode] = []
                for hit in hits:
                    try:
                        node = hit_to_knowledge_node(hit)
                        nodes.append(node)
                    except Exception as exc:
                        logger.warning("Failed to parse hit: %s", exc)
                return nodes
            else:
                logger.error("Query failed with status %d: %s", status, body)
                return []
        except Exception as exc:
            logger.error("Query transport error: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """Check if the MemoryHost service is reachable and healthy."""
        url = f"{self.base_url}/health"
        try:
            status, _ = await self._transport.get(url, headers=self._headers())
            return 200 <= status < 300
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Sync orchestration
    # ------------------------------------------------------------------

    async def sync_after_write(
        self,
        nodes_created: list[KnowledgeNode],
        nodes_updated: list[KnowledgeNode],
        nodes_deleted: list[str] | None = None,
    ) -> BatchSyncReport:
        """Sync vault changes to the vector index after a curator run.

        Called after VaultWriter has written/updated/deleted nodes.
        Upserts created and updated nodes, deletes removed ones.
        """
        all_nodes = nodes_created + nodes_updated
        report = BatchSyncReport(total=len(all_nodes) + len(nodes_deleted or []))

        if all_nodes:
            upsert_report = await self.upsert_batch(all_nodes)
            report.upserted = upsert_report.upserted
            report.failed += upsert_report.failed
            report.results.extend(upsert_report.results)
            report.errors.extend(upsert_report.errors)

        if nodes_deleted:
            delete_report = await self.delete_batch(nodes_deleted)
            report.deleted = delete_report.deleted
            report.failed += delete_report.failed
            report.results.extend(delete_report.results)
            report.errors.extend(delete_report.errors)

        return report

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _backoff(self, attempt: int) -> None:
        """Exponential backoff with jitter."""
        base_ms = self._config.retry_base_delay_ms
        delay_ms = base_ms * (2 ** attempt)
        delay_ms = min(delay_ms, 5000)
        await asyncio.sleep(delay_ms / 1000.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "BatchSyncReport",
    "HttpTransport",
    "HttpxTransport",
    "IndexSyncClient",
    "IndexSyncConfig",
    "SyncResult",
    "hit_to_knowledge_node",
    "node_to_memory_doc",
]
