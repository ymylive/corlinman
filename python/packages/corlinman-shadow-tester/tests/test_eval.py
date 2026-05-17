"""Eval-loader tests — ports of ``rust/.../src/eval.rs#tests``."""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_evolution_store import EvolutionKind

from corlinman_shadow_tester.eval import (
    EmptySet,
    KindMismatch,
    MissingDirError,
    ParseFailure,
    load_eval_set,
)


async def test_load_eval_set_returns_missing_dir_when_path_absent(
    tmp_path: Path,
) -> None:
    with pytest.raises(MissingDirError):
        await load_eval_set(tmp_path, EvolutionKind.MEMORY_OP)


async def test_load_eval_set_parses_real_fixtures(fixtures_root: Path) -> None:
    set_ = await load_eval_set(fixtures_root, EvolutionKind.MEMORY_OP)
    assert set_.kind == EvolutionKind.MEMORY_OP
    assert len(set_.cases) == 4

    names = [c.name for c in set_.cases]
    assert names == [
        "case-001-near-duplicate-merge",
        "case-002-distinct-no-op",
        "case-003-identical-content",
        "case-004-three-way-cluster",
    ]
    assert set_.cases[0].expected.outcome == "merged"


async def test_load_eval_set_rejects_malformed_yaml(tmp_path: Path) -> None:
    kind_dir = tmp_path / "memory_op"
    kind_dir.mkdir(parents=True)
    # Broken indentation inside the mapping; PyYAML surfaces an error.
    (kind_dir / "broken.yaml").write_text(
        "description: bad\nproposal:\n  target: x\n   reasoning: y\n"
    )
    with pytest.raises(ParseFailure):
        await load_eval_set(tmp_path, EvolutionKind.MEMORY_OP)


async def test_load_eval_set_rejects_kind_mismatch(tmp_path: Path) -> None:
    kind_dir = tmp_path / "memory_op"
    kind_dir.mkdir(parents=True)
    (kind_dir / "wrong.yaml").write_text(
        """
kind: skill_update
description: wrong kind
proposal:
  target: irrelevant
  reasoning: irrelevant
expected:
  outcome: no_op
"""
    )
    with pytest.raises(KindMismatch) as exc_info:
        await load_eval_set(tmp_path, EvolutionKind.MEMORY_OP)
    assert exc_info.value.found == "skill_update"


async def test_load_eval_set_rejects_empty_dir(tmp_path: Path) -> None:
    (tmp_path / "memory_op").mkdir(parents=True)
    with pytest.raises(EmptySet):
        await load_eval_set(tmp_path, EvolutionKind.MEMORY_OP)


async def test_load_eval_set_skips_underscore_prefixed(tmp_path: Path) -> None:
    kind_dir = tmp_path / "memory_op"
    kind_dir.mkdir(parents=True)
    valid = """
description: real case
proposal:
  target: merge_chunks:1,2
  reasoning: dupes
expected:
  outcome: no_op
"""
    (kind_dir / "real.yaml").write_text(valid)
    # _draft.yaml is intentionally bogus; loader must skip it.
    (kind_dir / "_draft.yaml").write_text("this is not yaml :::")
    set_ = await load_eval_set(tmp_path, EvolutionKind.MEMORY_OP)
    assert len(set_.cases) == 1
    assert set_.cases[0].name == "real"


async def test_load_eval_set_sorts_cases_by_name(tmp_path: Path) -> None:
    kind_dir = tmp_path / "memory_op"
    kind_dir.mkdir(parents=True)

    def body(name: str) -> str:
        return f"""
name: {name}
description: ordering check
proposal:
  target: t
  reasoning: r
expected:
  outcome: no_op
"""

    # Write out of order on disk; loader must sort by ``name``.
    (kind_dir / "z.yaml").write_text(body("zebra"))
    (kind_dir / "a.yaml").write_text(body("alpha"))
    (kind_dir / "m.yaml").write_text(body("mango"))
    set_ = await load_eval_set(tmp_path, EvolutionKind.MEMORY_OP)
    names = [c.name for c in set_.cases]
    assert names == ["alpha", "mango", "zebra"]
