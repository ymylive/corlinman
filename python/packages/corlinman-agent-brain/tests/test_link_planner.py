"""Tests for corlinman_agent_brain.link_planner module.

Covers:
- _compute_similarity_score: Jaccard-based scoring, edge cases
- _decide_action: threshold boundaries, conflict/blocked handling
- _generate_links: wiki-link generation
- plan_links: async entry point with mock retrieval provider
- plan_links_batch: concurrent batch processing
"""

from __future__ import annotations

import pytest

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.link_planner import (
    _compute_similarity_score,
    _decide_action,
    _generate_links,
    plan_links,
    plan_links_batch,
)
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> CuratorConfig:
    return CuratorConfig()


def _make_candidate(
    *,
    candidate_id: str = "cand-1",
    topic: str = "pytest testing framework",
    kind: MemoryKind = MemoryKind.DECISION,
    tags: list[str] | None = None,
    confidence: float = 0.8,
    risk: RiskLevel = RiskLevel.LOW,
    discard: bool = False,
    discard_reason: str = "",
) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id=candidate_id,
        topic=topic,
        kind=kind,
        summary="Test summary.",
        evidence=["some evidence"],
        confidence=confidence,
        risk=risk,
        source_session_id="sess-001",
        agent_id="agent-x",
        tenant_id="tenant-a",
        tags=tags if tags is not None else ["testing", "python"],
        discard=discard,
        discard_reason=discard_reason,
    )


def _make_node(
    *,
    node_id: str = "node-1",
    title: str = "pytest testing framework",
    kind: MemoryKind = MemoryKind.DECISION,
    tags: list[str] | None = None,
) -> KnowledgeNode:
    return KnowledgeNode(
        node_id=node_id,
        title=title,
        path="decisions/pytest-testing-framework.md",
        kind=kind,
        frontmatter=KnowledgeNodeFrontmatter(
            id=node_id,
            tenant_id="tenant-a",
            agent_id="agent-x",
            scope=NodeScope.AGENT,
            kind=kind,
            status=NodeStatus.ACTIVE,
            confidence=0.9,
            risk=RiskLevel.LOW,
            tags=tags if tags is not None else ["testing", "python"],
        ),
        summary="Existing node about pytest.",
    )


# ---------------------------------------------------------------------------
# Tests: _compute_similarity_score
# ---------------------------------------------------------------------------


class TestComputeSimilarityScore:
    def test_identical_topic_and_tags(self) -> None:
        candidate = _make_candidate(topic="pytest testing", tags=["testing", "python"])
        node = _make_node(title="pytest testing", tags=["testing", "python"])
        score = _compute_similarity_score(candidate, node)
        assert score == pytest.approx(1.0)

    def test_completely_different(self) -> None:
        candidate = _make_candidate(topic="database migration", tags=["sql", "postgres"])
        node = _make_node(title="frontend styling", tags=["css", "react"])
        score = _compute_similarity_score(candidate, node)
        assert score == 0.0

    def test_partial_overlap_topic(self) -> None:
        candidate = _make_candidate(topic="pytest unit testing", tags=[])
        node = _make_node(title="pytest integration testing", tags=[])
        score = _compute_similarity_score(candidate, node)
        assert 0.0 < score < 1.0

    def test_partial_overlap_tags(self) -> None:
        candidate = _make_candidate(topic="something unique", tags=["python", "testing", "ci"])
        node = _make_node(title="totally different", tags=["python", "testing", "deploy"])
        score = _compute_similarity_score(candidate, node)
        assert score == pytest.approx(0.2)

    def test_empty_tags_both(self) -> None:
        candidate = _make_candidate(topic="hello world", tags=[])
        node = _make_node(title="hello world", tags=[])
        score = _compute_similarity_score(candidate, node)
        assert score == pytest.approx(0.6)

    def test_case_insensitive(self) -> None:
        candidate = _make_candidate(topic="Pytest Testing", tags=["Python"])
        node = _make_node(title="pytest testing", tags=["python"])
        score = _compute_similarity_score(candidate, node)
        assert score == pytest.approx(1.0)

    def test_single_word_topic(self) -> None:
        candidate = _make_candidate(topic="docker", tags=[])
        node = _make_node(title="docker", tags=[])
        score = _compute_similarity_score(candidate, node)
        assert score == pytest.approx(0.6)

    def test_empty_topic_both(self) -> None:
        candidate = _make_candidate(topic="", tags=["a", "b"])
        node = _make_node(title="", tags=["a", "b"])
        score = _compute_similarity_score(candidate, node)
        assert score == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Tests: _decide_action
# ---------------------------------------------------------------------------


class TestDecideAction:
    def test_no_matches_creates_new(self, config: CuratorConfig) -> None:
        candidate = _make_candidate()
        entry = _decide_action(candidate, [], config)
        assert entry.action == LinkAction.CREATE_NEW
        assert entry.target_node_id is None
        assert entry.similarity_score == 0.0

    def test_high_similarity_same_kind_updates(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node = _make_node(kind=MemoryKind.DECISION)
        matches = [(node, 0.85)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.UPDATE_EXISTING
        assert entry.target_node_id == "node-1"
        assert entry.similarity_score == 0.85

    def test_high_similarity_different_kind_does_not_update(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node = _make_node(kind=MemoryKind.CONCEPT)
        matches = [(node, 0.85)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.CREATE_AND_LINK

    def test_medium_similarity_same_kind_merges(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node = _make_node(kind=MemoryKind.DECISION)
        matches = [(node, 0.7)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.MERGE_INTO_EXISTING
        assert entry.target_node_id == "node-1"

    def test_low_moderate_similarity_creates_and_links(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node = _make_node(kind=MemoryKind.CONCEPT)
        matches = [(node, 0.5)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.CREATE_AND_LINK
        assert entry.target_node_id == "node-1"

    def test_below_all_thresholds_creates_new(self, config: CuratorConfig) -> None:
        candidate = _make_candidate()
        node = _make_node()
        matches = [(node, 0.2)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.CREATE_NEW
        assert entry.target_node_id is None
        assert entry.similarity_score == 0.2

    def test_blocked_risk_sends_to_review(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(risk=RiskLevel.BLOCKED)
        node = _make_node()
        matches = [(node, 0.9)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.SEND_TO_REVIEW
        assert entry.target_node_id == "node-1"

    def test_conflict_kind_sends_to_review(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.CONFLICT, risk=RiskLevel.LOW)
        matches = []
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.SEND_TO_REVIEW
        assert entry.target_node_id is None

    def test_conflict_kind_with_matches(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.CONFLICT)
        node = _make_node()
        matches = [(node, 0.95)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.SEND_TO_REVIEW
        assert entry.target_node_id == "node-1"
        assert entry.similarity_score == 0.95

    def test_boundary_exactly_at_update_threshold(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node = _make_node(kind=MemoryKind.DECISION)
        matches = [(node, 0.8)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.MERGE_INTO_EXISTING

    def test_boundary_exactly_at_merge_threshold(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node = _make_node(kind=MemoryKind.DECISION)
        matches = [(node, 0.6)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.CREATE_AND_LINK

    def test_boundary_exactly_at_link_threshold(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node = _make_node(kind=MemoryKind.DECISION)
        matches = [(node, 0.4)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.CREATE_NEW

    def test_multiple_matches_uses_best(self, config: CuratorConfig) -> None:
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node_low = _make_node(node_id="node-low", kind=MemoryKind.DECISION)
        node_high = _make_node(node_id="node-high", kind=MemoryKind.DECISION)
        matches = [(node_low, 0.3), (node_high, 0.85)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.UPDATE_EXISTING
        assert entry.target_node_id == "node-high"

    def test_reason_string_populated(self, config: CuratorConfig) -> None:
        candidate = _make_candidate()
        entry = _decide_action(candidate, [], config)
        assert len(entry.reason) > 0

    def test_custom_thresholds(self) -> None:
        config = CuratorConfig(
            similarity_threshold_update=0.9,
            similarity_threshold_merge=0.7,
            similarity_threshold_link=0.5,
        )
        candidate = _make_candidate(kind=MemoryKind.DECISION)
        node = _make_node(kind=MemoryKind.DECISION)
        matches = [(node, 0.85)]
        entry = _decide_action(candidate, matches, config)
        assert entry.action == LinkAction.MERGE_INTO_EXISTING


# ---------------------------------------------------------------------------
# Tests: _generate_links
# ---------------------------------------------------------------------------


class TestGenerateLinks:
    def test_generates_wiki_links(self) -> None:
        nodes = [
            _make_node(node_id="n1", title="First Node"),
            _make_node(node_id="n2", title="Second Node"),
        ]
        candidate = _make_candidate()
        links = _generate_links(candidate, nodes)
        assert links == ["[[First Node]]", "[[Second Node]]"]

    def test_empty_nodes_returns_empty(self) -> None:
        candidate = _make_candidate()
        links = _generate_links(candidate, [])
        assert links == []

    def test_deduplicates_links(self) -> None:
        node = _make_node(title="Same Title")
        candidate = _make_candidate()
        links = _generate_links(candidate, [node, node])
        assert links == ["[[Same Title]]"]

    def test_single_node(self) -> None:
        node = _make_node(title="Only Node")
        candidate = _make_candidate()
        links = _generate_links(candidate, [node])
        assert links == ["[[Only Node]]"]


# ---------------------------------------------------------------------------
# Tests: plan_links (async)
# ---------------------------------------------------------------------------


class TestPlanLinks:
    @pytest.mark.asyncio
    async def test_no_existing_nodes_creates_new(self, config: CuratorConfig) -> None:
        async def empty_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            return []

        candidate = _make_candidate()
        plan = await plan_links([candidate], empty_retrieval, config)
        assert len(plan.entries) == 1
        assert plan.entries[0].action == LinkAction.CREATE_NEW
        assert plan.entries[0].candidate_id == "cand-1"

    @pytest.mark.asyncio
    async def test_high_similarity_match_updates(self, config: CuratorConfig) -> None:
        node = _make_node(
            title="pytest testing framework",
            kind=MemoryKind.DECISION,
            tags=["testing", "python"],
        )

        async def matching_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            return [node]

        candidate = _make_candidate(
            topic="pytest testing framework",
            kind=MemoryKind.DECISION,
            tags=["testing", "python"],
        )
        plan = await plan_links([candidate], matching_retrieval, config)
        assert len(plan.entries) == 1
        assert plan.entries[0].action == LinkAction.UPDATE_EXISTING
        assert plan.entries[0].target_node_id == "node-1"

    @pytest.mark.asyncio
    async def test_discarded_candidate_skipped(self, config: CuratorConfig) -> None:
        async def should_not_be_called(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            raise AssertionError("Retrieval should not be called for discarded candidates")

        candidate = _make_candidate(discard=True, discard_reason="Too ephemeral")
        plan = await plan_links([candidate], should_not_be_called, config)
        assert len(plan.entries) == 1
        assert plan.entries[0].action == LinkAction.CREATE_NEW
        assert "discarded" in plan.entries[0].reason.lower()

    @pytest.mark.asyncio
    async def test_multiple_candidates(self, config: CuratorConfig) -> None:
        async def empty_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            return []

        candidates = [
            _make_candidate(candidate_id="c1", topic="first topic"),
            _make_candidate(candidate_id="c2", topic="second topic"),
            _make_candidate(candidate_id="c3", topic="third topic"),
        ]
        plan = await plan_links(candidates, empty_retrieval, config)
        assert len(plan.entries) == 3
        ids = [e.candidate_id for e in plan.entries]
        assert ids == ["c1", "c2", "c3"]

    @pytest.mark.asyncio
    async def test_query_includes_topic_and_tags(self, config: CuratorConfig) -> None:
        queries_received: list[str] = []

        async def tracking_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            queries_received.append(query)
            return []

        candidate = _make_candidate(topic="docker setup", tags=["containers", "devops"])
        await plan_links([candidate], tracking_retrieval, config)
        assert len(queries_received) == 1
        assert "docker setup" in queries_received[0]
        assert "containers" in queries_received[0]
        assert "devops" in queries_received[0]

    @pytest.mark.asyncio
    async def test_respects_max_retrieval_results(self) -> None:
        config = CuratorConfig(max_retrieval_results=3)
        limits_received: list[int] = []

        async def tracking_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            limits_received.append(limit)
            return []

        candidate = _make_candidate()
        await plan_links([candidate], tracking_retrieval, config)
        assert limits_received == [3]

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_empty_plan(self, config: CuratorConfig) -> None:
        async def empty_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            return []

        plan = await plan_links([], empty_retrieval, config)
        assert len(plan.entries) == 0


# ---------------------------------------------------------------------------
# Tests: plan_links_batch (async)
# ---------------------------------------------------------------------------


class TestPlanLinksBatch:
    @pytest.mark.asyncio
    async def test_processes_multiple_batches(self, config: CuratorConfig) -> None:
        async def empty_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            return []

        batch1 = [_make_candidate(candidate_id="b1-c1")]
        batch2 = [_make_candidate(candidate_id="b2-c1"), _make_candidate(candidate_id="b2-c2")]

        plans = await plan_links_batch([batch1, batch2], empty_retrieval, config)
        assert len(plans) == 2
        assert len(plans[0].entries) == 1
        assert len(plans[1].entries) == 2

    @pytest.mark.asyncio
    async def test_empty_batches(self, config: CuratorConfig) -> None:
        async def empty_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            return []

        plans = await plan_links_batch([], empty_retrieval, config)
        assert plans == []

    @pytest.mark.asyncio
    async def test_batch_with_empty_sequence(self, config: CuratorConfig) -> None:
        async def empty_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            return []

        plans = await plan_links_batch([[], []], empty_retrieval, config)
        assert len(plans) == 2
        assert len(plans[0].entries) == 0
        assert len(plans[1].entries) == 0

    @pytest.mark.asyncio
    async def test_results_correspond_to_input_order(self, config: CuratorConfig) -> None:
        async def empty_retrieval(query: str, *, limit: int = 5) -> list[KnowledgeNode]:
            return []

        batch1 = [_make_candidate(candidate_id="first")]
        batch2 = [_make_candidate(candidate_id="second")]
        batch3 = [_make_candidate(candidate_id="third")]

        plans = await plan_links_batch([batch1, batch2, batch3], empty_retrieval, config)
        assert plans[0].entries[0].candidate_id == "first"
        assert plans[1].entries[0].candidate_id == "second"
        assert plans[2].entries[0].candidate_id == "third"
