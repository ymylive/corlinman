"""Simulator tests — ports of ``rust/.../src/simulator.rs#tests``."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import aiosqlite
import pytest
from corlinman_evolution_store import EvolutionKind, EvolutionRisk

from corlinman_shadow_tester.eval import EvalCase, ExpectedOutcome, ProposalSpec
from corlinman_shadow_tester.simulator import (
    InvalidTargetError,
    MemoryOpSimulator,
    PathRejectedError,
    SkillUpdateSimulator,
    parse_merge_target,
)


# ---------------------------------------------------------------------------
# parse_merge_target
# ---------------------------------------------------------------------------


def test_parse_merge_target_happy_path() -> None:
    assert parse_merge_target("merge_chunks:1,2,3") == [1, 2, 3]


def test_parse_merge_target_rejects_missing_prefix() -> None:
    with pytest.raises(InvalidTargetError):
        parse_merge_target("not_a_merge:1,2")


def test_parse_merge_target_rejects_single_id() -> None:
    with pytest.raises(InvalidTargetError):
        parse_merge_target("merge_chunks:1")


def test_parse_merge_target_rejects_non_integer() -> None:
    with pytest.raises(InvalidTargetError):
        parse_merge_target("merge_chunks:1,abc")


def test_parse_merge_target_rejects_duplicates() -> None:
    with pytest.raises(InvalidTargetError):
        parse_merge_target("merge_chunks:1,1")


# ---------------------------------------------------------------------------
# simulate
# ---------------------------------------------------------------------------


async def _make_kb(tmp_path: Path, seed: list[str]) -> Path:
    path = tmp_path / "kb.sqlite"
    bootstrap = [
        "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, diary_name TEXT, "
        "checksum TEXT, mtime INTEGER, size INTEGER);",
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_id INTEGER, "
        "chunk_index INTEGER, content TEXT, namespace TEXT DEFAULT 'general');",
        "INSERT INTO files VALUES (1, 'fx.md', 'fixture', 'h', 0, 0);",
    ]
    conn = await aiosqlite.connect(path)
    try:
        for stmt in [*bootstrap, *seed]:
            await conn.execute(stmt)
        await conn.commit()
    finally:
        await conn.close()
    return path


def _case(name: str, target: str, expected: ExpectedOutcome) -> EvalCase:
    return EvalCase(
        description="test",
        proposal=ProposalSpec(
            target=target,
            reasoning="test",
            risk=EvolutionRisk.HIGH,
        ),
        expected=expected,
        name=name,
        kind=EvolutionKind.MEMORY_OP,
    )


async def test_simulate_returns_merged_for_existing_chunks(tmp_path: Path) -> None:
    kb = await _make_kb(
        tmp_path,
        [
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) "
            "VALUES (1, 1, 0, 'alpha', 'general');",
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) "
            "VALUES (2, 1, 1, 'beta', 'general');",
        ],
    )
    case = _case(
        "merged",
        "merge_chunks:1,2",
        ExpectedOutcome(
            outcome="merged", rows_merged=1, surviving_chunk_id=1, latency_ms_max=500
        ),
    )
    out = await MemoryOpSimulator().simulate(case, kb)
    assert out.passed, f"expected pass; out={out}"
    assert out.shadow.get("rows_merged") == 1
    assert out.shadow.get("surviving_chunk_id") == 1
    assert out.error is None


async def test_simulate_returns_noop_when_target_missing(tmp_path: Path) -> None:
    kb = await _make_kb(
        tmp_path,
        [
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) "
            "VALUES (1, 1, 0, 'only', 'general');",
        ],
    )
    case = _case(
        "noop",
        "merge_chunks:1,99",
        ExpectedOutcome(outcome="no_op", latency_ms_max=500),
    )
    out = await MemoryOpSimulator().simulate(case, kb)
    assert out.passed, f"expected pass; out={out}"
    assert out.shadow.get("rows_merged") == 0


async def test_simulate_invalid_target_marks_failed(tmp_path: Path) -> None:
    kb = await _make_kb(
        tmp_path,
        [
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) "
            "VALUES (1, 1, 0, 'x', 'general');",
        ],
    )
    case = _case(
        "bad",
        "not_a_merge:1,2",
        ExpectedOutcome(outcome="no_op", latency_ms_max=500),
    )
    out = await MemoryOpSimulator().simulate(case, kb)
    assert not out.passed
    assert out.error is not None
    assert out.baseline == {}
    assert out.shadow == {}


async def test_simulate_records_baseline_and_shadow_keys(tmp_path: Path) -> None:
    kb = await _make_kb(
        tmp_path,
        [
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) "
            "VALUES (1, 1, 0, 'a', 'general');",
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) "
            "VALUES (2, 1, 1, 'b', 'general');",
        ],
    )
    case = _case(
        "keys",
        "merge_chunks:1,2",
        ExpectedOutcome(
            outcome="merged", rows_merged=1, surviving_chunk_id=1, latency_ms_max=500
        ),
    )
    out = await MemoryOpSimulator().simulate(case, kb)
    for k in [
        "chunks_total",
        "target_chunk_ids",
        "target_contents",
        "surviving_id_candidate",
    ]:
        assert k in out.baseline, f"baseline missing {k}"
    for k in [
        "chunks_total",
        "surviving_chunk_id",
        "rows_merged",
        "surviving_content",
    ]:
        assert k in out.shadow, f"shadow missing {k}"


# ---------------------------------------------------------------------------
# Phase 3.1: skill-update sandbox enforcement
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink semantics differ on Windows; the sandbox check is unaffected",
)
async def test_simulate_rejects_symlinked_skills_dir_escape(tmp_path: Path) -> None:
    """SkillUpdateSimulator must reject a ``kb_path`` whose canonicalised
    form lands outside the system temp_dir. Build a tempdir that looks
    valid, then symlink ``<tempdir>/skills`` to a non-temp directory
    before running the simulator. The pre-write re-canonicalise should
    catch the escape and return :class:`PathRejectedError`.
    """
    # ``tmp_path`` itself is under pytest's tmp area — usually under
    # ``$TMPDIR`` on macOS — so we want an ``outside`` path that lives
    # OUTSIDE the system temp_dir. The current working directory of the
    # test process is suitable (workspace root).
    outside = Path.cwd().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if str(outside).startswith(str(temp_root)):
        pytest.skip("workspace dir is under temp_root")

    # We need a real tempdir for kb_path that lives under temp_root.
    # ``pytest``'s ``tmp_path`` on macOS resolves under ``/private/var/...``
    # which IS under temp_root (after symlink resolution).
    with tempfile.TemporaryDirectory() as raw_tmp:
        sandbox = Path(raw_tmp)
        kb_path = sandbox / "kb.sqlite"
        kb_path.write_bytes(b"")
        skills_link = sandbox / "skills"
        os.symlink(outside, skills_link)

        prior_path = outside / "web_search.md"
        pre_existed = prior_path.exists()
        if not pre_existed:
            prior_path.write_bytes(b"prior\n")

        case = EvalCase(
            description="test",
            proposal=ProposalSpec(
                target="skills/web_search.md",
                reasoning="test",
                risk=EvolutionRisk.HIGH,
                diff=(
                    "--- a/skills/web_search.md\n"
                    "+++ b/skills/web_search.md\n"
                    "@@ __APPEND__,0 +__APPEND__,1 @@\n"
                    "+x\n"
                ),
            ),
            expected=ExpectedOutcome(
                outcome="skill_updated",
                file="skills/web_search.md",
                content_includes="x",
                latency_ms_max=500,
            ),
            name="symlink-escape",
            kind=EvolutionKind.SKILL_UPDATE,
        )

        try:
            with pytest.raises(PathRejectedError) as exc_info:
                await SkillUpdateSimulator().simulate(case, kb_path)
            reason = exc_info.value.reason
            assert (
                "temp_root" in reason or "temp_dir" in reason
            ), f"expected sandbox-boundary message, got {reason!r}"
        finally:
            if not pre_existed and prior_path.exists():
                prior_path.unlink()


async def test_simulate_accepts_clean_tempdir_kb_path(tmp_path: Path) -> None:
    """Happy path: a normal tempdir-only kb_path simulator run still
    passes the canonicalize boundary check."""
    # ``pytest``'s ``tmp_path`` on macOS is ``/private/var/folders/...``
    # which is *equivalent* to ``/var/folders/...`` after symlink
    # resolution — both resolve into the system temp_dir. We use it
    # directly here because it stays in temp_root on every platform we
    # support.
    with tempfile.TemporaryDirectory() as raw_tmp:
        sandbox = Path(raw_tmp)
        kb_path = sandbox / "kb.sqlite"
        kb_path.write_bytes(b"")
        skills_dir = sandbox / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "web_search.md").write_bytes(b"prior\n")

        case = EvalCase(
            description="test",
            proposal=ProposalSpec(
                target="skills/web_search.md",
                reasoning="test",
                risk=EvolutionRisk.HIGH,
                diff=(
                    "--- a/skills/web_search.md\n"
                    "+++ b/skills/web_search.md\n"
                    "@@ __APPEND__,0 +__APPEND__,1 @@\n"
                    "+x\n"
                ),
            ),
            expected=ExpectedOutcome(
                outcome="skill_updated",
                file="skills/web_search.md",
                content_includes="x",
                latency_ms_max=500,
            ),
            name="clean",
            kind=EvolutionKind.SKILL_UPDATE,
        )
        out = await SkillUpdateSimulator().simulate(case, kb_path)
        assert out.passed, f"clean tempdir kb_path must pass; out={out}"
