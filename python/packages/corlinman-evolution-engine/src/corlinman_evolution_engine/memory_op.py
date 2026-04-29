"""Generate ``memory_op`` proposals from near-duplicate kb chunks.

Phase 2 ships exactly one memory_op flavour: ``merge_chunks``. The detector
walks ``kb.sqlite`` chunks pairwise, computing Jaccard similarity over
whitespace-tokenised content. Pairs above ``similarity_threshold`` (default
0.95 — almost-identical) become merge proposals.

We deliberately avoid embeddings, BM25 indexes, or any other heavy
machinery. The signal density of "two chunks with >95% token overlap" is
already high; Phase 3 can swap in vector cosine if needed.

A complete pass over a kb is O(n^2) on the chunk count. The CLI exposes a
``--max-chunks`` knob; for production kbs the engine will read a bounded
recent slice and rely on signal-driven scheduling so we don't rescan
everything every run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from corlinman_evolution_engine.proposals import EvolutionProposal, ProposalContext
from corlinman_evolution_engine.store import ChunkRow, KbStore, fetch_existing_targets

if TYPE_CHECKING:
    import aiosqlite

_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)

KIND_MEMORY_OP = "memory_op"


def _tokenise(text: str) -> frozenset[str]:
    """Lowercased word-token set. Empty strings → empty set (no false hits)."""
    return frozenset(t.lower() for t in _TOKEN_RE.findall(text or ""))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Standard Jaccard index. Two empty sets → 0.0 (treated as "no signal")."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass(frozen=True)
class DuplicatePair:
    """One near-duplicate chunk pair with its similarity score."""

    chunk_a: int
    chunk_b: int
    similarity: float

    @property
    def merge_target(self) -> str:
        """The ``target`` string for an ``EvolutionProposal``.

        Format: ``merge_chunks:<lower_id>,<higher_id>``. Stable across
        argument order so dedup keys line up.
        """
        lo, hi = sorted((self.chunk_a, self.chunk_b))
        return f"merge_chunks:{lo},{hi}"


def find_near_duplicate_pairs(
    chunks: list[ChunkRow],
    *,
    similarity_threshold: float = 0.95,
    min_token_count: int = 4,
) -> list[DuplicatePair]:
    """All chunk pairs whose Jaccard similarity is ``>= similarity_threshold``.

    ``min_token_count`` filters out trivially-short chunks where Jaccard is
    unstable (a one-word chunk matches every other one-word chunk). Callers
    can bypass with ``min_token_count=0``.
    """
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError(
            f"similarity_threshold out of range [0,1]: {similarity_threshold}"
        )

    # Pre-tokenise once. Memory cost is bounded by chunk count, not n^2.
    tokenised: list[tuple[ChunkRow, frozenset[str]]] = [
        (c, _tokenise(c.content)) for c in chunks
    ]
    tokenised = [(c, t) for c, t in tokenised if len(t) >= min_token_count]

    pairs: list[DuplicatePair] = []
    for i, (chunk_i, tokens_i) in enumerate(tokenised):
        for chunk_j, tokens_j in tokenised[i + 1 :]:
            score = jaccard(tokens_i, tokens_j)
            if score >= similarity_threshold:
                pairs.append(
                    DuplicatePair(
                        chunk_a=chunk_i.id,
                        chunk_b=chunk_j.id,
                        similarity=score,
                    )
                )
    # Strongest matches first — the run budget caps how many we file.
    pairs.sort(key=lambda p: p.similarity, reverse=True)
    return pairs


def reasoning_for(pair: DuplicatePair) -> str:
    """Human-readable ``reasoning`` field for a merge proposal."""
    return (
        f"near-duplicate detected: chunks {pair.chunk_a} and {pair.chunk_b} "
        f"share {pair.similarity:.2%} of their tokens"
    )


# ---------------------------------------------------------------------------
# Strategy implementation
# ---------------------------------------------------------------------------


class MemoryOpHandler:
    """``KindHandler`` for the ``memory_op`` kind.

    Phase 2 implementation: scan ``kb.sqlite`` for near-duplicate chunks
    and emit one merge proposal per pair.

    ``existing_targets`` reads ``evolution_proposals`` to dedup against
    targets already filed (any status). The engine owns the connection so
    the handler stays free of database lifecycle concerns.
    """

    @property
    def kind(self) -> str:
        return KIND_MEMORY_OP

    async def existing_targets(self, conn: object) -> set[tuple[str, str]]:
        # ``conn`` is the engine's ``aiosqlite.Connection``. Typed loosely
        # in the protocol so KindHandler doesn't drag aiosqlite into every
        # caller; cast here.
        sqlite_conn: aiosqlite.Connection = conn  # type: ignore[assignment]
        return await fetch_existing_targets(sqlite_conn, self.kind)

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        # Per-handler: open + close kb connection so the engine doesn't have
        # to know about kb.sqlite. Phase 3 handlers reading transcripts /
        # tag trees follow the same pattern.
        async with KbStore(ctx.kb_path) as kb:
            chunks = await kb.list_chunks(limit=ctx.max_chunks_scanned)

        pairs = find_near_duplicate_pairs(
            chunks,
            similarity_threshold=ctx.similarity_threshold,
        )
        if not pairs:
            return []

        # Triggering signals — same set for every proposal in this batch
        # because the cluster only gates the scan; per-pair attribution is
        # a Phase 3 refinement.
        signal_ids: list[int] = []
        trace_ids: list[str] = []
        seen_traces: set[str] = set()
        for c in ctx.clusters:
            signal_ids.extend(c.signal_ids)
            for t in c.trace_ids:
                if t not in seen_traces:
                    seen_traces.add(t)
                    trace_ids.append(t)

        # The kb the chunks come from is itself per-tenant in Phase 4 W1
        # 4-1A; every cluster gating this scan therefore shares one
        # tenant. We grab it from the first cluster (clusters are
        # presence-checked above).
        tenant_id = ctx.clusters[0].tenant_id if ctx.clusters else "default"

        return [
            EvolutionProposal(
                kind=self.kind,
                target=pair.merge_target,
                diff="",  # memory_ops encode the operation in target, not diff.
                reasoning=reasoning_for(pair),
                risk="low",
                budget_cost=0,
                signal_ids=signal_ids,
                trace_ids=trace_ids,
                tenant_id=tenant_id,
            )
            for pair in pairs
        ]
