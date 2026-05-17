"""``KindSimulator`` protocol + per-kind implementations.

Ported 1:1 from ``rust/crates/corlinman-shadow-tester/src/simulator.rs``.

A simulator takes one :class:`EvalCase` and a path to a tempdir copy of
``kb.sqlite`` that the :class:`ShadowRunner` has already seeded with
``case.kb_seed``. It must:

1. Read pre-state from the tempdir DB -> ``output.baseline``.
2. Apply ``case.proposal.target``'s operation to the tempdir DB only.
3. Read post-state -> ``output.shadow``.
4. Compare against ``case.expected`` -> set ``output.passed``.
5. Return :class:`SimulatorOutput`.

Sandbox invariant: simulators never touch any path other than
``kb_path``. The runner hands them a tempdir; the prod ``kb.sqlite`` is
never opened.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aiosqlite
from corlinman_evolution_store import EvolutionKind

from corlinman_shadow_tester.eval import EvalCase


# Max chars copied from ``chunks.content`` into baseline/shadow metrics.
# Keeps the per-case JSON small enough that the runner can fan-in many
# cases into one proposal row without blowing past sqlite's TEXT
# practicality.
CONTENT_PREVIEW_CHARS = 200


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SimulatorError(RuntimeError):
    """Base class for simulator errors.

    The runner downgrades these into a failed :class:`SimulatorOutput`
    (``passed=False``, ``error=...``) rather than aborting the whole
    shadow run, so one bad case doesn't poison the rest of the eval set.
    """


class InvalidTargetError(SimulatorError):
    """``case.proposal.target`` could not be parsed."""

    def __init__(self, target: str, reason: str) -> None:
        super().__init__(f"invalid target {target!r}: {reason}")
        self.target = target
        self.reason = reason


class SqliteSimulatorError(SimulatorError):
    """Fixture seed or simulated mutation failed against the tempdir DB."""

    def __init__(self, step: str, source: Exception) -> None:
        super().__init__(f"sqlite error in {step}: {source}")
        self.step = step
        self.source = source


class PathRejectedError(SimulatorError):
    """A path the simulator was asked to operate on canonicalised outside
    the tempdir sandbox. Carries the path the runner provided plus a
    short reason."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"path rejected {path!r}: {reason}")
        self.path = path
        self.reason = reason


class RuntimeSimulatorError(SimulatorError):
    """Catch-all for unanticipated runtime conditions."""


# ---------------------------------------------------------------------------
# SimulatorOutput
# ---------------------------------------------------------------------------


@dataclass
class SimulatorOutput:
    """Outcome of running one :class:`EvalCase` through a simulator.

    ``baseline`` and ``shadow`` are free-form ``dict`` blobs so each kind
    decides its own metric vocabulary. The runner aggregates across
    cases without inspecting the keys.
    """

    case_name: str
    passed: bool
    latency_ms: int = 0
    baseline: dict[str, Any] = field(default_factory=dict)
    shadow: dict[str, Any] = field(default_factory=dict)
    # Set when the simulator hit ``SimulatorError``; ``passed`` is false
    # in that case and ``baseline`` / ``shadow`` may be empty.
    error: str | None = None


# ---------------------------------------------------------------------------
# KindSimulator protocol
# ---------------------------------------------------------------------------


class KindSimulator(Protocol):
    """Pluggable per-kind simulator. The runner holds a registry keyed
    by :class:`EvolutionKind` and dispatches at run time."""

    def kind(self) -> EvolutionKind: ...

    async def simulate(self, case: EvalCase, kb_path: Path) -> SimulatorOutput: ...


# ---------------------------------------------------------------------------
# MemoryOpSimulator
# ---------------------------------------------------------------------------


def parse_merge_target(target: str) -> list[int]:
    """Parse a ``merge_chunks:<id>,<id>[,<id>...]`` target into chunk ids.

    Rejects: missing prefix, fewer than 2 ids, non-integer ids, duplicate
    ids.
    """
    prefix = "merge_chunks:"
    if not target.startswith(prefix):
        raise InvalidTargetError(target, "expected prefix 'merge_chunks:'")

    rest = target[len(prefix):]
    ids: list[int] = []
    for raw in rest.split(","):
        trimmed = raw.strip()
        try:
            ids.append(int(trimmed))
        except ValueError as exc:
            raise InvalidTargetError(target, f"non-integer id '{trimmed}'") from exc

    if len(ids) < 2:
        raise InvalidTargetError(target, "merge needs at least 2 chunk ids")

    seen: set[int] = set()
    for a in ids:
        if a in seen:
            raise InvalidTargetError(target, f"duplicate id {a}")
        seen.add(a)

    return ids


def _preview(text: str) -> str:
    """Truncate ``text`` to at most :data:`CONTENT_PREVIEW_CHARS` Unicode
    scalar values. Char-based (not byte-based) so we never split a UTF-8
    codepoint."""
    if len(text) <= CONTENT_PREVIEW_CHARS:
        return text
    return text[:CONTENT_PREVIEW_CHARS]


async def _open_pool(kb_path: Path) -> aiosqlite.Connection:
    """Open the runner-prepared tempdir DB. If the runner forgot to seed
    we want a hard error here, not a silent empty DB that "passes" every
    case — refuse to create the file if it's missing."""
    if not kb_path.exists():
        raise SqliteSimulatorError(
            "open_pool", FileNotFoundError(f"kb_path missing: {kb_path}")
        )
    try:
        # ``uri=True`` so we can pass ``mode=rwc``? Not needed — file
        # already exists. Plain connect is sufficient.
        return await aiosqlite.connect(kb_path)
    except Exception as exc:  # pragma: no cover - aiosqlite raises sqlite3.Error
        raise SqliteSimulatorError("open_pool", exc) from exc


async def _fetch_content(conn: aiosqlite.Connection, id_: int) -> str | None:
    try:
        cursor = await conn.execute(
            "SELECT content FROM chunks WHERE id = ?", (id_,)
        )
    except Exception as exc:
        raise SqliteSimulatorError("fetch_content", exc) from exc
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return None if row[0] is None else str(row[0])


async def _capture_baseline_memory(
    conn: aiosqlite.Connection, parsed_ids: list[int]
) -> dict[str, Any]:
    try:
        cursor = await conn.execute("SELECT COUNT(*) FROM chunks")
    except Exception as exc:
        raise SqliteSimulatorError("baseline.count", exc) from exc
    row = await cursor.fetchone()
    await cursor.close()
    chunks_total = int(row[0]) if row and row[0] is not None else 0

    existing_ids: list[int] = []
    target_contents: dict[str, str] = {}
    for id_ in parsed_ids:
        content = await _fetch_content(conn, id_)
        if content is not None:
            existing_ids.append(id_)
            target_contents[str(id_)] = _preview(content)

    surviving_candidate = min(parsed_ids)

    return {
        "chunks_total": chunks_total,
        "target_chunk_ids": existing_ids,
        "target_contents": target_contents,
        "surviving_id_candidate": surviving_candidate,
    }


def _parsed_existing_ids(baseline: dict[str, Any]) -> list[int]:
    raw = baseline.get("target_chunk_ids")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for v in raw:
        if isinstance(v, int) and not isinstance(v, bool):
            out.append(v)
    return out


async def _apply_merge(
    conn: aiosqlite.Connection,
    surviving_id: int,
    parsed_ids: list[int],
) -> tuple[int, str]:
    to_delete = [i for i in parsed_ids if i != surviving_id]
    placeholders = ",".join("?" for _ in to_delete)
    sql = f"DELETE FROM chunks WHERE id IN ({placeholders})"
    try:
        cursor = await conn.execute(sql, to_delete)
    except Exception as exc:
        raise SqliteSimulatorError("apply_merge.delete", exc) from exc
    rows_merged = cursor.rowcount
    await cursor.close()
    await conn.commit()
    surviving_content = await _fetch_content(conn, surviving_id) or ""
    return int(rows_merged), surviving_content


async def _capture_shadow_memory(
    conn: aiosqlite.Connection,
    surviving_id: int,
    rows_merged: int,
    surviving_content: str,
) -> dict[str, Any]:
    try:
        cursor = await conn.execute("SELECT COUNT(*) FROM chunks")
    except Exception as exc:
        raise SqliteSimulatorError("shadow.count", exc) from exc
    row = await cursor.fetchone()
    await cursor.close()
    chunks_total = int(row[0]) if row and row[0] is not None else 0
    return {
        "chunks_total": chunks_total,
        "surviving_chunk_id": surviving_id,
        "rows_merged": rows_merged,
        "surviving_content": _preview(surviving_content),
    }


class MemoryOpSimulator:
    """Simulator for ``memory_op`` proposals.

    Collapses a set of chunk rows into the lowest-id surviving row by
    deleting the rest, all within the runner's tempdir SQLite. NoOp here
    means "target ids don't all exist", not "content too dissimilar".
    """

    def kind(self) -> EvolutionKind:
        return EvolutionKind.MEMORY_OP

    async def simulate(self, case: EvalCase, kb_path: Path) -> SimulatorOutput:
        started = time.perf_counter()

        try:
            parsed_ids = parse_merge_target(case.proposal.target)
        except InvalidTargetError as exc:
            return SimulatorOutput(
                case_name=case.name,
                passed=False,
                latency_ms=_elapsed_ms(started),
                error=str(exc),
            )

        conn = await _open_pool(kb_path)
        try:
            baseline = await _capture_baseline_memory(conn, parsed_ids)
            existing_ids = _parsed_existing_ids(baseline)
            surviving_id = min(parsed_ids)
            all_present = len(existing_ids) == len(parsed_ids)

            if all_present:
                rows_merged, surviving_content = await _apply_merge(
                    conn, surviving_id, parsed_ids
                )
            else:
                content = await _fetch_content(conn, surviving_id) or ""
                rows_merged = 0
                surviving_content = content

            shadow = await _capture_shadow_memory(
                conn, surviving_id, rows_merged, surviving_content
            )
        finally:
            await conn.close()

        expected = case.expected
        if expected.outcome == "merged":
            passed = (
                expected.rows_merged is not None
                and rows_merged == expected.rows_merged
                and expected.surviving_chunk_id is not None
                and surviving_id == expected.surviving_chunk_id
            )
        elif expected.outcome == "no_op":
            passed = rows_merged == 0
        else:
            # Tag / skill outcomes are checked by their own simulators —
            # a memory_op fixture that uses one of those variants is a
            # mis-categorised case and should fail loudly here rather
            # than silently pass.
            passed = False

        return SimulatorOutput(
            case_name=case.name,
            passed=passed,
            latency_ms=_elapsed_ms(started),
            baseline=baseline,
            shadow=shadow,
        )


# ---------------------------------------------------------------------------
# TagRebalanceSimulator
# ---------------------------------------------------------------------------


class TagRebalanceSimulator:
    """Simulator for ``tag_rebalance`` proposals.

    Re-points ``chunk_tags`` rows from a leaf ``tag_nodes`` row to its
    parent and drops the leaf, mirroring the gateway applier's
    ``apply_tag_rebalance`` SQL inline. NoOp = target path didn't
    resolve to a node, so nothing moved.
    """

    def kind(self) -> EvolutionKind:
        return EvolutionKind.TAG_REBALANCE

    async def simulate(self, case: EvalCase, kb_path: Path) -> SimulatorOutput:
        started = time.perf_counter()

        target = case.proposal.target
        path_prefix = "merge_tag:"
        if not target.startswith(path_prefix) or len(target) <= len(path_prefix):
            return SimulatorOutput(
                case_name=case.name,
                passed=False,
                latency_ms=_elapsed_ms(started),
                error=f"invalid target {target!r}: expected 'merge_tag:<path>'",
            )
        tag_path = target[len(path_prefix):]

        conn = await _open_pool(kb_path)
        try:
            try:
                cursor = await conn.execute("SELECT COUNT(*) FROM tag_nodes")
            except Exception as exc:
                raise SqliteSimulatorError("tag_baseline.count", exc) from exc
            row = await cursor.fetchone()
            await cursor.close()
            tag_nodes_total = int(row[0]) if row and row[0] is not None else 0

            try:
                cursor = await conn.execute(
                    "SELECT id, parent_id FROM tag_nodes WHERE path = ?",
                    (tag_path,),
                )
            except Exception as exc:
                raise SqliteSimulatorError("tag_baseline.lookup", exc) from exc
            target_row = await cursor.fetchone()
            await cursor.close()

            if target_row is None:
                target_id: int | None = None
                parent_id_opt: int | None = None
            else:
                target_id = int(target_row[0])
                parent_id_opt = None if target_row[1] is None else int(target_row[1])

            chunks_under_target = 0
            if target_id is not None:
                try:
                    cursor = await conn.execute(
                        "SELECT COUNT(*) FROM chunk_tags WHERE tag_node_id = ?",
                        (target_id,),
                    )
                except Exception as exc:
                    raise SqliteSimulatorError("tag_baseline.chunks", exc) from exc
                row = await cursor.fetchone()
                await cursor.close()
                chunks_under_target = int(row[0]) if row and row[0] is not None else 0

            baseline = {
                "tag_nodes_total": tag_nodes_total,
                "target_path": tag_path,
                "target_node_id": target_id,
                "parent_id": parent_id_opt,
                "chunk_tags_under_target": chunks_under_target,
            }

            moved_chunk_count = 0
            node_deleted = False
            if target_id is not None and parent_id_opt is not None:
                # Conflict-DELETE before UPDATE — same idempotence guard
                # the gateway applier uses (chunk_tags PK is
                # (chunk_id, tag_node_id)).
                try:
                    cursor = await conn.execute(
                        "DELETE FROM chunk_tags WHERE tag_node_id = ? "
                        "AND chunk_id IN ("
                        "  SELECT chunk_id FROM chunk_tags WHERE tag_node_id = ?"
                        ")",
                        (target_id, parent_id_opt),
                    )
                except Exception as exc:
                    raise SqliteSimulatorError("tag_apply.dedupe", exc) from exc
                await cursor.close()

                try:
                    cursor = await conn.execute(
                        "UPDATE chunk_tags SET tag_node_id = ? WHERE tag_node_id = ?",
                        (parent_id_opt, target_id),
                    )
                except Exception as exc:
                    raise SqliteSimulatorError("tag_apply.reparent", exc) from exc
                moved_chunk_count = int(cursor.rowcount)
                await cursor.close()

                try:
                    cursor = await conn.execute(
                        "DELETE FROM tag_nodes WHERE id = ?", (target_id,)
                    )
                except Exception as exc:
                    raise SqliteSimulatorError("tag_apply.delete", exc) from exc
                node_deleted = int(cursor.rowcount) > 0
                await cursor.close()

                await conn.commit()

            try:
                cursor = await conn.execute("SELECT COUNT(*) FROM tag_nodes")
            except Exception as exc:
                raise SqliteSimulatorError("tag_shadow.count", exc) from exc
            row = await cursor.fetchone()
            await cursor.close()
            post_total = int(row[0]) if row and row[0] is not None else 0

            chunks_under_parent = 0
            if parent_id_opt is not None:
                try:
                    cursor = await conn.execute(
                        "SELECT COUNT(*) FROM chunk_tags WHERE tag_node_id = ?",
                        (parent_id_opt,),
                    )
                except Exception as exc:
                    raise SqliteSimulatorError("tag_shadow.chunks", exc) from exc
                row = await cursor.fetchone()
                await cursor.close()
                chunks_under_parent = int(row[0]) if row and row[0] is not None else 0

            shadow = {
                "tag_nodes_total": post_total,
                "target_node_present": target_id is not None and not node_deleted,
                "moved_chunk_count": moved_chunk_count,
                "chunks_now_under_parent": chunks_under_parent,
            }
        finally:
            await conn.close()

        expected = case.expected
        passed = False
        error: str | None = None
        if expected.outcome == "tag_merged":
            passed = (
                node_deleted
                and expected.src_path == tag_path
                and expected.parent_id is not None
                and parent_id_opt == expected.parent_id
                and expected.moved_chunk_count is not None
                and moved_chunk_count == expected.moved_chunk_count
            )
        elif expected.outcome == "tag_no_op":
            passed = (
                target_id is None and moved_chunk_count == 0 and not node_deleted
            )
        else:
            error = "expected outcome shape mismatch for kind"

        return SimulatorOutput(
            case_name=case.name,
            passed=passed,
            latency_ms=_elapsed_ms(started),
            baseline=baseline,
            shadow=shadow,
            error=error,
        )


# ---------------------------------------------------------------------------
# SkillUpdateSimulator
# ---------------------------------------------------------------------------


def _sha256_short(text: str) -> str:
    """First 16 hex chars of SHA-256 over the bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _parse_append_diff(diff: str) -> list[str]:
    """Parse the ``__APPEND__``-shaped diff the EvolutionEngine emits.

    Mirrors the gateway applier's ``parse_append_diff`` byte-for-byte —
    kept inline because the shadow-tester crate has no gateway dep.

    Raises :class:`ValueError` with a parse-failure reason on rejection.
    """
    lines = diff.splitlines()
    idx = 0
    found_hunk = False
    appended: list[str] = []
    while idx < len(lines):
        line = lines[idx]
        idx += 1
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith("@@"):
            if "__APPEND__" not in line:
                raise ValueError(f"unsupported hunk header: {line}")
            found_hunk = True
            while idx < len(lines):
                body = lines[idx]
                idx += 1
                if body.startswith("+"):
                    appended.append(body[1:])
                elif body == "":
                    continue
                else:
                    raise ValueError(f"non-append body line: {body}")
            break
        if line != "":
            raise ValueError(f"non-header line before hunk: {line}")
    if not found_hunk:
        raise ValueError("no hunk header")
    return appended


class SkillUpdateSimulator:
    """Simulator for ``skill_update`` proposals.

    Replays the ``__APPEND__`` hunk against the runner-prepared per-case
    ``<tempdir>/skills/`` dir. The simulator never touches the production
    ``skills_dir`` — only ``kb_path.parent / 'skills'``, which the
    runner owns.
    """

    def kind(self) -> EvolutionKind:
        return EvolutionKind.SKILL_UPDATE

    async def simulate(self, case: EvalCase, kb_path: Path) -> SimulatorOutput:
        started = time.perf_counter()

        # Phase 3.1 sandbox enforcement.
        #
        # Canonicalise the tempdir-rooted ``kb_path`` and the system
        # temp_dir, then assert containment — TOCTOU dodges between the
        # assert and the write are closed by re-canonicalising the
        # parent right before the write below.
        try:
            kb_path_canonical = kb_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PathRejectedError(kb_path, f"canonicalize kb_path: {exc}") from exc
        try:
            temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PathRejectedError(kb_path, f"canonicalize temp_dir: {exc}") from exc

        if not _is_within(kb_path_canonical, temp_root):
            raise PathRejectedError(
                kb_path,
                f"kb_path canonicalises outside temp_dir {temp_root!r}",
            )

        skills_dir = (
            kb_path_canonical.parent if kb_path_canonical.parent != Path("")
            else Path(".")
        ) / "skills"

        target = case.proposal.target
        skills_prefix = "skills/"
        basename: str | None = None
        if target.startswith(skills_prefix):
            candidate = target[len(skills_prefix):]
            if (
                candidate.endswith(".md")
                and candidate
                and "/" not in candidate
                and ".." not in candidate
            ):
                basename = candidate
        if basename is None:
            return _failed_skill_output(
                case,
                started,
                f"invalid target {target!r}: expected 'skills/<name>.md'",
            )

        path = skills_dir / basename

        prior_content: str | None = None
        try:
            if path.is_file():
                try:
                    prior_content = path.read_text(encoding="utf-8")
                except OSError as exc:
                    return _failed_skill_output(case, started, f"read prior: {exc}")
        except OSError:
            prior_content = None

        baseline_size = len(prior_content.encode("utf-8")) if prior_content is not None else 0
        prior_sha = _sha256_short(prior_content) if prior_content is not None else "absent"

        baseline: dict[str, Any] = {
            "file": target,
            "file_present": prior_content is not None,
            "file_size": baseline_size,
            "byte_count": baseline_size,
            "prior_content_sha": prior_sha,
        }

        if prior_content is None:
            shadow_missing: dict[str, Any] = {
                "file": target,
                "applied": False,
                "file_size": 0,
                "byte_count": 0,
                "appended_bytes": 0,
            }
            passed = case.expected.outcome == "skill_no_op"
            return SimulatorOutput(
                case_name=case.name,
                passed=passed,
                latency_ms=_elapsed_ms(started),
                baseline=baseline,
                shadow=shadow_missing,
                error=None if passed else "skill file missing — expected SkillNoOp",
            )

        try:
            appended_lines = _parse_append_diff(case.proposal.diff)
        except ValueError as exc:
            reason = str(exc)
            shadow_reject: dict[str, Any] = {
                "file": target,
                "applied": False,
                "file_size": baseline_size,
                "byte_count": baseline_size,
                "appended_bytes": 0,
                "reject_reason": reason,
            }
            passed = case.expected.outcome == "skill_no_op"
            return SimulatorOutput(
                case_name=case.name,
                passed=passed,
                latency_ms=_elapsed_ms(started),
                baseline=baseline,
                shadow=shadow_reject,
                error=None if passed else f"diff rejected: {reason}",
            )

        new_content = prior_content
        if new_content and not new_content.endswith("\n"):
            new_content += "\n"
        for line in appended_lines:
            new_content += line + "\n"

        # Re-validate the parent dir canonicalises under ``temp_root``
        # *immediately* before the write. If a racing process swapped
        # ``<tempdir>/skills`` for a symlink between the entry-point
        # check and now, the second canonicalise surfaces it and we
        # reject.
        parent = path.parent
        try:
            parent_canon = parent.resolve(strict=True)
        except (OSError, RuntimeError):
            parent_canon = None
        if parent_canon is not None and not _is_within(parent_canon, temp_root):
            raise PathRejectedError(
                path,
                f"parent dir {parent_canon!r} escaped temp_root {temp_root!r}",
            )

        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return _failed_skill_output(case, started, f"write: {exc}")

        new_size = len(new_content.encode("utf-8"))
        appended_bytes = max(0, new_size - baseline_size)

        shadow: dict[str, Any] = {
            "file": target,
            "applied": True,
            "file_size": new_size,
            "byte_count": new_size,
            "appended_bytes": appended_bytes,
        }

        expected = case.expected
        passed = False
        error: str | None = None
        if expected.outcome == "skill_updated":
            basename_match = (
                expected.file is not None
                and expected.file.startswith(skills_prefix)
                and expected.file[len(skills_prefix):] == basename
            )
            content_includes = expected.content_includes or ""
            passed = basename_match and content_includes in new_content
        elif expected.outcome == "skill_no_op":
            passed = new_size == baseline_size
        else:
            error = "expected outcome shape mismatch for kind"

        return SimulatorOutput(
            case_name=case.name,
            passed=passed,
            latency_ms=_elapsed_ms(started),
            baseline=baseline,
            shadow=shadow,
            error=error,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _failed_skill_output(case: EvalCase, started: float, error: str) -> SimulatorOutput:
    return SimulatorOutput(
        case_name=case.name,
        passed=False,
        latency_ms=_elapsed_ms(started),
        error=error,
    )


def _is_within(path: Path, root: Path) -> bool:
    """Return ``True`` iff ``path`` is equal to or under ``root``.

    Both arguments must already be canonicalised (``resolve(strict=True)``).
    Uses ``os.path.commonpath`` on POSIX-equivalent string forms so we
    avoid the ``Path.is_relative_to`` 3.9+ corner-cases on Windows.
    """
    try:
        common = os.path.commonpath([str(path), str(root)])
    except ValueError:
        return False
    return common == str(root)


__all__ = [
    "CONTENT_PREVIEW_CHARS",
    "InvalidTargetError",
    "KindSimulator",
    "MemoryOpSimulator",
    "PathRejectedError",
    "RuntimeSimulatorError",
    "SimulatorError",
    "SimulatorOutput",
    "SkillUpdateSimulator",
    "SqliteSimulatorError",
    "TagRebalanceSimulator",
    "parse_merge_target",
]
