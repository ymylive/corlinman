"""``ShadowRunner`` — pulls pending medium/high-risk proposals, dispatches
to the per-kind simulator, writes results back.

Ported 1:1 from ``rust/crates/corlinman-shadow-tester/src/runner.rs``.

Per ``run_once`` invocation the runner:

1. For each registered :class:`KindSimulator`, asks the proposals repo
   for ``Pending`` rows of that kind whose risk is in ``shadow_risks``.
2. Atomically claims each row (``Pending -> ShadowRunning``); a losing
   racer skips silently — exactly-one-runner is enforced at the DB.
3. Loads the eval set for the kind, replays per-case ``kb_seed`` SQL
   against a tempdir copy of ``kb.sqlite``, hands the kb path to the
   simulator, captures its :class:`SimulatorOutput`.
4. Aggregates per-case ``baseline`` / ``shadow`` maps into one
   proposal-level baseline + shadow JSON blob and writes everything
   back via ``mark_shadow_done`` (``ShadowRunning -> ShadowDone``).

Failure isolation: a panicking or erroring simulator does not poison
the run — the case is recorded with ``passed=false`` + ``error=...``
and the runner moves on. A simulator-less kind is silently skipped (no
claim) so an operator can register simulators incrementally.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from corlinman_evolution_store import (
    EvolutionKind,
    EvolutionRisk,
    ProposalId,
    ProposalsRepo,
    RepoError,
)

from corlinman_shadow_tester.eval import (
    EmptySet,
    EvalCase,
    EvalLoadError,
    MissingDirError,
    load_eval_set,
)
from corlinman_shadow_tester.simulator import (
    KindSimulator,
    SimulatorOutput,
)


logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """Counts surfaced by :meth:`ShadowRunner.run_once` so the caller can
    log a one-line summary or expose Prometheus counters."""

    proposals_claimed: int = 0
    proposals_completed: int = 0
    proposals_failed: int = 0
    cases_run: int = 0
    errors: int = 0


# ---------------------------------------------------------------------------
# ShadowRunner
# ---------------------------------------------------------------------------


_DEFAULT_SHADOW_RISKS: tuple[EvolutionRisk, ...] = (
    EvolutionRisk.MEDIUM,
    EvolutionRisk.HIGH,
)


@dataclass
class ShadowRunner:
    """Orchestrates ``run_once`` shadow passes against pending proposals."""

    proposals: ProposalsRepo
    # Production kb.sqlite. Copied to a tempdir per case; never opened
    # by the runner directly. Missing path triggers a logged warn + an
    # inline empty-kb bootstrap so tests / fresh installs work.
    kb_path: Path
    eval_set_dir: Path
    simulators: dict[EvolutionKind, KindSimulator] = field(default_factory=dict)
    # Risks ShadowTester gates on. Low-risk proposals skip shadow
    # entirely and remain on the original ``pending -> approved`` path.
    shadow_risks: list[EvolutionRisk] = field(
        default_factory=lambda: list(_DEFAULT_SHADOW_RISKS)
    )
    max_proposals_per_run: int = 10

    def with_shadow_risks(self, risks: list[EvolutionRisk]) -> ShadowRunner:
        """Builder — override the shadow risk filter. Returns ``self``."""
        self.shadow_risks = list(risks)
        return self

    def with_max_proposals_per_run(self, n: int) -> ShadowRunner:
        self.max_proposals_per_run = int(n)
        return self

    def register_simulator(self, sim: KindSimulator) -> None:
        self.simulators[sim.kind()] = sim

    async def run_once(self) -> RunSummary:
        summary = RunSummary()

        # Per-kind so unrelated simulators don't share an eval set.
        for kind, simulator in self.simulators.items():
            try:
                pending = await self.proposals.list_pending_for_shadow(
                    kind,
                    list(self.shadow_risks),
                    int(self.max_proposals_per_run),
                )
            except Exception as exc:
                logger.warning(
                    "shadow: list_pending_for_shadow failed (kind=%s): %s",
                    kind,
                    exc,
                )
                summary.errors += 1
                continue

            for proposal in pending:
                # Claim races: losers see NotFound and skip without
                # touching the row.
                try:
                    await self.proposals.claim_for_shadow(proposal.id)
                except RepoError as exc:
                    logger.info(
                        "shadow: claim_for_shadow lost race or row missing — "
                        "skipping (proposal_id=%s, error=%s)",
                        proposal.id,
                        exc,
                    )
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    logger.info(
                        "shadow: claim_for_shadow raised (proposal_id=%s, error=%s)",
                        proposal.id,
                        exc,
                    )
                    continue
                summary.proposals_claimed += 1

                try:
                    cases = await self._run_proposal(kind, simulator, proposal.id)
                except Exception as exc:
                    logger.warning(
                        "shadow: proposal failed during shadow run "
                        "(proposal_id=%s, error=%s)",
                        proposal.id,
                        exc,
                    )
                    summary.proposals_failed += 1
                    summary.errors += 1
                else:
                    summary.cases_run += cases
                    summary.proposals_completed += 1

        return summary

    async def _run_proposal(
        self,
        kind: EvolutionKind,
        simulator: KindSimulator,
        proposal_id: ProposalId,
    ) -> int:
        """Run one claimed proposal end-to-end. Returns the number of
        cases executed. Any exception raised here means the row stays
        in ``shadow_running`` — the caller should surface that and
        (eventually) reap stuck rows.
        """
        eval_run_id = _make_eval_run_id()

        # Empty / missing eval set: ``no-eval-set`` marker so the operator
        # sees a finished proposal with a clear "untested" label rather
        # than a stuck shadow_running row.
        try:
            set_ = await load_eval_set(self.eval_set_dir, kind)
        except (EmptySet, MissingDirError):
            logger.warning(
                "shadow: no eval set for kind — recording no_eval_set "
                "(proposal_id=%s, kind=%s, eval_set_dir=%s)",
                proposal_id,
                kind,
                self.eval_set_dir,
            )
            empty: dict[str, Any] = {}
            shadow: dict[str, Any] = {
                "eval_run_id": "no-eval-set",
                "kind": kind.as_str(),
                "total_cases": 0,
                "passed_cases": 0,
                "failed_cases": [],
                "pass_rate": 0.0,
                "p50_latency_ms": 0,
                "p95_latency_ms": 0,
                "per_case_shadow": [],
            }
            await self.proposals.mark_shadow_done(
                proposal_id, "no-eval-set", empty, shadow
            )
            return 0
        except EvalLoadError as exc:
            # Surfaces as caller-side warn; row stays shadow_running.
            raise RuntimeError(f"eval load: {exc}") from exc

        outputs: list[SimulatorOutput] = []
        for case in set_.cases:
            outputs.append(await self._run_case(simulator, case))
        cases_run = len(outputs)

        baseline_agg, shadow_agg = aggregate(eval_run_id, kind, outputs)
        await self.proposals.mark_shadow_done(
            proposal_id, eval_run_id, baseline_agg, shadow_agg
        )
        return cases_run

    async def _run_case(
        self, simulator: KindSimulator, case: EvalCase
    ) -> SimulatorOutput:
        """Run one case in its own tempdir. Errors are downgraded to a
        failed :class:`SimulatorOutput` so one bad case doesn't tank
        the set.
        """
        try:
            tmp = tempfile.TemporaryDirectory()
        except OSError as exc:
            return _failed_output(case, f"tempdir: {exc}")
        try:
            tmp_path = Path(tmp.name)
            kb_path = tmp_path / "kb.sqlite"

            try:
                await self._materialize_kb(kb_path)
            except _MaterializeError as exc:
                return _failed_output(case, f"kb materialize: {exc}")

            try:
                await _replay_seed(kb_path, case.kb_seed)
            except Exception as exc:
                return _failed_output(case, f"kb seed: {exc}")

            # skill_update simulator reads ``<tempdir>/skills/<basename>``.
            # Empty map = no-op; safe for memory_op / tag_rebalance.
            if case.skill_seed:
                try:
                    await _seed_skills(tmp_path, case.skill_seed)
                except Exception as exc:
                    return _failed_output(case, f"skill seed: {exc}")

            try:
                return await simulator.simulate(case, kb_path)
            except Exception as exc:
                return _failed_output(case, str(exc))
        finally:
            tmp.cleanup()

    async def _materialize_kb(self, dest: Path) -> None:
        """Either copy the production kb (normal path) or bootstrap an
        empty schema inline (fallback when prod kb is absent — typical
        for tests and fresh installs).
        """
        if self.kb_path.exists():
            try:
                await asyncio.to_thread(shutil.copyfile, self.kb_path, dest)
            except OSError as exc:
                raise _MaterializeError(str(exc)) from exc
            return

        logger.warning(
            "shadow: production kb missing — bootstrapping empty kb (kb_path=%s)",
            self.kb_path,
        )
        try:
            await _bootstrap_empty_kb(dest)
        except Exception as exc:
            raise _MaterializeError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _MaterializeError(RuntimeError):
    """Internal kb materialize failure; never escapes the runner."""


def _failed_output(case: EvalCase, error: str) -> SimulatorOutput:
    return SimulatorOutput(
        case_name=case.name,
        passed=False,
        latency_ms=0,
        baseline={},
        shadow={},
        error=error,
    )


async def _replay_seed(kb_path: Path, seed: list[str]) -> None:
    """Replay ``kb_seed`` SQL against a one-shot connection. We close
    the connection before the simulator opens its own — SQLite + WAL is
    fine with concurrent readers but tests run faster with one-at-a-time
    setup.
    """
    if not seed:
        return
    conn = await aiosqlite.connect(kb_path)
    try:
        for stmt in seed:
            try:
                await conn.execute(stmt)
            except Exception as exc:
                raise RuntimeError(f"seed stmt {stmt!r}: {exc}") from exc
        await conn.commit()
    finally:
        await conn.close()


# Minimal kb schema for the fallback path. Mirrors the columns
# memory_op + tag_rebalance fixtures rely on.
_BOOTSTRAP_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT,
    diary_name TEXT,
    checksum TEXT,
    mtime INTEGER,
    size INTEGER
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER,
    chunk_index INTEGER,
    content TEXT,
    namespace TEXT DEFAULT 'general'
);
CREATE TABLE tag_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES tag_nodes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    depth INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE chunk_tags (
    chunk_id INTEGER NOT NULL,
    tag_node_id INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, tag_node_id)
);
"""


async def _bootstrap_empty_kb(dest: Path) -> None:
    conn = await aiosqlite.connect(dest)
    try:
        await conn.executescript(_BOOTSTRAP_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


async def _seed_skills(tempdir_root: Path, seed: dict[str, str]) -> None:
    """Write each ``<basename> -> body`` entry from ``skill_seed`` into
    ``<tempdir>/skills/<basename>``. The skill_update simulator resolves
    the same dir via ``kb_path.parent / 'skills'``, so the layout must
    match.
    """
    dir_ = tempdir_root / "skills"
    try:
        await asyncio.to_thread(dir_.mkdir, parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"create skills dir: {exc}") from exc
    for basename, body in seed.items():
        # Defence in depth: reject ``..`` / ``/`` so a hostile fixture
        # can't escape the tempdir even though the runner owns it.
        if not basename or "/" in basename or ".." in basename:
            raise RuntimeError(f"invalid skill basename {basename!r}")
        target = dir_ / basename
        try:
            await asyncio.to_thread(target.write_bytes, body.encode("utf-8"))
        except OSError as exc:
            raise RuntimeError(f"write skill {basename}: {exc}") from exc


def _make_eval_run_id() -> str:
    """``eval-YYYY-MM-DD-<short-uuid>`` — date for human grepping, uuid
    for uniqueness across runners on the same day."""
    now = datetime.now(timezone.utc)
    date = f"{now.year:04d}-{now.month:02d}-{now.day:02d}"
    short = uuid.uuid4().hex[:6]
    return f"eval-{date}-{short}"


def aggregate(
    eval_run_id: str,
    kind: EvolutionKind,
    outputs: list[SimulatorOutput],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the proposal-level baseline + shadow JSON blobs.

    Both sides share the same shape so the operator UI can diff them
    directly. ``baseline.passed_cases`` is set to ``total_cases`` because
    the pre-state is "what the kb was before" — pass/fail is meaningless
    pre-mutation, but keeping the field consistent simplifies UI code.
    """
    total = len(outputs)
    passed = sum(1 for o in outputs if o.passed)
    failed_names = [o.case_name for o in outputs if not o.passed]

    latencies = sorted(int(o.latency_ms) for o in outputs)
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)

    pass_rate = 0.0 if total == 0 else float(passed) / float(total)

    per_case_shadow: list[dict[str, Any]] = [
        {
            "name": o.case_name,
            "passed": o.passed,
            "latency_ms": int(o.latency_ms),
            "error": o.error,
            "metrics": dict(o.shadow),
        }
        for o in outputs
    ]
    per_case_baseline: list[dict[str, Any]] = [
        {
            "name": o.case_name,
            "passed": True,
            "latency_ms": 0,
            "error": None,
            "metrics": dict(o.baseline),
        }
        for o in outputs
    ]

    shadow: dict[str, Any] = {
        "eval_run_id": eval_run_id,
        "kind": kind.as_str(),
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": failed_names,
        "pass_rate": pass_rate,
        "p50_latency_ms": p50,
        "p95_latency_ms": p95,
        "per_case_shadow": per_case_shadow,
    }
    baseline: dict[str, Any] = {
        "eval_run_id": eval_run_id,
        "kind": kind.as_str(),
        "total_cases": total,
        "passed_cases": total,
        "failed_cases": [],
        "pass_rate": 0.0 if total == 0 else 1.0,
        "p50_latency_ms": 0,
        "p95_latency_ms": 0,
        "per_case_shadow": per_case_baseline,
    }
    return baseline, shadow


def _percentile(sorted_values: list[int], p: int) -> int:
    """Nearest-rank percentile on a pre-sorted list. Empty -> 0."""
    if not sorted_values:
        return 0
    idx = math.ceil((p / 100.0) * len(sorted_values))
    idx = min(max(idx - 1, 0), len(sorted_values) - 1)
    return int(sorted_values[idx])


__all__ = [
    "RunSummary",
    "ShadowRunner",
    "aggregate",
]
