"""Episode evidence lookup for goal reflection (D1 ↔ D2 bridge).

The reflection job (iter 5) needs a tier-windowed list of episodes to
hand to the LLM grader. D1 (``corlinman-episodes``) owns episode
storage; this module is the thin read-only adapter that stays inside
the D2 package so we never edit D1's surface.

Two layers:

- :class:`EpisodeEvidence` — protocol the reflection job depends on.
  Stubs in tests, real implementation in production. Decouples reflection
  from the D1 store shape so a future swap (e.g. vector-only retrieval)
  doesn't ripple.
- :class:`EpisodesStoreEvidence` — the D1-backed implementation. Opens
  the per-tenant ``episodes.sqlite`` D1 wrote and runs a single
  bounded SQL window scan.

The window math comes from :mod:`corlinman_goals.windows`. We pass the
``Window`` in pre-built so reflection can adjust ``start_ms`` for
partial windows (goal created Wednesday, scored Sunday → window is
``(created_at, Sunday)``, not the full week).

Why a bare SQL query rather than calling a D1 method? ``EpisodesStore``
in D1 doesn't currently expose a "list by window" API — its read-side
contracts are ``find_episode_by_natural_key`` and the embed-pending
sweep. Adding one would be a D1 edit, which the iter brief forbids
("READ-ONLY on every D1 file"). Instead we treat the open
``aiosqlite.Connection`` as the contract — the schema in D1 is stable
(see ``docs/design/phase4-w4-d1-design.md``) and the columns we touch
(``id, started_at, ended_at, summary_text, kind, importance_score``)
are the public-by-design fields the gateway ``{{episodes.*}}`` resolver
also reads.

Multi-tenant safety: every query carries ``tenant_id`` in the WHERE
clause and we never cross databases. One ``EpisodesStoreEvidence``
instance is bound to one ``(data_dir, tenant_id)`` pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import aiosqlite

from corlinman_goals.windows import Window

# Cap the number of episodes we ever pull for one goal evaluation. The
# design's ``[goals.reflection]`` block sets ``evidence_max_episodes =
# 8`` — we hard-cap at the SQL layer so a chatty week doesn't blow up
# the LLM prompt. Surfaced as a constant so reflection (iter 5) can
# import the same number for its prompt-budget guard.
DEFAULT_EVIDENCE_LIMIT: Final[int] = 8


@dataclass(frozen=True)
class EvidenceEpisode:
    """Lightweight projection of one D1 episode for grading.

    Frozen so the reflection job can pass the list through to the LLM
    prompt builder and trust ids stay stable. Only carries the columns
    the grader actually shows the model — embeddings, source-key
    joins, distillation provenance all stay in D1.

    ``importance_score`` rides along so reflection can sort or
    truncate by it before hitting the prompt-budget cap (current
    iter 4: pure most-recent-first; iter 7+ may layer importance in).
    """

    episode_id: str
    started_at_ms: int
    ended_at_ms: int
    kind: str
    summary_text: str
    importance_score: float


@runtime_checkable
class EpisodeEvidence(Protocol):
    """Read-only episode lookup the reflection job depends on.

    Single method, single shape — keeps the surface tiny so a stub can
    be a one-liner ``return self._fixture``. Real impl is
    :class:`EpisodesStoreEvidence`.

    The protocol is :func:`runtime_checkable` so tests can assert
    fixtures conform without inheriting; the cost (one ``isinstance``
    check) is paid only at test time.
    """

    async def fetch(
        self,
        *,
        agent_id: str,
        window: Window,
        limit: int = DEFAULT_EVIDENCE_LIMIT,
    ) -> list[EvidenceEpisode]:
        """Return episodes whose ``[started_at, ended_at)`` overlaps
        ``window``, most-recent-first, capped at ``limit``.

        ``agent_id`` is currently unused by the D1-backed impl —
        D1 episodes are tenant-scoped, not agent-scoped (one tenant
        often hosts one agent today; the column is pre-staged for
        Phase 5 multi-agent tenants). The kwarg lives in the protocol
        so a future agent-scoped impl drops in without a signature
        churn.
        """
        ...


class EpisodesStoreEvidence:
    """D1-backed :class:`EpisodeEvidence` implementation.

    Opens a *read-only* connection to ``<data_dir>/tenants/<t>/
    episodes.sqlite`` (the per-tenant path D1's runner writes). We
    don't go through ``corlinman_episodes.EpisodesStore`` because that
    surface is async-context-manager-shaped and would tie reflection's
    lifetime to ours; an explicit ``aiosqlite.connect`` is simpler and
    keeps D1 untouched.

    The connection stays open across ``fetch`` calls for the life of
    the reflection run (~one window, dozens of goals) and is released
    by :meth:`close` or the ``async with`` exit. Reflection uses one
    instance per (tenant, run).
    """

    # SQL we never want to drift from D1's schema. The columns are
    # the same ones the gateway ``{{episodes.*}}`` resolver reads, so
    # this query and that resolver both break loudly if D1 renames a
    # column — there's no silent skew.
    _WINDOW_SQL: Final[str] = (
        "SELECT id, started_at, ended_at, kind, summary_text, "
        "importance_score "
        "FROM episodes "
        "WHERE tenant_id = ? "
        "  AND started_at < ? "
        "  AND ended_at > ? "
        "ORDER BY ended_at DESC, id DESC "
        "LIMIT ?"
    )

    def __init__(
        self,
        *,
        episodes_db_path: Path,
        tenant_id: str,
    ) -> None:
        self._path = episodes_db_path
        self._tenant_id = tenant_id
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open(
        cls,
        *,
        episodes_db_path: Path,
        tenant_id: str,
    ) -> EpisodesStoreEvidence:
        """Open a connection and return an entered evidence source.

        Mirrors :meth:`GoalStore.open_or_create` so callers that prefer
        non-context-manager framing (CLI subcommands) get a familiar
        shape. Caller is responsible for ``await ev.close()``.
        """
        ev = cls(episodes_db_path=episodes_db_path, tenant_id=tenant_id)
        await ev._open()
        return ev

    async def __aenter__(self) -> EpisodesStoreEvidence:
        await self._open()
        return self

    async def __aexit__(
        self, exc_type: object, exc: object, tb: object
    ) -> None:
        await self.close()

    async def _open(self) -> None:
        # ``mode=ro`` would be ideal but aiosqlite's ``connect`` doesn't
        # plumb URI flags cleanly; instead we trust the caller's intent
        # and never issue a write statement. The schema-version row
        # (D1's ``schema_version=1``) is the canary if D1 evolves.
        self._conn = await aiosqlite.connect(self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "EpisodesStoreEvidence used outside async context"
            )
        return self._conn

    async def fetch(
        self,
        *,
        agent_id: str,
        window: Window,
        limit: int = DEFAULT_EVIDENCE_LIMIT,
    ) -> list[EvidenceEpisode]:
        """Window-overlap query against the D1 episodes table.

        Overlap test: ``episode.started_at < window.end_ms AND
        episode.ended_at > window.start_ms``. This catches episodes
        that started before the window but ended inside it (a
        Sunday-evening conversation that ran into Monday is "this
        week's evidence" by either tier's view), plus episodes that
        started inside but are still open at the cutoff.

        ``limit`` is applied AFTER the ORDER BY, so we always keep the
        most recent ``limit`` episodes — the older tail gets dropped if
        the window is chatty, which is the design's intent (the LLM
        sees the freshest signal first).
        """
        # ``agent_id`` is intentionally not part of the query — see
        # protocol docstring. Bind it locally so a debugger sees the
        # value on a stack frame.
        del agent_id
        cursor = await self.conn.execute(
            self._WINDOW_SQL,
            (
                self._tenant_id,
                int(window.end_ms),
                int(window.start_ms),
                int(limit),
            ),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            EvidenceEpisode(
                episode_id=str(r[0]),
                started_at_ms=int(r[1]),
                ended_at_ms=int(r[2]),
                kind=str(r[3]),
                summary_text=str(r[4]),
                importance_score=float(r[5]),
            )
            for r in rows
        ]


class StaticEvidence:
    """In-memory :class:`EpisodeEvidence` for tests + dry-runs.

    Holds a fixed list and applies the same window-overlap + limit
    rules in Python the SQL impl applies in SQLite. Two-second test
    setup — no fixture DB to seed.

    The sort matches :class:`EpisodesStoreEvidence`'s ``ORDER BY
    ended_at DESC, id DESC`` so callers comparing the two impls see
    the same ordering.
    """

    def __init__(self, episodes: list[EvidenceEpisode]) -> None:
        self._episodes = list(episodes)

    async def fetch(
        self,
        *,
        agent_id: str,
        window: Window,
        limit: int = DEFAULT_EVIDENCE_LIMIT,
    ) -> list[EvidenceEpisode]:
        del agent_id
        # Half-open overlap: same predicate as the SQL impl.
        matched = [
            e
            for e in self._episodes
            if e.started_at_ms < window.end_ms
            and e.ended_at_ms > window.start_ms
        ]
        matched.sort(
            key=lambda e: (e.ended_at_ms, e.episode_id), reverse=True
        )
        return matched[:limit]


__all__ = [
    "DEFAULT_EVIDENCE_LIMIT",
    "EpisodeEvidence",
    "EpisodesStoreEvidence",
    "EvidenceEpisode",
    "StaticEvidence",
]
