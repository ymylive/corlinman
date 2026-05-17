"""Cross-encoder rerank stage sitting on top of RRF fusion.

Python port of :mod:`corlinman_vector::rerank`. Defines a :class:`Reranker`
ABC plus the default :class:`NoopReranker` passthrough. The actual remote
cross-encoder client lives in :mod:`corlinman_embedding.rerank_client`
(``RemoteRerankProvider`` / ``LocalRerankProvider``) — this module is
deliberately decoupled from those backends so the import graph stays
small.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corlinman_embedding.vector.fusion import RagHit

__all__ = ["Reranker", "NoopReranker"]


class Reranker(ABC):
    """Contract for any post-RRF reranker."""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        hits: list["RagHit"],
        top_k: int,
    ) -> list["RagHit"]:
        """Re-order ``hits`` and return the best ``top_k``."""


class NoopReranker(Reranker):
    """Passthrough reranker. Just truncates to ``top_k`` without changing order.

    Default wired into :class:`HybridSearcher` so existing callers who don't
    care about rerank keep their RRF-only behaviour.
    """

    async def rerank(
        self,
        query: str,  # noqa: ARG002 - intentional passthrough
        hits: list["RagHit"],
        top_k: int,
    ) -> list["RagHit"]:
        return list(hits[:top_k])

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "NoopReranker()"
