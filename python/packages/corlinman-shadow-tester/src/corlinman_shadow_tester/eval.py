"""Eval-case spec + YAML loader for ShadowTester.

Ported 1:1 from ``rust/crates/corlinman-shadow-tester/src/eval.rs``.

Cases live as YAML files under ``<eval_set_dir>/<kind>/*.yaml``. The
per-kind subdir is the contract: it lets :func:`load_eval_set` default
``kind`` from the path so authors don't have to repeat themselves, and
makes ``ls memory_op/`` the way an operator audits coverage.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from corlinman_evolution_store import EvolutionKind, EvolutionRisk


DEFAULT_LATENCY_MS_MAX = 500


@dataclass
class ProposalSpec:
    """Minimum proposal data the simulator needs.

    The runner assembles a full :class:`EvolutionProposal` by attaching a
    generated id + timestamps when it dispatches to a simulator.
    """

    target: str
    reasoning: str
    # Shadow only fires for medium/high; default High keeps fixtures
    # in-scope by default.
    risk: EvolutionRisk = EvolutionRisk.HIGH
    signal_ids: list[int] = field(default_factory=list)
    # Unified-diff payload — ``skill_update`` ships an ``__APPEND__`` hunk
    # here. memory_op / tag_rebalance leave it empty.
    diff: str = ""


@dataclass
class ExpectedOutcome:
    """What the simulator should observe after replaying the proposal.

    ``outcome`` is the discriminator (snake_case string). The other
    fields are populated per-variant; absent fields default to ``None``
    (the variant inspection layer is the simulator).

    Variants (matching the Rust ``ExpectedOutcome`` enum tag values):

    - ``merged`` — memory_op merge that consumed ``rows_merged`` chunks
      and kept ``surviving_chunk_id`` as the canonical row.
    - ``no_op`` — memory_op detected a bogus / unsafe target.
    - ``tag_merged`` — tag_rebalance executed; ``src_path`` gone and
      ``parent_id`` now owns its ``chunk_tags`` rows.
    - ``tag_no_op`` — tag_rebalance target path didn't resolve.
    - ``skill_updated`` — skill_update file appended; final body must
      contain ``content_includes`` as a substring.
    - ``skill_no_op`` — skill_update rejected (unsupported diff shape,
      missing file, invalid target).
    """

    outcome: str
    rows_merged: int | None = None
    surviving_chunk_id: int | None = None
    src_path: str | None = None
    parent_id: int | None = None
    moved_chunk_count: int | None = None
    file: str | None = None
    content_includes: str | None = None
    latency_ms_max: int = DEFAULT_LATENCY_MS_MAX

    _ALLOWED: frozenset[str] = frozenset(
        {
            "merged",
            "no_op",
            "tag_merged",
            "tag_no_op",
            "skill_updated",
            "skill_no_op",
        }
    )

    def __post_init__(self) -> None:
        if self.outcome not in self._ALLOWED:
            raise EvalParseError(f"unknown expected outcome: {self.outcome!r}")


@dataclass
class EvalCase:
    """One YAML-defined test case for one kind.

    ``kb_seed`` runs raw SQL against a tempdir copy of ``kb.sqlite``
    before the proposal is shadowed. Normally ``INSERT INTO chunks ...``;
    cases that want a from-scratch fixture can include ``CREATE TABLE``
    first.
    """

    description: str
    proposal: ProposalSpec
    expected: ExpectedOutcome
    # Defaults to the YAML file stem in :func:`load_eval_set`.
    name: str = ""
    # Defaults to the directory's kind in :func:`load_eval_set`.
    kind: EvolutionKind | None = None
    kb_seed: list[str] = field(default_factory=list)
    # ``<basename> -> file body`` map written into a runner-managed
    # per-case ``<tempdir>/skills/`` directory before the simulator runs.
    # Empty for kinds that don't touch ``skills/``.
    skill_seed: dict[str, str] = field(default_factory=dict)


@dataclass
class EvalSet:
    """All cases loaded from one ``<dir>/<kind>/`` subdir."""

    kind: EvolutionKind
    cases: list[EvalCase]
    loaded_from: Path


@dataclass
class EvalRunResult:
    """Per-case run result; metrics shape is simulator-defined."""

    case_name: str
    passed: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# Errors. Empty-set is intentionally an error: a misconfigured path that
# silently shadows zero cases would look "green" forever.
# ---------------------------------------------------------------------------


class EvalLoadError(RuntimeError):
    """Base class for every loader error."""


class MissingDirError(EvalLoadError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"eval-set dir missing: {path}")
        self.path = path


class IoError(EvalLoadError):
    def __init__(self, path: Path, source: Exception) -> None:
        super().__init__(f"io error reading {path}: {source}")
        self.path = path
        self.source = source


class ParseFailure(EvalLoadError):
    def __init__(self, file: Path, reason: str) -> None:
        super().__init__(f"parse failure in {file}: {reason}")
        self.file = file
        self.reason = reason


class KindMismatch(EvalLoadError):
    def __init__(self, file: Path, expected: EvolutionKind, found: str) -> None:
        super().__init__(
            f"kind mismatch in {file}: expected {expected!r}, found '{found}'"
        )
        self.file = file
        self.expected = expected
        self.found = found


class EmptySet(EvalLoadError):
    def __init__(self, dir_: Path, kind: EvolutionKind) -> None:
        super().__init__(f"no eval cases found for kind {kind!r} under {dir_}")
        self.dir = dir_
        self.kind = kind


class EvalParseError(EvalLoadError):
    """Raised when a YAML doc cannot be coerced into an :class:`EvalCase`."""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _parse_case(raw: Any) -> EvalCase:
    """Coerce a parsed YAML document into :class:`EvalCase`.

    Mirrors the Rust ``serde(default)`` shape — missing fields default to
    sensible empties. Throws :class:`EvalParseError` on shape mismatch.
    """
    if not isinstance(raw, dict):
        raise EvalParseError(f"top-level YAML must be a mapping, got {type(raw).__name__}")

    description = raw.get("description")
    if not isinstance(description, str):
        raise EvalParseError("missing required str 'description'")

    proposal_raw = raw.get("proposal")
    if not isinstance(proposal_raw, dict):
        raise EvalParseError("missing required mapping 'proposal'")
    target = proposal_raw.get("target")
    reasoning = proposal_raw.get("reasoning")
    if not isinstance(target, str):
        raise EvalParseError("proposal.target must be a string")
    if not isinstance(reasoning, str):
        raise EvalParseError("proposal.reasoning must be a string")
    risk_raw = proposal_raw.get("risk", "high")
    risk = EvolutionRisk.from_str(str(risk_raw))
    signal_ids_raw = proposal_raw.get("signal_ids", [])
    if not isinstance(signal_ids_raw, list):
        raise EvalParseError("proposal.signal_ids must be a list")
    signal_ids = [int(x) for x in signal_ids_raw]
    diff = proposal_raw.get("diff", "")
    if not isinstance(diff, str):
        raise EvalParseError("proposal.diff must be a string")

    expected_raw = raw.get("expected")
    if not isinstance(expected_raw, dict):
        raise EvalParseError("missing required mapping 'expected'")
    outcome = expected_raw.get("outcome")
    if not isinstance(outcome, str):
        raise EvalParseError("expected.outcome must be a string")
    expected = ExpectedOutcome(
        outcome=outcome,
        rows_merged=expected_raw.get("rows_merged"),
        surviving_chunk_id=expected_raw.get("surviving_chunk_id"),
        src_path=expected_raw.get("src_path"),
        parent_id=expected_raw.get("parent_id"),
        moved_chunk_count=expected_raw.get("moved_chunk_count"),
        file=expected_raw.get("file"),
        content_includes=expected_raw.get("content_includes"),
        latency_ms_max=int(expected_raw.get("latency_ms_max", DEFAULT_LATENCY_MS_MAX)),
    )

    name = raw.get("name", "") or ""
    kind_raw = raw.get("kind")
    kind: EvolutionKind | None = None
    if kind_raw is not None:
        kind = EvolutionKind.from_str(str(kind_raw))

    kb_seed_raw = raw.get("kb_seed", [])
    if not isinstance(kb_seed_raw, list):
        raise EvalParseError("kb_seed must be a list of strings")
    kb_seed = [str(x) for x in kb_seed_raw]

    skill_seed_raw = raw.get("skill_seed", {}) or {}
    if not isinstance(skill_seed_raw, dict):
        raise EvalParseError("skill_seed must be a mapping")
    skill_seed = {str(k): str(v) for k, v in skill_seed_raw.items()}

    return EvalCase(
        description=description,
        proposal=ProposalSpec(
            target=target,
            reasoning=reasoning,
            risk=risk,
            signal_ids=signal_ids,
            diff=diff,
        ),
        expected=expected,
        name=str(name),
        kind=kind,
        kb_seed=kb_seed,
        skill_seed=skill_seed,
    )


async def load_eval_set(eval_set_dir: Path, kind: EvolutionKind) -> EvalSet:
    """Load every ``*.yaml``/``*.yml`` file from ``<eval_set_dir>/<kind>/``.

    Files prefixed with ``_`` are skipped (drafts). Non-recursive. Cases
    are sorted by ``name`` for deterministic ordering across runs.

    The Rust implementation is fully async via ``tokio::fs``; we delegate
    filesystem and YAML parsing to a thread (via ``asyncio.to_thread``)
    so the coroutine yields and matches the async surface.
    """
    return await asyncio.to_thread(_load_eval_set_sync, eval_set_dir, kind)


def _load_eval_set_sync(eval_set_dir: Path, kind: EvolutionKind) -> EvalSet:
    dir_ = eval_set_dir / kind.as_str()
    try:
        exists = dir_.exists()
    except OSError as exc:
        raise IoError(dir_, exc) from exc
    if not exists:
        raise MissingDirError(dir_)

    yaml_files: list[Path] = []
    try:
        for entry in dir_.iterdir():
            name = entry.name
            if name.startswith("_"):
                continue
            suffix = entry.suffix.lower()
            if suffix not in (".yaml", ".yml"):
                continue
            if not entry.is_file():
                continue
            yaml_files.append(entry)
    except OSError as exc:
        raise IoError(dir_, exc) from exc

    cases: list[EvalCase] = []
    for file in yaml_files:
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise IoError(file, exc) from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ParseFailure(file, str(exc)) from exc
        try:
            case = _parse_case(raw)
        except EvalParseError as exc:
            raise ParseFailure(file, str(exc)) from exc

        # Reject explicit-but-wrong kinds; default the unset case to the dir's kind.
        if case.kind is not None and case.kind != kind:
            raise KindMismatch(file, kind, case.kind.as_str())
        case.kind = kind

        if not case.name:
            case.name = file.stem or "unnamed"
        cases.append(case)

    if not cases:
        raise EmptySet(dir_, kind)

    cases.sort(key=lambda c: c.name)
    return EvalSet(kind=kind, cases=cases, loaded_from=dir_)


__all__ = [
    "DEFAULT_LATENCY_MS_MAX",
    "EmptySet",
    "EvalCase",
    "EvalLoadError",
    "EvalParseError",
    "EvalRunResult",
    "EvalSet",
    "ExpectedOutcome",
    "IoError",
    "KindMismatch",
    "MissingDirError",
    "ParseFailure",
    "ProposalSpec",
    "load_eval_set",
]
