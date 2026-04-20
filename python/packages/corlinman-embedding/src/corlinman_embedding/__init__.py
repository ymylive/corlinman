"""corlinman-embedding — local or remote embedding dispatch.

Responsibility: present a single ``embed(texts) -> list[list[float]]`` API
to the agent loop; route to a local sentence-transformers pool or to a
remote provider per config. See plan §5.2 RAG data flow.

Also exposes the :mod:`corlinman_embedding.rerank_client` backends
(local cross-encoder / remote HTTP) used by the optional RAG rerank
stage (Sprint 3 T6).

TODO(M4): implement the router, local pool, and remote client.
"""

from __future__ import annotations

from corlinman_embedding.rerank_client import (
    LocalRerankProvider,
    RemoteRerankProvider,
    RerankHit,
    RerankProvider,
)

__all__: list[str] = [
    "LocalRerankProvider",
    "RemoteRerankProvider",
    "RerankHit",
    "RerankProvider",
]
