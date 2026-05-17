"""corlinman ShadowTester (Python port).

Python sibling of the Rust crate ``corlinman-shadow-tester``.

Sits between the Python :class:`EvolutionEngine` (writes ``pending``
proposals) and the operator approval queue. For medium/high-risk
proposals it:

1. Loads matching eval cases from ``[evolution.shadow].eval_set_dir``
   (per-kind subdirs under that root).
2. Runs each case in an in-process sandbox against a tempdir copy of
   ``kb.sqlite`` — production state is never written.
3. Captures ``shadow_metrics`` (post-change) + ``baseline_metrics_json``
   (pre-change) and an ``eval_run_id`` for traceability.
4. Transitions the row ``pending -> shadow_running -> shadow_done``, so
   the admin UI can render a measured delta before the operator decides.

Low-risk kinds (Phase 2's ``memory_op`` is the only one shipping in
v0.3) skip ShadowTester entirely and remain on the original
``pending -> approved`` path.

Modules:

- :mod:`corlinman_shadow_tester.eval` — :class:`EvalCase` / :class:`EvalSet`
  types and YAML loader.
- :mod:`corlinman_shadow_tester.simulator` — :class:`KindSimulator`
  protocol + per-kind implementations.
- :mod:`corlinman_shadow_tester.runner` — :class:`ShadowRunner`
  orchestration.
- :mod:`corlinman_shadow_tester.sandbox` — execution sandbox
  (:class:`InProcessBackend`, :class:`DockerBackend`).
"""

from __future__ import annotations

from corlinman_shadow_tester.eval import (
    EmptySet,
    EvalCase,
    EvalLoadError,
    EvalParseError,
    EvalRunResult,
    EvalSet,
    ExpectedOutcome,
    IoError,
    KindMismatch,
    MissingDirError,
    ParseFailure,
    ProposalSpec,
    load_eval_set,
)
from corlinman_shadow_tester.runner import (
    RunSummary,
    ShadowRunner,
    aggregate,
)
from corlinman_shadow_tester.sandbox import (
    DaemonUnavailableError,
    DockerBackend,
    InProcessBackend,
    NonZeroExitError,
    OutputParseError,
    SandboxBackend,
    SandboxError,
    SelfTestResult,
    SpawnError,
    TimeoutError_,
    sha256_hex,
)
from corlinman_shadow_tester.simulator import (
    CONTENT_PREVIEW_CHARS,
    InvalidTargetError,
    KindSimulator,
    MemoryOpSimulator,
    PathRejectedError,
    RuntimeSimulatorError,
    SimulatorError,
    SimulatorOutput,
    SkillUpdateSimulator,
    SqliteSimulatorError,
    TagRebalanceSimulator,
    parse_merge_target,
)


__all__ = [
    "CONTENT_PREVIEW_CHARS",
    "DaemonUnavailableError",
    "DockerBackend",
    "EmptySet",
    "EvalCase",
    "EvalLoadError",
    "EvalParseError",
    "EvalRunResult",
    "EvalSet",
    "ExpectedOutcome",
    "InProcessBackend",
    "InvalidTargetError",
    "IoError",
    "KindMismatch",
    "KindSimulator",
    "MemoryOpSimulator",
    "MissingDirError",
    "NonZeroExitError",
    "OutputParseError",
    "ParseFailure",
    "PathRejectedError",
    "ProposalSpec",
    "RunSummary",
    "RuntimeSimulatorError",
    "SandboxBackend",
    "SandboxError",
    "SelfTestResult",
    "ShadowRunner",
    "SimulatorError",
    "SimulatorOutput",
    "SkillUpdateSimulator",
    "SpawnError",
    "SqliteSimulatorError",
    "TagRebalanceSimulator",
    "TimeoutError_",
    "aggregate",
    "load_eval_set",
    "parse_merge_target",
    "sha256_hex",
]
