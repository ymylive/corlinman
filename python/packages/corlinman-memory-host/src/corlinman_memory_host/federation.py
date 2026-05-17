"""Fan-out :class:`MemoryHost` that queries a set of sub-hosts in
parallel and merges the per-host rankings.

Python port of ``rust/crates/corlinman-memory-host/src/federation.rs``.
Ships Reciprocal Rank Fusion only; :class:`FusionStrategy` leaves room
for weighted-average / learned-fusion later (mirrors the Rust
``#[non_exhaustive]`` enum).

Failure model: a sub-host that errors or times out is logged at
``warning`` and **skipped**. A single failing backend never fails the
whole federated query. If every sub-host fails (or returns empty),
``query`` returns ``[]``.

``upsert`` / ``delete`` raise :class:`MemoryHostError` — the federator
does not own a canonical namespace and so has no meaningful semantics.
Call the specific sub-host directly for writes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from corlinman_memory_host.base import MemoryHost
from corlinman_memory_host.types import (
    MemoryDoc,
    MemoryHit,
    MemoryHostError,
    MemoryQuery,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fusion strategy (sealed enum analogue)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FusionStrategy:
    """Merge strategy applied to per-host result sets.

    Forward-compatible: extra variants (weighted-average, learned) will
    keep the ``kind`` discriminator and add fields. Existing constructors
    keep working."""

    kind: str
    # Reciprocal Rank Fusion constant ``k``. Cormack et al. 2009 use 60.
    k: float = 60.0

    @classmethod
    def rrf(cls, k: float = 60.0) -> FusionStrategy:
        return cls(kind="rrf", k=k)

    @classmethod
    def rrf_default(cls) -> FusionStrategy:
        """RRF with the canonical ``k = 60``."""
        return cls(kind="rrf", k=60.0)


# ---------------------------------------------------------------------------
# FederatedMemoryHost
# ---------------------------------------------------------------------------


class FederatedMemoryHost(MemoryHost):
    """A :class:`MemoryHost` that fans out to child hosts and merges
    results."""

    def __init__(
        self,
        host_name: str,
        hosts: list[MemoryHost],
        strategy: FusionStrategy,
    ) -> None:
        self._name = host_name
        self._hosts: list[MemoryHost] = list(hosts)
        self._strategy = strategy

    @classmethod
    def with_rrf(
        cls, host_name: str, hosts: list[MemoryHost]
    ) -> FederatedMemoryHost:
        """Convenience constructor using :meth:`FusionStrategy.rrf_default`."""
        return cls(host_name, hosts, FusionStrategy.rrf_default())

    @property
    def host_count(self) -> int:
        """Number of sub-hosts wired into this federation."""
        return len(self._hosts)

    # ---- MemoryHost surface -----------------------------------------------

    def name(self) -> str:
        return self._name

    async def query(self, req: MemoryQuery) -> list[MemoryHit]:
        if not self._hosts or req.top_k == 0:
            return []

        # Fan out. Every sub-host sees an identical request; the
        # federator doesn't rewrite filters.
        async def _run(h: MemoryHost) -> tuple[str, list[MemoryHit] | BaseException]:
            try:
                return (h.name(), await h.query(req))
            except BaseException as exc:
                return (h.name(), exc)

        results = await asyncio.gather(*(_run(h) for h in self._hosts))

        ranked_lists: list[list[MemoryHit]] = []
        for host_name, res in results:
            if isinstance(res, BaseException):
                _log.warning(
                    "federated sub-host %s failed; skipping: %s", host_name, res
                )
                continue
            if res:
                ranked_lists.append(res)

        if not ranked_lists:
            return []

        return _fuse(ranked_lists, self._strategy, req.top_k)

    async def upsert(self, doc: MemoryDoc) -> str:
        _ = doc
        raise MemoryHostError(
            "FederatedMemoryHost does not support upsert; call a specific sub-host"
        )

    async def delete(self, doc_id: str) -> None:
        _ = doc_id
        raise MemoryHostError(
            "FederatedMemoryHost does not support delete; call a specific sub-host"
        )


# ---------------------------------------------------------------------------
# Fusion math
# ---------------------------------------------------------------------------


def _fuse(
    ranked_lists: list[list[MemoryHit]],
    strategy: FusionStrategy,
    top_k: int,
) -> list[MemoryHit]:
    """Merge a set of ranked hit lists into one global top-``top_k`` list.

    Identity for de-duplication is ``(source, id)`` — two different hosts
    returning the same internal id are kept as separate hits. When the
    same ``(source, id)`` shows up in multiple input lists we sum their
    reciprocal-rank contributions (defensive; in practice each host
    contributes only once)."""
    # Forward-compat path matching the Rust ``match`` on the
    # non-exhaustive enum: unknown variants fall through to RRF with
    # whatever ``k`` was provided.
    k = strategy.k

    # Keyed by (source, id). Value = (fused_score, representative hit).
    scores: dict[tuple[str, str], tuple[float, MemoryHit]] = {}
    for hit_list in ranked_lists:
        for rank, hit in enumerate(hit_list):
            contribution = 1.0 / (k + float(rank) + 1.0)
            key = (hit.source, hit.id)
            existing = scores.get(key)
            if existing is None:
                scores[key] = (contribution, hit)
            else:
                scores[key] = (existing[0] + contribution, existing[1])

    fused: list[tuple[float, MemoryHit]] = []
    for (score, hit) in scores.values():
        # Overwrite per-host score with fused score so downstream code
        # sees a directly-comparable number — same as Rust.
        hit.score = score
        fused.append((score, hit))

    # Descending by fused score; stable tie-break by (source, id) so the
    # tests are deterministic.
    fused.sort(key=lambda pair: (-pair[0], pair[1].source, pair[1].id))
    return [h for (_, h) in fused[:top_k]]


__all__ = ["FederatedMemoryHost", "FusionStrategy"]
