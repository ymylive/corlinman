"""Link / Merge / Create Planner for the Memory Curator.

Given a batch of MemoryCandidate objects, this module retrieves similar
existing KnowledgeNodes (via an injected RetrievalProvider) and decides
what linking action to take for each candidate.

Design principles:
- Pure functions for decision logic (testable without async)
- Protocol-based dependency injection for retrieval
- Every decision is traceable via reason strings in LinkPlanEntry
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.models import (
    KnowledgeNode,
    LinkAction,
    LinkPlan,
    LinkPlanEntry,
    MemoryCandidate,
    MemoryKind,
    RiskLevel,
)

# ---------------------------------------------------------------------------
# Protocol for retrieval injection
# ---------------------------------------------------------------------------


class RetrievalProvider(Protocol):
    """Async callable that retrieves existing nodes similar to a query.

    Implementations may wrap vector search, keyword search, or vault
    file scanning.  The planner does not care about the mechanism.
    """

    async def __call__(self, query: str, *, limit: int = 5) -> list[KnowledgeNode]:
        ...


# ---------------------------------------------------------------------------
# Similarity helpers (pure functions)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Lowercase split into word tokens."""
    return set(text.lower().split())


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two sets."""
    if not a and not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union) if union else 0.0


def _compute_similarity_score(candidate: MemoryCandidate, node: KnowledgeNode) -> float:
    """Compute text-based similarity between a candidate and an existing node.

    Uses a weighted combination of:
    - Jaccard similarity on tags (40%)
    - Title/topic word overlap (60%)
    """
    # Tag similarity
    candidate_tags = {t.lower() for t in candidate.tags}
    node_tags = {t.lower() for t in node.frontmatter.tags}
    tag_sim = _jaccard(candidate_tags, node_tags)

    # Title / topic word overlap
    candidate_words = _tokenize(candidate.topic)
    node_words = _tokenize(node.title)
    title_sim = _jaccard(candidate_words, node_words)

    return 0.4 * tag_sim + 0.6 * title_sim


# ---------------------------------------------------------------------------
# Decision logic (pure)
# ---------------------------------------------------------------------------


def _decide_action(
    candidate: MemoryCandidate,
    matches: list[tuple[KnowledgeNode, float]],
    config: CuratorConfig,
) -> LinkPlanEntry:
    """Decide what action to take for a candidate given its similarity matches.

    Returns a LinkPlanEntry with the chosen action, target node (if any),
    similarity score, and a human-readable reason string.
    """
    # Check conflict flag first - takes priority
    if candidate.risk == RiskLevel.BLOCKED or candidate.kind == MemoryKind.CONFLICT:
        target_id = matches[0][0].node_id if matches else None
        best_score = matches[0][1] if matches else 0.0
        return LinkPlanEntry(
            candidate_id=candidate.candidate_id,
            action=LinkAction.SEND_TO_REVIEW,
            target_node_id=target_id,
            target_node=matches[0][0] if matches else None,
            similarity_score=best_score,
            reason=(
                f"Candidate flagged for review: risk={candidate.risk}, kind={candidate.kind}"
            ),
        )

    if not matches:
        return LinkPlanEntry(
            candidate_id=candidate.candidate_id,
            action=LinkAction.CREATE_NEW,
            target_node_id=None,
            similarity_score=0.0,
            reason="No similar existing nodes found",
        )

    # Sort by similarity descending
    matches_sorted = sorted(matches, key=lambda m: m[1], reverse=True)
    best_node, best_score = matches_sorted[0]

    # High similarity + same kind -> UPDATE
    if best_score > config.similarity_threshold_update and best_node.kind == candidate.kind:
        return LinkPlanEntry(
            candidate_id=candidate.candidate_id,
            action=LinkAction.UPDATE_EXISTING,
            target_node_id=best_node.node_id,
            target_node=best_node,
            similarity_score=best_score,
            reason=(
                f"High similarity ({best_score:.3f} > {config.similarity_threshold_update}) "
                f"and same kind ({candidate.kind}); updating existing node"
            ),
        )

    # Medium-high similarity + same kind -> MERGE
    if best_score > config.similarity_threshold_merge and best_node.kind == candidate.kind:
        return LinkPlanEntry(
            candidate_id=candidate.candidate_id,
            action=LinkAction.MERGE_INTO_EXISTING,
            target_node_id=best_node.node_id,
            target_node=best_node,
            similarity_score=best_score,
            reason=(
                f"Moderate similarity ({best_score:.3f} > {config.similarity_threshold_merge}) "
                f"and same kind ({candidate.kind}); merging into existing node"
            ),
        )

    # Moderate similarity -> CREATE_AND_LINK
    if best_score > config.similarity_threshold_link:
        return LinkPlanEntry(
            candidate_id=candidate.candidate_id,
            action=LinkAction.CREATE_AND_LINK,
            target_node_id=best_node.node_id,
            target_node=best_node,
            similarity_score=best_score,
            reason=(
                f"Low-moderate similarity ({best_score:.3f} > {config.similarity_threshold_link}); "
                f"creating new node and linking to existing"
            ),
        )

    # Below all thresholds -> CREATE_NEW
    return LinkPlanEntry(
        candidate_id=candidate.candidate_id,
        action=LinkAction.CREATE_NEW,
        target_node_id=None,
        similarity_score=best_score,
        reason=(
            f"Best similarity ({best_score:.3f}) below link threshold "
            f"({config.similarity_threshold_link}); creating independent node"
        ),
    )


def _generate_links(candidate: MemoryCandidate, related_nodes: list[KnowledgeNode]) -> list[str]:
    """Generate [[wiki-link]] style bidirectional links for a candidate.

    Returns a list of link strings in the format [[node_title]] that can
    be embedded in the node's markdown content.
    """
    links: list[str] = []
    for node in related_nodes:
        link = f"[[{node.title}]]"
        if link not in links:
            links.append(link)
    return links


# ---------------------------------------------------------------------------
# Async planner entry points
# ---------------------------------------------------------------------------


async def plan_links(
    candidates: Sequence[MemoryCandidate],
    retrieval_provider: RetrievalProvider,
    config: CuratorConfig,
) -> LinkPlan:
    """Plan link/merge/create actions for a list of memory candidates.

    For each candidate:
    1. Build a query from the candidate's topic and tags
    2. Retrieve similar existing nodes via the provider
    3. Compute similarity scores
    4. Decide the appropriate action

    Args:
        candidates: Memory candidates to plan for.
        retrieval_provider: Async callable returning similar existing nodes.
        config: Curator configuration with similarity thresholds.

    Returns:
        A LinkPlan containing one LinkPlanEntry per candidate.
    """
    plan = LinkPlan()

    for candidate in candidates:
        # Skip discarded candidates
        if candidate.discard:
            plan.entries.append(
                LinkPlanEntry(
                    candidate_id=candidate.candidate_id,
                    action=LinkAction.CREATE_NEW,
                    target_node_id=None,
                    similarity_score=0.0,
                    reason=f"Candidate discarded: {candidate.discard_reason}",
                )
            )
            continue

        # Build retrieval query from topic + tags
        query_parts = [candidate.topic, *candidate.tags]
        query = " ".join(query_parts)

        # Retrieve similar nodes
        existing_nodes = await retrieval_provider(
            query, limit=config.max_retrieval_results
        )

        # Compute similarity scores
        matches: list[tuple[KnowledgeNode, float]] = []
        for node in existing_nodes:
            score = _compute_similarity_score(candidate, node)
            matches.append((node, score))

        # Decide action
        entry = _decide_action(candidate, matches, config)
        plan.entries.append(entry)

    return plan


async def plan_links_batch(
    candidate_batches: Sequence[Sequence[MemoryCandidate]],
    retrieval_provider: RetrievalProvider,
    config: CuratorConfig,
) -> list[LinkPlan]:
    """Process multiple candidate batches concurrently.

    Each batch is planned independently via plan_links(). All batches
    run concurrently using asyncio.gather.

    Args:
        candidate_batches: Multiple sequences of candidates to plan.
        retrieval_provider: Async callable returning similar existing nodes.
        config: Curator configuration with similarity thresholds.

    Returns:
        A list of LinkPlan objects, one per input batch.
    """
    tasks = [
        plan_links(batch, retrieval_provider, config)
        for batch in candidate_batches
    ]
    results = await asyncio.gather(*tasks)
    return list(results)


__all__ = [
    "RetrievalProvider",
    "_compute_similarity_score",
    "_decide_action",
    "_generate_links",
    "plan_links",
    "plan_links_batch",
]

