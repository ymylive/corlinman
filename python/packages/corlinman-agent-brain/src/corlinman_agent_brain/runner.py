"""End-to-end runner for the Agent Brain memory curator.

The runner is intentionally orchestration-only: extraction, risk
classification, link planning, vault writes, and index sync stay in their
focused modules. This file wires those pieces into one executable pass.
"""

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.extractor import ExtractionProvider, extract_candidates
from corlinman_agent_brain.index_sync import IndexSyncClient, SyncResult
from corlinman_agent_brain.link_planner import RetrievalProvider, plan_links
from corlinman_agent_brain.models import (
    CuratorRun,
    CuratorRunStatus,
    KnowledgeNode,
    KnowledgeNodeFrontmatter,
    LinkAction,
    LinkPlanEntry,
    MemoryCandidate,
    NodeScope,
    NodeStatus,
    SessionBundle,
    WritePolicy,
)
from corlinman_agent_brain.risk_classifier import (
    classify_risk_batch,
    decide_write_action,
)
from corlinman_agent_brain.serialization import now_iso
from corlinman_agent_brain.session_reader import read_session_by_id
from corlinman_agent_brain.vault_writer import VaultWriter, WriteResult


class SyncClient(Protocol):
    """Minimal index-sync contract used by the runner."""

    async def upsert_node(self, node: KnowledgeNode) -> SyncResult: ...


class NullRetrievalProvider:
    """Retrieval provider that returns no existing nodes.

    Useful for dry runs, tests, and first-boot environments where the
    MemoryHost endpoint is not reachable yet.
    """

    async def __call__(self, query: str, *, limit: int = 5) -> list[KnowledgeNode]:
        return []


@dataclass
class CuratorReport:
    """Summary emitted by one curator pass."""

    run: CuratorRun
    candidates_total: int = 0
    candidates_discarded: int = 0
    nodes_written: int = 0
    nodes_synced: int = 0
    write_results: list[WriteResult] = field(default_factory=list)
    sync_results: list[SyncResult] = field(default_factory=list)


class CuratorPipeline:
    """Executable memory-curation pipeline."""

    def __init__(
        self,
        *,
        config: CuratorConfig,
        vault_root: Path,
        extraction_provider: ExtractionProvider,
        retrieval_provider: RetrievalProvider | None = None,
        sync_client: SyncClient | None = None,
    ) -> None:
        self._config = config
        self._vault = VaultWriter(vault_root, config)
        self._extract = extraction_provider
        self._retrieve = retrieval_provider or NullRetrievalProvider()
        self._sync = sync_client

    async def curate_session(
        self,
        *,
        session_id: str,
        sessions_db: Path,
        dry_run: bool = False,
    ) -> CuratorReport:
        started_ms = _now_ms()
        bundle = read_session_by_id(sessions_db=sessions_db, session_key=session_id)
        if bundle is None:
            run = CuratorRun(
                run_id=_run_id(),
                tenant_id="",
                agent_id="",
                session_id=session_id,
                status=CuratorRunStatus.SKIPPED_EMPTY,
                started_at_ms=started_ms,
                finished_at_ms=_now_ms(),
            )
            return CuratorReport(run=run)

        run = CuratorRun(
            run_id=_run_id(),
            tenant_id=bundle.tenant_id,
            agent_id=bundle.agent_id,
            session_id=bundle.session_id,
            status=CuratorRunStatus.RUNNING,
            started_at_ms=started_ms,
        )
        report = CuratorReport(run=run)

        try:
            candidates = await extract_candidates(
                bundle=bundle,
                config=self._config,
                provider=self._extract,
            )
            classify_risk_batch(candidates, self._config)
            plan = await plan_links(candidates, self._retrieve, self._config)
            plan_by_candidate = {entry.candidate_id: entry for entry in plan.entries}

            report.candidates_total = len(candidates)
            run.candidates_total = len(candidates)

            for candidate in candidates:
                if candidate.discard:
                    report.candidates_discarded += 1
                    run.candidates_discarded += 1
                    continue

                entry = plan_by_candidate.get(candidate.candidate_id)
                if entry is None:
                    run.errors.append(f"{candidate.candidate_id}: missing link plan")
                    continue

                decision = decide_write_action(
                    candidate,
                    _write_policy(self._config),
                    self._config,
                )
                if decision.action == "block":
                    run.decision_log.append(
                        f"{candidate.candidate_id}: blocked: {decision.reason}"
                    )
                    continue

                if (
                    entry.action
                    in {LinkAction.UPDATE_EXISTING, LinkAction.MERGE_INTO_EXISTING}
                    and (
                        decision.action != "auto_write"
                        or candidate.confidence < self._config.auto_write_min_confidence
                    )
                ):
                    review_node = _candidate_to_node(candidate, bundle, entry)
                    review_node.frontmatter.status = NodeStatus.CONFLICT
                    result = self._vault.write_conflict(review_node, dry_run=dry_run)
                    report.write_results.append(result)
                    if result.action != "skipped":
                        report.nodes_written += 1
                    run.candidates_drafted += 1
                    run.decision_log.append(
                        f"{candidate.candidate_id}: review required; "
                        f"{entry.action}: {decision.reason}"
                    )
                    continue

                node = _node_for_plan(candidate, bundle, entry)

                updated_targets: list[KnowledgeNode] = []
                if entry.action == LinkAction.CREATE_AND_LINK and entry.target_node is not None:
                    updated_targets.append(_add_backlink(entry.target_node, node))

                if entry.action in {
                    LinkAction.UPDATE_EXISTING,
                    LinkAction.MERGE_INTO_EXISTING,
                }:
                    result = self._vault.update_node(node, dry_run=dry_run)
                    run.nodes_updated.append(node.node_id)
                    run.candidates_auto_written += 1
                elif entry.action == LinkAction.SEND_TO_REVIEW:
                    result = self._vault.write_conflict(node, dry_run=dry_run)
                    run.candidates_drafted += 1
                elif decision.action == "auto_write":
                    result = self._vault.write_node(node, dry_run=dry_run)
                    run.candidates_auto_written += 1
                else:
                    node.frontmatter.status = NodeStatus.DRAFT
                    result = self._vault.write_draft(node, dry_run=dry_run)
                    run.candidates_drafted += 1

                report.write_results.append(result)
                if result.action != "skipped":
                    report.nodes_written += 1
                if entry.action not in {
                    LinkAction.UPDATE_EXISTING,
                    LinkAction.MERGE_INTO_EXISTING,
                }:
                    run.nodes_created.append(node.node_id)
                run.decision_log.append(
                    f"{candidate.candidate_id}: {decision.action}; {entry.action}: {entry.reason}"
                )

                for updated in updated_targets:
                    backlink_result = self._vault.update_node(updated, dry_run=dry_run)
                    report.write_results.append(backlink_result)
                    if backlink_result.action != "skipped":
                        report.nodes_written += 1
                    run.nodes_updated.append(updated.node_id)

                if self._sync is not None and not dry_run:
                    for sync_node in [node, *updated_targets]:
                        sync_result = await self._sync.upsert_node(sync_node)
                        report.sync_results.append(sync_result)
                        if sync_result.action == "upserted":
                            report.nodes_synced += 1
                        elif sync_result.action == "failed":
                            run.errors.append(
                                f"sync {sync_node.node_id}: {sync_result.error or 'failed'}"
                            )

            run.status = CuratorRunStatus.OK if not run.errors else CuratorRunStatus.FAILED
        except Exception as exc:
            run.status = CuratorRunStatus.FAILED
            run.errors.append(str(exc))
        finally:
            run.finished_at_ms = _now_ms()

        return report


async def curate_session(
    *,
    session_id: str,
    sessions_db: Path,
    vault_root: Path,
    config: CuratorConfig,
    extraction_provider: ExtractionProvider,
    retrieval_provider: RetrievalProvider | None = None,
    sync_client: SyncClient | None = None,
    dry_run: bool = False,
) -> CuratorReport:
    """Convenience wrapper for one session-curation pass."""

    pipeline = CuratorPipeline(
        config=config,
        vault_root=vault_root,
        extraction_provider=extraction_provider,
        retrieval_provider=retrieval_provider,
        sync_client=sync_client,
    )
    return await pipeline.curate_session(
        session_id=session_id,
        sessions_db=sessions_db,
        dry_run=dry_run,
    )


def memoryhost_retrieval(sync_client: IndexSyncClient) -> RetrievalProvider:
    """Adapt an ``IndexSyncClient`` into the link-planner retrieval protocol."""

    return sync_client


def _candidate_to_node(
    candidate: MemoryCandidate,
    bundle: SessionBundle,
    entry: LinkPlanEntry | LinkAction,
) -> KnowledgeNode:
    now = now_iso()
    node_id = _node_id()
    links: list[str] = []
    related_nodes: list[str] = []
    if isinstance(entry, LinkPlanEntry):
        action = entry.action
        if action == LinkAction.CREATE_AND_LINK and entry.target_node is not None:
            links = [entry.target_node.node_id]
            related_nodes = [entry.target_node.title]
    else:
        action = entry

    fm = KnowledgeNodeFrontmatter(
        id=node_id,
        tenant_id=candidate.tenant_id or bundle.tenant_id,
        agent_id=candidate.agent_id or bundle.agent_id,
        scope=NodeScope.AGENT,
        kind=candidate.kind,
        status=NodeStatus.ACTIVE,
        confidence=candidate.confidence,
        risk=candidate.risk,
        source_session_id=candidate.source_session_id or bundle.session_id,
        source_episode_id=candidate.source_episode_id,
        created_at=now,
        updated_at=now,
        links=links,
        tags=candidate.tags,
    )
    return KnowledgeNode(
        node_id=node_id,
        title=candidate.topic,
        path="",
        kind=candidate.kind,
        frontmatter=fm,
        summary=candidate.summary,
        key_facts=[candidate.summary],
        evidence_sources=candidate.evidence,
        related_nodes=related_nodes,
    )


def _node_for_plan(
    candidate: MemoryCandidate,
    bundle: SessionBundle,
    entry: LinkPlanEntry,
) -> KnowledgeNode:
    if entry.action in {LinkAction.UPDATE_EXISTING, LinkAction.MERGE_INTO_EXISTING}:
        if entry.target_node is None:
            return _candidate_to_node(candidate, bundle, entry)
        return _merge_candidate_into_node(entry.target_node, candidate)
    return _candidate_to_node(candidate, bundle, entry)


def _merge_candidate_into_node(
    target: KnowledgeNode,
    candidate: MemoryCandidate,
) -> KnowledgeNode:
    node = deepcopy(target)
    now = now_iso()
    node.summary = candidate.summary or node.summary
    node.key_facts = _append_unique(node.key_facts, [candidate.summary])
    node.evidence_sources = _append_unique(node.evidence_sources, candidate.evidence)
    node.frontmatter.confidence = max(node.frontmatter.confidence, candidate.confidence)
    node.frontmatter.risk = candidate.risk
    node.frontmatter.updated_at = now
    node.frontmatter.tags = _append_unique(node.frontmatter.tags, candidate.tags)
    return node


def _add_backlink(target: KnowledgeNode, new_node: KnowledgeNode) -> KnowledgeNode:
    node = deepcopy(target)
    node.frontmatter.links = _append_unique(node.frontmatter.links, [new_node.node_id])
    node.related_nodes = _append_unique(node.related_nodes, [new_node.title])
    node.frontmatter.updated_at = now_iso()
    return node


def _append_unique(existing: list[str], additions: list[str]) -> list[str]:
    out = [item for item in existing if item]
    seen = set(out)
    for item in additions:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _write_policy(config: CuratorConfig) -> WritePolicy:
    try:
        return WritePolicy(config.write_policy)
    except ValueError:
        return WritePolicy.DRAFT_FIRST


def _run_id() -> str:
    return f"cr-{uuid.uuid4().hex[:12]}"


def _node_id() -> str:
    return f"kn-{uuid.uuid4().hex[:12]}"


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "CuratorPipeline",
    "CuratorReport",
    "NullRetrievalProvider",
    "SyncClient",
    "curate_session",
    "memoryhost_retrieval",
]
