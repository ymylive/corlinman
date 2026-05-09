"""Cold archival sweep — Phase 4 W4 D1 iter 8.

Per ``docs/design/phase4-w4-d1-design.md`` §"Decay / pruning":

- Episodes are **never deleted** — they're the audit trail; deleting
  breaks downstream provenance.
- After ``cold_archive_days`` (default 180) without a render hit,
  ``summary_text`` + ``embedding`` move to
  ``<root>/episodes_cold/<id>.md``; the row remains with NULL hot
  columns + sentinel ``summary_text='<archived:see cold>'``.
- Auto-rollback episodes (``EpisodeKind.INCIDENT``) and operator-flagged
  ``important=true`` (D1.5) are exempted — high-novelty narratives
  the operator may want to grep three years later.
- Reads transparently re-hydrate (handled in iter 9 via ``rehydrate_*``);
  writes never touch cold.

Design rationale for the on-disk shape (markdown with YAML-ish front
matter):

- The cold file is human-readable. An operator dropping into the
  ``episodes_cold/`` dir with ``less`` or ``grep`` should see the
  summary in clear text — that's the *whole point* of the audit trail.
- The front matter keeps the embedding hex-encoded inline rather than
  in a sidecar binary. Episodes are low-volume; the readability win
  (one file = one episode, complete) outranks the size cost.
- The sentinel value carries a stable string the resolver (or a
  future re-hydration path) can match cheaply: ``ARCHIVED_SENTINEL``.

Tests in ``test_archive.py`` cover:
    - Archival skips rows referenced inside the cutoff.
    - Archival skips already-archived rows (idempotent re-run).
    - Archival exempts ``INCIDENT`` rows.
    - Archived row keeps id + tenant_id + kind + importance_score;
      hot columns blanked.
    - Cold file round-trips summary_text + embedding bytes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corlinman_episodes.config import DEFAULT_TENANT_ID, EpisodesConfig
from corlinman_episodes.store import EpisodeKind, EpisodesStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentinel value the row's ``summary_text`` carries after archival.
#: Stable across versions so a future re-hydration path can detect
#: archived rows without parsing the cold file or scanning the
#: filesystem first. Wrapped in angle brackets so it can never collide
#: with a real LLM-generated summary (which would be regular prose).
ARCHIVED_SENTINEL: str = "<archived:see cold>"

#: Set of kinds **never** archived — auto-rollback INCIDENT episodes
#: are the canonical never-cold case per design §"Decay / pruning"
#: line "Auto-rollback episodes + operator-flagged ``important=true``
#: (D1.5) are exempted from cold archival."
COLD_EXEMPT_KINDS: frozenset[str] = frozenset({EpisodeKind.INCIDENT.value})

#: Subdirectory (under the configured root) holding cold episode
#: files. One file per archived episode, named ``<episode_id>.md``.
#: Matches the design doc's path: ``<data_dir>/tenants/<slug>/episodes_cold/<id>.md``.
COLD_DIR_NAME: str = "episodes_cold"


# ---------------------------------------------------------------------------
# Result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveSummary:
    """Outcome of a single :func:`archive_unreferenced_episodes` call.

    Mirrors :class:`corlinman_episodes.runner.RunSummary` /
    :class:`corlinman_episodes.embed.EmbedSummary` in style — one
    structured record per pass so the operator/admin route can surface
    "archived N, skipped M, exempted K" without parsing logs.
    """

    tenant_id: str
    archived: int = 0
    skipped_recent: int = 0
    skipped_already_archived: int = 0
    skipped_exempt: int = 0
    archived_episode_ids: tuple[str, ...] = field(default_factory=tuple)
    cold_dir: str = ""
    bytes_written: int = 0


# ---------------------------------------------------------------------------
# Cold-file format
# ---------------------------------------------------------------------------


def cold_file_path(*, root: Path, episode_id: str) -> Path:
    """Compute the on-disk path for an archived episode.

    Splitting this out keeps the rendering layer + the hydration layer
    (iter 9) using the exact same path computation — a future migration
    that introduces sharding (e.g. ``<id-prefix>/<id>.md``) only has
    to land here.
    """
    return root / COLD_DIR_NAME / f"{episode_id}.md"


def render_cold_file(
    *,
    episode_id: str,
    tenant_id: str,
    kind: str,
    started_at: int,
    ended_at: int,
    importance_score: float,
    distilled_by: str,
    distilled_at: int,
    last_referenced_at: int | None,
    summary_text: str,
    embedding: bytes | None,
    embedding_dim: int | None,
) -> str:
    """Build the markdown payload for one cold-archive file.

    Front-matter is YAML-ish key:value pairs (no nested structures);
    that keeps the parser in :func:`parse_cold_file` (iter 9) trivial
    — split on ``---``, parse each ``key: value`` line — without
    pulling in a YAML lib.

    The embedding (if present) is hex-encoded inline. That's verbose
    but the rowcount is low and the operator-readability win is high
    (cf. design's "Cold storage is fair game" framing — verbose-but-
    readable beats compact-but-opaque for a 180-day-old narrative).
    """
    lines: list[str] = ["---"]
    lines.append(f"episode_id: {episode_id}")
    lines.append(f"tenant_id: {tenant_id}")
    lines.append(f"kind: {kind}")
    lines.append(f"started_at: {started_at}")
    lines.append(f"ended_at: {ended_at}")
    lines.append(f"importance_score: {importance_score}")
    lines.append(f"distilled_by: {distilled_by}")
    lines.append(f"distilled_at: {distilled_at}")
    if last_referenced_at is not None:
        lines.append(f"last_referenced_at: {last_referenced_at}")
    if embedding is not None and embedding_dim is not None:
        lines.append(f"embedding_dim: {embedding_dim}")
        lines.append(f"embedding_hex: {embedding.hex()}")
    lines.append("---")
    lines.append("")
    lines.append(summary_text)
    # Trailing newline so ``less``/``cat`` close cleanly.
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def archive_unreferenced_episodes(
    *,
    config: EpisodesConfig,
    store: EpisodesStore,
    cold_root: Path,
    tenant_id: str = DEFAULT_TENANT_ID,
    now_ms: int | None = None,
) -> ArchiveSummary:
    """Sweep episodes unreferenced for ``cold_archive_days`` and demote
    them to the cold archive on disk.

    Behaviour (matches the design's §"Decay / pruning"):

    1. Compute cutoff = ``now - cold_archive_days * 86_400_000`` ms.
    2. Select rows where:
        - ``tenant_id = ?``
        - ``summary_text != '<archived:see cold>'`` (idempotent re-runs)
        - ``kind NOT IN COLD_EXEMPT_KINDS`` (incident exemption)
        - ``COALESCE(last_referenced_at, ended_at) < cutoff`` —
          unreferenced rows fall back to ``ended_at`` so a never-
          rendered episode can still age out, matching the design's
          "180 days unreferenced" wording.
    3. For each candidate:
        a. Render the cold-file content.
        b. Write to ``<cold_root>/episodes_cold/<id>.md`` (creating
           directories as needed). Atomic write: ``tmp + os.replace``
           so a crashed pass never leaves a half-written cold file.
        c. UPDATE the row: ``summary_text=ARCHIVED_SENTINEL``,
           ``embedding=NULL``, ``embedding_dim=NULL``. Importance
           score + tenant + kind + ids stay (the row is still the
           audit-trail anchor; only the *hot* columns blank).

    Embeddings are nulled out alongside summary_text — once cold, the
    similarity-search path has to re-hydrate first. Per the design:
    "writes never touch cold" so the BLOB doesn't need to live on the
    row anymore.

    The function is naturally idempotent: re-running on the same data
    finds zero candidates because the sentinel filter excludes already-
    archived rows.
    """
    if not config.enabled:
        return ArchiveSummary(tenant_id=tenant_id)

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff_ms = now - int(config.cold_archive_days) * 86_400_000

    candidates = await _select_archive_candidates(
        store=store,
        tenant_id=tenant_id,
        cutoff_ms=cutoff_ms,
    )

    cold_dir = cold_root / COLD_DIR_NAME
    archived_ids: list[str] = []
    bytes_written = 0
    skipped_already_archived = 0
    skipped_exempt = 0

    for row in candidates:
        if row["summary_text"] == ARCHIVED_SENTINEL:
            # Defence in depth — the SELECT excludes these, but a
            # narrow race where two sweeps run back-to-back could
            # surface the same row twice if the second runs before the
            # first commits. The check is cheap.
            skipped_already_archived += 1
            continue
        if row["kind"] in COLD_EXEMPT_KINDS:
            # Same defence in depth — the SELECT excludes exempt kinds
            # too, but keeping the test in code lets a future kind
            # change land via the constant without re-auditing the SQL.
            skipped_exempt += 1
            continue

        cold_path = cold_file_path(root=cold_root, episode_id=row["id"])
        payload = render_cold_file(
            episode_id=row["id"],
            tenant_id=row["tenant_id"],
            kind=row["kind"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            importance_score=row["importance_score"],
            distilled_by=row["distilled_by"],
            distilled_at=row["distilled_at"],
            last_referenced_at=row["last_referenced_at"],
            summary_text=row["summary_text"],
            embedding=row["embedding"],
            embedding_dim=row["embedding_dim"],
        )
        _atomic_write(cold_path, payload)
        bytes_written += len(payload.encode("utf-8"))

        await _blank_hot_columns(store, episode_id=row["id"])
        archived_ids.append(row["id"])

    skipped_recent = max(0, len(candidates) - len(archived_ids) - skipped_already_archived - skipped_exempt)

    return ArchiveSummary(
        tenant_id=tenant_id,
        archived=len(archived_ids),
        skipped_recent=skipped_recent,
        skipped_already_archived=skipped_already_archived,
        skipped_exempt=skipped_exempt,
        archived_episode_ids=tuple(archived_ids),
        cold_dir=str(cold_dir),
        bytes_written=bytes_written,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _select_archive_candidates(
    *,
    store: EpisodesStore,
    tenant_id: str,
    cutoff_ms: int,
) -> list[dict[str, Any]]:
    """Select rows that should be archived this pass.

    Filters at SQL time (not in Python) so a tenant with millions of
    rows doesn't materialise the full table. The sentinel-text filter
    is the idempotency guard; the kind filter implements the
    auto-rollback exemption.
    """
    exempt_placeholders = ",".join(["?"] * len(COLD_EXEMPT_KINDS))
    exempt_values = tuple(sorted(COLD_EXEMPT_KINDS))
    sql = f"""
        SELECT id, tenant_id, started_at, ended_at, kind, summary_text,
               embedding, embedding_dim, importance_score,
               last_referenced_at, distilled_by, distilled_at
          FROM episodes
         WHERE tenant_id = ?
           AND summary_text != ?
           AND kind NOT IN ({exempt_placeholders})
           AND COALESCE(last_referenced_at, ended_at) < ?
         ORDER BY ended_at ASC
    """
    cursor = await store.conn.execute(
        sql,
        (tenant_id, ARCHIVED_SENTINEL, *exempt_values, int(cutoff_ms)),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [
        {
            "id": str(r[0]),
            "tenant_id": str(r[1]),
            "started_at": int(r[2]),
            "ended_at": int(r[3]),
            "kind": str(r[4]),
            "summary_text": str(r[5]),
            "embedding": bytes(r[6]) if r[6] is not None else None,
            "embedding_dim": int(r[7]) if r[7] is not None else None,
            "importance_score": float(r[8]),
            "last_referenced_at": int(r[9]) if r[9] is not None else None,
            "distilled_by": str(r[10]),
            "distilled_at": int(r[11]),
        }
        for r in rows
    ]


async def _blank_hot_columns(store: EpisodesStore, *, episode_id: str) -> None:
    """Replace ``summary_text`` with the sentinel and null out the
    embedding columns.

    Keeps the row addressable by id (the archival path is still
    reversible — iter 9's :func:`rehydrate_episode` reads the cold
    file back). Importance / kind / source-id columns are untouched
    so analytics over the audit trail still work.
    """
    await store.conn.execute(
        """UPDATE episodes
              SET summary_text = ?,
                  embedding = NULL,
                  embedding_dim = NULL
            WHERE id = ?""",
        (ARCHIVED_SENTINEL, episode_id),
    )
    await store.conn.commit()


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via tmp + ``os.replace``.

    Crash-safe: a half-written cold file would otherwise survive a
    process kill mid-archival and the next pass would see a row with
    archived hot columns + a corrupt cold file (data loss). The tmp
    trick reduces the window to "exactly atomic" on POSIX.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Convenience: enumerate cold files (used by iter-9 rehydrate-all)
# ---------------------------------------------------------------------------


def iter_cold_files(*, cold_root: Path) -> Iterable[Path]:
    """Yield ``Path`` objects for every cold-archive file under
    ``cold_root/episodes_cold``.

    Returns empty if the directory doesn't exist — keeps the iter-9
    ``rehydrate-all`` happy on a freshly-deployed tenant.
    """
    cold_dir = cold_root / COLD_DIR_NAME
    if not cold_dir.exists():
        return ()
    return sorted(cold_dir.glob("*.md"))


__all__ = [
    "ARCHIVED_SENTINEL",
    "COLD_DIR_NAME",
    "COLD_EXEMPT_KINDS",
    "ArchiveSummary",
    "archive_unreferenced_episodes",
    "cold_file_path",
    "iter_cold_files",
    "render_cold_file",
]
