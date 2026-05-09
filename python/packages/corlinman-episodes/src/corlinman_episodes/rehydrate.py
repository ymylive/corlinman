"""Cold-archive rehydration — Phase 4 W4 D1 iter 9.

Inverse of :mod:`corlinman_episodes.archive`. Per the design doc's
§"Decay / pruning":

- Reads transparently re-hydrate (one-render latency penalty); writes
  never touch cold.
- ``corlinman-episodes rehydrate-all`` CLI flag forces hot promotion
  (pre-migration use).

Iter 9 lands the read-side primitives + the bulk CLI flag. Transparent
hot-promotion at render time on the *Rust* gateway resolver path is a
follow-up — the current resolver returns the sentinel string for
archived rows; an operator running ``rehydrate-all`` (or the future
admin route) restores the hot column so the next render sees the real
summary again.

The cold-file format is the markdown produced by
:func:`corlinman_episodes.archive.render_cold_file`:

    ---
    episode_id: <id>
    tenant_id: default
    kind: conversation
    started_at: <ms>
    ended_at: <ms>
    importance_score: <float>
    distilled_by: <alias>
    distilled_at: <ms>
    last_referenced_at: <ms>          # optional
    embedding_dim: <int>              # optional
    embedding_hex: <hex>              # optional, paired with dim
    ---

    <summary_text body>

The parser is intentionally minimal — split on ``---`` boundaries,
parse each ``key: value`` line; no YAML lib. Keeps the package free
of a hard yaml dep and the file format owner-controlled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from corlinman_episodes.archive import (
    ARCHIVED_SENTINEL,
    cold_file_path,
    iter_cold_files,
)
from corlinman_episodes.config import DEFAULT_TENANT_ID
from corlinman_episodes.store import EpisodesStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ColdFileMalformedError(Exception):
    """Raised when a cold file's front-matter / body shape is unparsable.

    The parser is intentionally strict: a malformed cold file is *not*
    fixed silently. An operator who hand-edits ``episodes_cold/foo.md``
    and breaks the front matter should see the failure on the next
    rehydrate pass — silent rehydration of garbage would corrupt the
    audit trail.
    """

    def __init__(self, *, path: Path, reason: str) -> None:
        super().__init__(f"malformed cold file {path}: {reason}")
        self.path = path
        self.reason = reason


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColdEpisode:
    """Parsed contents of one ``episodes_cold/<id>.md`` file.

    Mirrors the columns the archive sweep blanked, plus the metadata
    needed to confirm the row id round-trips cleanly. Embedding bytes
    are reconstructed from the hex string on parse.
    """

    episode_id: str
    tenant_id: str
    kind: str
    started_at: int
    ended_at: int
    importance_score: float
    distilled_by: str
    distilled_at: int
    summary_text: str
    last_referenced_at: int | None = None
    embedding: bytes | None = None
    embedding_dim: int | None = None


@dataclass(frozen=True)
class RehydrateSummary:
    """Outcome of a :func:`rehydrate_all` pass — same shape family as
    :class:`corlinman_episodes.archive.ArchiveSummary` and
    :class:`corlinman_episodes.runner.RunSummary`."""

    tenant_id: str
    rehydrated: int = 0
    skipped_already_hot: int = 0
    skipped_no_row: int = 0
    failed: int = 0
    rehydrated_episode_ids: tuple[str, ...] = field(default_factory=tuple)
    failed_episode_ids: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Cold-file parser
# ---------------------------------------------------------------------------


def parse_cold_file(text: str, *, source_path: Path | None = None) -> ColdEpisode:
    """Parse a cold-archive file payload into a :class:`ColdEpisode`.

    Format expected:
        ``---\\n`` ``key: value\\n`` ``…`` ``\\n---\\n\\n`` ``<body>``.

    Raises :class:`ColdFileMalformedError` on:
        - Missing front-matter delimiters.
        - Required keys absent (id/tenant/kind/started/ended/importance/
          distilled_by/distilled_at).
        - Embedding hex present without a matching dim (or vice versa).

    Optional keys (``last_referenced_at``, ``embedding_*``) default
    to None when absent — matching the archive-side serialisation.
    """
    if not text.startswith("---"):
        raise ColdFileMalformedError(
            path=source_path or Path("<text>"),
            reason="missing leading '---' delimiter",
        )
    # Body begins after the second `---\n`. Use partition twice so the
    # body retains internal `---` lines (a summary that contains a horizontal
    # rule must round-trip).
    _lead, _, after_lead = text.partition("---\n")
    front_block, sep, body_block = after_lead.partition("\n---")
    if sep == "":
        raise ColdFileMalformedError(
            path=source_path or Path("<text>"),
            reason="missing closing '---' delimiter",
        )
    front: dict[str, str] = {}
    for raw_line in front_block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, _, value = line.partition(":")
        if not key:
            continue
        front[key.strip()] = value.strip()

    required = (
        "episode_id",
        "tenant_id",
        "kind",
        "started_at",
        "ended_at",
        "importance_score",
        "distilled_by",
        "distilled_at",
    )
    missing = [k for k in required if k not in front]
    if missing:
        raise ColdFileMalformedError(
            path=source_path or Path("<text>"),
            reason=f"missing required key(s): {','.join(missing)}",
        )

    # Body strips the leading `\n` after `---`, then any single
    # blank-line separator, but preserves interior whitespace + trailing
    # newline behaviour we wrote.
    body = body_block.lstrip("\n").rstrip("\n")

    embedding: bytes | None = None
    embedding_dim: int | None = None
    has_hex = "embedding_hex" in front
    has_dim = "embedding_dim" in front
    if has_hex != has_dim:
        raise ColdFileMalformedError(
            path=source_path or Path("<text>"),
            reason="embedding_hex/embedding_dim must appear together",
        )
    if has_hex and has_dim:
        try:
            embedding_dim = int(front["embedding_dim"])
            embedding = bytes.fromhex(front["embedding_hex"])
        except ValueError as exc:
            raise ColdFileMalformedError(
                path=source_path or Path("<text>"),
                reason=f"embedding hex/dim parse: {exc}",
            ) from exc
        # Sanity: BLOB length must match dim*4 (f32). Same invariant
        # the writer enforces; pinning here means a hand-edited file
        # that lies about either field gets caught at parse time.
        if len(embedding) != embedding_dim * 4:
            raise ColdFileMalformedError(
                path=source_path or Path("<text>"),
                reason=(
                    f"embedding bytes {len(embedding)} != "
                    f"embedding_dim*4 = {embedding_dim * 4}"
                ),
            )

    last_ref = (
        int(front["last_referenced_at"])
        if "last_referenced_at" in front
        else None
    )

    try:
        return ColdEpisode(
            episode_id=front["episode_id"],
            tenant_id=front["tenant_id"],
            kind=front["kind"],
            started_at=int(front["started_at"]),
            ended_at=int(front["ended_at"]),
            importance_score=float(front["importance_score"]),
            distilled_by=front["distilled_by"],
            distilled_at=int(front["distilled_at"]),
            summary_text=body,
            last_referenced_at=last_ref,
            embedding=embedding,
            embedding_dim=embedding_dim,
        )
    except (TypeError, ValueError) as exc:
        raise ColdFileMalformedError(
            path=source_path or Path("<text>"),
            reason=f"front-matter type coercion: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Single-episode rehydration
# ---------------------------------------------------------------------------


async def rehydrate_episode(
    *,
    store: EpisodesStore,
    cold_root: Path,
    episode_id: str,
) -> bool:
    """Read ``<cold_root>/episodes_cold/<id>.md`` and restore the hot
    columns on the matching ``episodes`` row.

    Returns:
        ``True`` if the row was rehydrated.
        ``False`` if the row is already hot (sentinel text absent) or
        the cold file is missing — both are non-fatal "nothing to do"
        cases for the caller.

    Raises:
        :class:`ColdFileMalformedError` on a corrupt cold file.

    Behaviour pinned by tests:
        - Hot columns restored: ``summary_text``, ``embedding``,
          ``embedding_dim`` (when the cold file carried them).
        - ``last_referenced_at`` is *bumped to now* by the resolver on
          the next render hit; we don't reset it here. Operator-forced
          rehydration is a maintenance event, not a usage event.
        - Cold file is **not** deleted — re-archival on the next sweep
          would just regenerate it; keeping the file means a follow-up
          mistake (`rehydrate-all` then immediate `archive`) doesn't
          double-write the same content.
    """
    path = cold_file_path(root=cold_root, episode_id=episode_id)
    if not path.exists():
        logger.debug("rehydrate: cold file missing", extra={"episode_id": episode_id})
        return False

    text = path.read_text(encoding="utf-8")
    cold = parse_cold_file(text, source_path=path)

    # Look up the existing hot row — we restore in place rather than
    # re-inserting so the row's id stays stable for the placeholder
    # resolver and the source-id columns the cold file doesn't carry
    # (source_session_keys, source_signal_ids, source_history_ids)
    # remain untouched.
    cur = await store.conn.execute(
        "SELECT summary_text FROM episodes WHERE id = ? AND tenant_id = ?",
        (cold.episode_id, cold.tenant_id),
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        logger.warning(
            "rehydrate: no hot row for cold file",
            extra={"episode_id": cold.episode_id, "tenant_id": cold.tenant_id},
        )
        return False

    if row[0] != ARCHIVED_SENTINEL:
        # Row already carries real summary text — nothing to do.
        return False

    await store.conn.execute(
        """UPDATE episodes
              SET summary_text = ?,
                  embedding = ?,
                  embedding_dim = ?
            WHERE id = ? AND tenant_id = ?""",
        (
            cold.summary_text,
            cold.embedding,
            cold.embedding_dim,
            cold.episode_id,
            cold.tenant_id,
        ),
    )
    await store.conn.commit()
    return True


# ---------------------------------------------------------------------------
# Bulk rehydration (CLI surface)
# ---------------------------------------------------------------------------


async def rehydrate_all(
    *,
    store: EpisodesStore,
    cold_root: Path,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> RehydrateSummary:
    """Walk every cold file under ``cold_root/episodes_cold`` and attempt
    rehydration. Tenant-filtered via the cold-file front matter.

    Used by the ``corlinman-episodes rehydrate-all`` CLI subcommand.
    Tolerant of partial corruption: a single malformed file gets
    counted under :attr:`RehydrateSummary.failed` and the sweep
    continues; an operator inspecting the summary picks the bad ids
    out of ``failed_episode_ids`` and fixes them by hand.

    Idempotent: calling ``rehydrate_all`` twice just bumps the
    "skipped_already_hot" counter on the second pass.
    """
    rehydrated_ids: list[str] = []
    failed_ids: list[str] = []
    skipped_already_hot = 0
    skipped_no_row = 0

    for path in iter_cold_files(cold_root=cold_root):
        try:
            cold = parse_cold_file(path.read_text(encoding="utf-8"), source_path=path)
        except ColdFileMalformedError as exc:
            logger.warning(
                "rehydrate: malformed cold file",
                extra={"path": str(path), "reason": exc.reason},
            )
            # We can't read the id off a malformed front matter, so
            # fall back to the filename stem (which the writer also
            # used). Failure-tracking is best-effort.
            failed_ids.append(path.stem)
            continue
        if cold.tenant_id != tenant_id:
            continue
        try:
            ok = await rehydrate_episode(
                store=store,
                cold_root=cold_root,
                episode_id=cold.episode_id,
            )
        except Exception:  # pragma: no cover  - defensive
            logger.exception(
                "rehydrate: rehydrate_episode raised",
                extra={"episode_id": cold.episode_id},
            )
            failed_ids.append(cold.episode_id)
            continue
        if ok:
            rehydrated_ids.append(cold.episode_id)
        else:
            # Either the row was already hot, or there's no matching
            # row at all — the caller can't tell from the bool, but
            # the side-effect-free outcome is the same. We disambiguate
            # by re-querying the row count for observability.
            cur = await store.conn.execute(
                "SELECT 1 FROM episodes WHERE id = ? AND tenant_id = ?",
                (cold.episode_id, cold.tenant_id),
            )
            exists = await cur.fetchone()
            await cur.close()
            if exists is None:
                skipped_no_row += 1
            else:
                skipped_already_hot += 1

    return RehydrateSummary(
        tenant_id=tenant_id,
        rehydrated=len(rehydrated_ids),
        skipped_already_hot=skipped_already_hot,
        skipped_no_row=skipped_no_row,
        failed=len(failed_ids),
        rehydrated_episode_ids=tuple(rehydrated_ids),
        failed_episode_ids=tuple(failed_ids),
    )


__all__ = [
    "ColdEpisode",
    "ColdFileMalformedError",
    "RehydrateSummary",
    "parse_cold_file",
    "rehydrate_all",
    "rehydrate_episode",
]
