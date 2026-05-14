"""``corlinman-goals`` CLI — operator surface for ``agent_goals.sqlite``.

Six subcommands per design §"Goal-setting CLI surface":

- ``set``   — insert one goal; tier-derived ``target_date`` default,
  cross-tier-parent rejection, slug-based id generation.
- ``list``  — filter by ``--agent-id`` / ``--tier`` / ``--status``;
  ``--include-evaluations`` joins the latest score per row.
- ``edit``  — update one or more mutable columns of one goal id.
- ``archive`` — set ``status='archived'`` (and optionally cascade).
- ``seed``  — load a YAML file of goals, insert what's missing.
- ``reflect-once`` — run one reflection pass for a tier; takes either
  a ``--stub-score`` (constant grader) or relies on a registered LLM
  factory (the iter-7 provider hook). Mirrors
  ``corlinman-episodes`` ``distill-once`` so the gateway boot can
  wire a real provider without an import cycle.

The shape mirrors :mod:`corlinman_episodes.cli` and
:mod:`corlinman_persona.cli` so an operator who learned one CLI knows
the others. ``--json`` flips machine-readable output on every
subcommand. Exit codes:

* 0 — success.
* 1 — runner-level failure (LLM raised, store IO error). Scheduler
  maps to ``EngineRunFailed``.
* 2 — argparse / validation failure (cross-tier parent, missing
  goal id). Scheduler treats as a misconfigured invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from corlinman_goals.evidence import EpisodeEvidence, StaticEvidence
from corlinman_goals.reflection import (
    Grader,
    ReflectionSummary,
    make_constant_grader,
    reflect_once,
)
from corlinman_goals.state import (
    SOURCE_VALUES,
    STATUS_VALUES,
    TIER_VALUES,
    Goal,
)
from corlinman_goals.store import DEFAULT_TENANT_ID, GoalStore
from corlinman_goals.windows import default_target_date_ms, tier_rank

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider factory registry
# ---------------------------------------------------------------------------
#
# Mirrors :mod:`corlinman_episodes.cli`'s factory hooks. Production
# deployments register a real LLM-backed :class:`Grader` factory before
# invoking ``main``; tests don't register anything and pass either
# ``--stub-score N`` or call :func:`run_reflect_once` directly with an
# inline ``Grader`` callable.
#
# The factory takes ``(config, alias)`` where ``config`` is the loaded
# ``[goals]`` block and ``alias`` is the ``reflection_llm_alias`` field.
# Returning a :class:`Grader` (a kwargs-only async callable per
# :mod:`corlinman_goals.reflection`) keeps the CLI ignorant of provider
# import paths — no ``import corlinman_providers`` here.

@dataclass(frozen=True)
class GoalsConfig:
    """``[goals]`` TOML block — surfaces the design's config knobs.

    Matches the design's "Config knobs" section. Defaults are
    intentionally permissive so a fresh boot with no TOML still
    works; the operator overrides only the knobs they care about.
    """

    enabled: bool = True
    reflection_llm_alias: str = "default-cheap"
    short_window_hours: int = 24
    mid_window_days: int = 7
    long_window_days: int = 90
    narrative_max_chars: int = 280
    evidence_max_episodes: int = 8
    no_evidence_sentinel: str = "no_evidence"
    extra: dict[str, Any] = field(default_factory=dict)


_grader_factory: Callable[[GoalsConfig, str], Grader] | None = None
_evidence_factory: (
    Callable[[GoalsConfig, Path, str], EpisodeEvidence] | None
) = None


def register_grader_factory(
    factory: Callable[[GoalsConfig, str], Grader] | None,
) -> None:
    """Plug a real LLM provider into the CLI without import cycles.

    Called by the gateway boot after it builds its
    ``corlinman-providers`` registry. ``None`` clears the hook (test
    teardown). Tests bypass the factory entirely by calling
    :func:`run_reflect_once` directly with an inline grader.
    """
    global _grader_factory
    _grader_factory = factory


def register_evidence_factory(
    factory: Callable[[GoalsConfig, Path, str], EpisodeEvidence] | None,
) -> None:
    """Plug a real D1 :class:`EpisodeEvidence` into the CLI.

    Default factory opens :class:`EpisodesStoreEvidence` against the
    ``--episodes-db`` path. The gateway boot can override to swap in a
    vector-only retrieval surface or a fixture for integration tests.
    """
    global _evidence_factory
    _evidence_factory = factory


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _load_goals_config(path: Path | None) -> GoalsConfig:
    """Parse ``[goals]`` and ``[goals.reflection]`` out of a TOML file.

    Missing file or missing section → built-in defaults. Unknown keys
    inside ``[goals]`` are ignored so a stale operator TOML doesn't
    break a fresh boot.
    """
    if path is None:
        return GoalsConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return GoalsConfig()
    section = data.get("goals", {})
    if not isinstance(section, dict):
        return GoalsConfig()
    reflection_section = section.get("reflection", {})
    if not isinstance(reflection_section, dict):
        reflection_section = {}

    fields = {
        "enabled": bool,
        "reflection_llm_alias": str,
        "short_window_hours": int,
        "mid_window_days": int,
        "long_window_days": int,
    }
    refl_fields = {
        "narrative_max_chars": int,
        "evidence_max_episodes": int,
        "no_evidence_sentinel": str,
    }
    kwargs: dict[str, object] = {}
    for name, conv in fields.items():
        if name in section:
            try:
                kwargs[name] = conv(section[name])
            except (TypeError, ValueError):
                continue
    for name, conv in refl_fields.items():
        if name in reflection_section:
            try:
                kwargs[name] = conv(reflection_section[name])
            except (TypeError, ValueError):
                continue
    return GoalsConfig(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helpers — id minting + parent-tier guard
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, *, max_len: int = 32) -> str:
    """``"Become competent at infra"`` → ``"become-competent-at-infra"``.

    Used for goal-id minting. Lowercases, replaces non-alnum with ``-``,
    collapses repeats, strips leading/trailing dashes, and caps at
    ``max_len`` chars. Empty input falls back to ``"goal"`` so id
    generation is total.
    """
    lowered = text.strip().lower()
    slug = _SLUG_RE.sub("-", lowered).strip("-")
    if not slug:
        return "goal"
    return slug[:max_len].rstrip("-")


def _mint_goal_id(*, body: str, now_ms: int) -> str:
    """``goal-<yyyymmdd>-<slug>`` — the design's id convention.

    Date prefix gives operators a chronological eyeball when tailing
    ``list``; slug is the body-derived suffix so collisions across the
    same day stay rare. Two operators authoring "Test the build" on the
    same day get the same id — the store rejects the second insert,
    forcing an explicit ``--id`` override (operator's responsibility).
    """
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    return f"goal-{dt.strftime('%Y%m%d')}-{_slugify(body)}"


def _parse_target_date(value: str) -> int:
    """Parse ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SSZ`` to unix ms.

    Operators write dates by calendar; the ``target_date`` column is
    unix ms. Anchored to UTC midnight when only a date is supplied so
    the tier-cron windows (``5 0 * * *`` UTC etc.) align cleanly.
    """
    raw = value.strip()
    try:
        if "T" in raw or " " in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"target-date {value!r} not parseable as YYYY-MM-DD or ISO 8601"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


async def _validate_parent(
    store: GoalStore,
    *,
    parent_goal_id: str,
    new_tier: str,
    tenant_id: str,
) -> None:
    """Reject cross-tenant / missing / cross-tier parents.

    Tier rule: a parent must have **strictly higher** tier than the
    child (long > mid > short). A short cannot parent a mid; a mid
    cannot parent a long. Same-tier parents are also rejected — the
    forest is strictly stratified by tier.
    """
    parent = await store.get_goal(parent_goal_id, tenant_id=tenant_id)
    if parent is None:
        raise SystemExit(
            f"corlinman-goals: parent_goal_id={parent_goal_id!r} not found "
            f"in tenant {tenant_id!r}"
        )
    if tier_rank(parent.tier) <= tier_rank(new_tier):
        raise SystemExit(
            f"corlinman-goals: cross_tier_parent: child tier={new_tier!r} "
            f"requires parent of strictly higher tier, but "
            f"parent {parent_goal_id!r} has tier={parent.tier!r}"
        )


# ---------------------------------------------------------------------------
# Library-level entry points (callable from tests + scheduler boot)
# ---------------------------------------------------------------------------


async def run_set_goal(
    *,
    db_path: Path,
    agent_id: str,
    tier: str,
    body: str,
    parent_goal_id: str | None = None,
    target_date_ms: int | None = None,
    source: str = "operator_cli",
    goal_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    now_ms: int | None = None,
) -> Goal:
    """Insert one goal row and return the inserted dataclass.

    Computes the tier-derived ``target_date`` if the caller didn't
    supply one. Validates parent. Mints id from body if not given.
    """
    if tier not in TIER_VALUES:
        raise SystemExit(f"corlinman-goals: tier={tier!r} not in {sorted(TIER_VALUES)}")
    if source not in SOURCE_VALUES:
        raise SystemExit(
            f"corlinman-goals: source={source!r} not in {sorted(SOURCE_VALUES)}"
        )
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    target = (
        target_date_ms
        if target_date_ms is not None
        else default_target_date_ms(tier, now_ms=now)
    )
    gid = goal_id if goal_id is not None else _mint_goal_id(body=body, now_ms=now)
    goal = Goal(
        id=gid,
        agent_id=agent_id,
        tier=tier,
        body=body,
        created_at_ms=now,
        target_date_ms=target,
        parent_goal_id=parent_goal_id,
        status="active",
        source=source,
    )
    async with GoalStore(db_path) as store:
        if parent_goal_id is not None:
            await _validate_parent(
                store,
                parent_goal_id=parent_goal_id,
                new_tier=tier,
                tenant_id=tenant_id,
            )
        await store.insert_goal(goal, tenant_id=tenant_id)
    return goal


async def run_list_goals(
    *,
    db_path: Path,
    agent_id: str | None = None,
    tier: str | None = None,
    status: str | None = None,
    include_evaluations: bool = False,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[dict[str, Any]]:
    """Return goals matching filters as a list of plain dicts.

    When ``include_evaluations`` is set, each row gets a
    ``latest_evaluation`` key (or ``None``). The CLI prints whatever
    this returns; tests assert against the dict shape.
    """
    out: list[dict[str, Any]] = []
    async with GoalStore(db_path) as store:
        rows = await store.list_goals(
            agent_id=agent_id, tier=tier, status=status, tenant_id=tenant_id
        )
        for goal in rows:
            d = asdict(goal)
            if include_evaluations:
                evs = await store.list_evaluations(goal.id, limit=1)
                d["latest_evaluation"] = (
                    asdict(evs[0]) if evs else None
                )
            out.append(d)
    return out


async def run_edit_goal(
    *,
    db_path: Path,
    goal_id: str,
    body: str | None = None,
    target_date_ms: int | None = None,
    parent_goal_id: str | None = ...,  # type: ignore[assignment]
    status: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> bool:
    """Apply one or more mutable updates to a goal.

    ``parent_goal_id=None`` clears the parent; the Ellipsis sentinel
    means "leave unchanged". Returns True iff a row changed.
    """
    async with GoalStore(db_path) as store:
        if parent_goal_id is not ... and parent_goal_id is not None:
            current = await store.get_goal(goal_id, tenant_id=tenant_id)
            if current is None:
                raise SystemExit(
                    f"corlinman-goals: goal_id={goal_id!r} not found"
                )
            await _validate_parent(
                store,
                parent_goal_id=parent_goal_id,
                new_tier=current.tier,
                tenant_id=tenant_id,
            )
        return await store.update_goal(
            goal_id,
            body=body,
            target_date_ms=target_date_ms,
            parent_goal_id=parent_goal_id,
            status=status,
            tenant_id=tenant_id,
        )


async def run_archive_goal(
    *,
    db_path: Path,
    goal_id: str,
    cascade: bool = False,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> int:
    """Archive ``goal_id`` and (optionally) its direct children."""
    async with GoalStore(db_path) as store:
        return await store.archive_goal(
            goal_id, cascade=cascade, tenant_id=tenant_id
        )


async def run_seed_yaml(
    *,
    db_path: Path,
    yaml_path: Path,
    agent_id_override: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    now_ms: int | None = None,
) -> list[Goal]:
    """Insert every goal from ``yaml_path`` that doesn't already exist.

    YAML shape (single top-level ``goals:`` list):

    .. code-block:: yaml

        goals:
          - id: goal-2026-05-09-deepen-infra
            agent_id: mentor
            tier: mid
            body: Become competent at infrastructure topics

    Idempotent — already-present ids are skipped. The ``source`` field
    on every inserted row is forced to ``"seed"`` so the operator can
    still tell where the goal came from.
    """
    with yaml_path.open("rb") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise SystemExit(
            f"corlinman-goals: seed yaml at {yaml_path} not a mapping"
        )
    raw_goals = data.get("goals", [])
    if not isinstance(raw_goals, list):
        raise SystemExit("corlinman-goals: seed yaml's 'goals:' must be a list")

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    inserted: list[Goal] = []
    async with GoalStore(db_path) as store:
        for entry in raw_goals:
            if not isinstance(entry, dict):
                continue
            tier = str(entry.get("tier", "")).strip()
            if tier not in TIER_VALUES:
                raise SystemExit(
                    f"corlinman-goals: seed entry has invalid tier={tier!r}"
                )
            body = str(entry.get("body", "")).strip()
            if not body:
                raise SystemExit("corlinman-goals: seed entry missing 'body'")
            agent_id = (
                agent_id_override
                if agent_id_override is not None
                else str(entry.get("agent_id", "")).strip()
            )
            if not agent_id:
                raise SystemExit(
                    "corlinman-goals: seed entry missing 'agent_id' (and no "
                    "--agent-id override supplied)"
                )
            gid = entry.get("id") or _mint_goal_id(body=body, now_ms=now)
            existing = await store.get_goal(gid, tenant_id=tenant_id)
            if existing is not None:
                continue
            target = entry.get("target_date_ms")
            target_ms = (
                int(target)
                if isinstance(target, int)
                else default_target_date_ms(tier, now_ms=now)
            )
            goal = Goal(
                id=gid,
                agent_id=agent_id,
                tier=tier,
                body=body,
                created_at_ms=now,
                target_date_ms=target_ms,
                parent_goal_id=entry.get("parent_goal_id"),
                status="active",
                source="seed",
            )
            await store.insert_goal(goal, tenant_id=tenant_id)
            inserted.append(goal)
    return inserted


async def run_reflect_once(
    *,
    config: GoalsConfig,
    db_path: Path,
    evidence_source: EpisodeEvidence,
    grader: Grader,
    tier: str,
    agent_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    now_ms: int | None = None,
    evolution_db: Path | None = None,
) -> ReflectionSummary:
    """Library-level reflect-once — preferred call site for tests.

    The CLI ``main()`` resolves the grader + evidence source via the
    factory hooks above and forwards everything else to here.
    ``evolution_db`` (iter 9) is optional — passing it lets the
    runner emit ``goal.weekly_failed`` signals when a mid-tier score
    lands below the underperformance threshold.
    """
    async with GoalStore(db_path) as store:
        return await reflect_once(
            store=store,
            evidence_source=evidence_source,
            grader=grader,
            tier=tier,
            agent_id=agent_id,
            tenant_id=tenant_id,
            now_ms=now_ms,
            evidence_limit=config.evidence_max_episodes,
            evolution_db=evolution_db,
        )


# ---------------------------------------------------------------------------
# Provider resolution helpers (CLI-internal)
# ---------------------------------------------------------------------------


def _resolve_grader(
    config: GoalsConfig,
    *,
    stub_score: int | None,
    stub_narrative: str,
) -> Grader:
    """Pick the grader implementation for one CLI invocation.

    Precedence:

    1. ``--stub-score N`` — always honoured (constant grader). Used by
       smoke runs and tests that drive ``main()``.
    2. Registered :func:`register_grader_factory` factory.
    3. Hard error — refuse to silently no-op.
    """
    if stub_score is not None:
        if not 0 <= stub_score <= 10:
            raise SystemExit(
                f"corlinman-goals: --stub-score {stub_score} not in [0, 10]"
            )
        return make_constant_grader(score=stub_score, narrative=stub_narrative)
    if _grader_factory is not None:
        return _grader_factory(config, config.reflection_llm_alias)
    raise SystemExit(
        "corlinman-goals: no grader registered. Pass --stub-score or "
        "register a factory via register_grader_factory."
    )


def _resolve_evidence(
    config: GoalsConfig,
    *,
    episodes_db: Path | None,
    tenant_id: str,
    stub_evidence: bool,
) -> EpisodeEvidence:
    """Pick the evidence source for one CLI invocation.

    Precedence:

    1. ``--stub-evidence`` — empty :class:`StaticEvidence`. Reflection
       writes the no-evidence sentinel for every goal; smoke / scheduler
       dry-run path.
    2. Registered :func:`register_evidence_factory` factory.
    3. ``--episodes-db <path>`` → open
       :class:`EpisodesStoreEvidence` (default factory).
    """
    if stub_evidence:
        return StaticEvidence([])
    if _evidence_factory is not None:
        if episodes_db is None:
            raise SystemExit(
                "corlinman-goals: --episodes-db required when an evidence "
                "factory is registered."
            )
        return _evidence_factory(config, episodes_db, tenant_id)
    if episodes_db is not None:
        # Lazy import — keeps the CLI's import time tight when the
        # default path isn't taken (tests usually don't touch D1).

        return _LazyEpisodesEvidence(episodes_db, tenant_id)
    raise SystemExit(
        "corlinman-goals: --episodes-db required (or pass --stub-evidence)."
    )


class _LazyEpisodesEvidence:
    """``EpisodesStoreEvidence`` adapter that opens on first ``fetch``.

    The CLI's ``run_reflect_once`` uses one async context for the
    goal store; the evidence source is opened lazily so we only pay
    the SQLite-open cost when the reflection has goals to score.
    Closes the underlying connection when this object is garbage-
    collected via the asyncio loop's cleanup — production scheduler
    runs are short-lived.
    """

    def __init__(self, episodes_db: Path, tenant_id: str) -> None:
        self._db = episodes_db
        self._tenant_id = tenant_id
        self._inner: EpisodeEvidence | None = None

    async def fetch(
        self,
        *,
        agent_id: str,
        window: Any,
        limit: int = 8,
    ) -> list[Any]:
        if self._inner is None:
            from corlinman_goals.evidence import EpisodesStoreEvidence

            self._inner = await EpisodesStoreEvidence.open(
                episodes_db_path=self._db, tenant_id=self._tenant_id
            )
        return await self._inner.fetch(
            agent_id=agent_id, window=window, limit=limit
        )


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Per-tenant agent_goals.sqlite path.",
    )
    parser.add_argument(
        "--tenant-id",
        default=DEFAULT_TENANT_ID,
        help="Tenant scope (default: %(default)s).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON on stdout.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO logging.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corlinman-goals",
        description=(
            "Phase 4 W4 D2 goal hierarchies — short/mid/long agent goals "
            "graded against D1 episodes."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="Insert one goal row.")
    _add_common_args(p_set)
    p_set.add_argument("--agent-id", required=True)
    p_set.add_argument("--tier", required=True, choices=sorted(TIER_VALUES))
    p_set.add_argument("--body", required=True)
    p_set.add_argument("--parent-goal-id", default=None)
    p_set.add_argument(
        "--target-date",
        type=_parse_target_date,
        default=None,
        help="YYYY-MM-DD (UTC). Defaults to tier-derived value.",
    )
    p_set.add_argument(
        "--source",
        default="operator_cli",
        choices=sorted(SOURCE_VALUES),
    )
    p_set.add_argument(
        "--id",
        dest="goal_id",
        default=None,
        help="Override the auto-generated 'goal-<yyyymmdd>-<slug>' id.",
    )
    p_set.add_argument("--now-ms", type=int, default=None, help=argparse.SUPPRESS)

    p_list = sub.add_parser("list", help="List goals (filtered).")
    _add_common_args(p_list)
    p_list.add_argument("--agent-id", default=None)
    p_list.add_argument("--tier", default=None, choices=sorted(TIER_VALUES))
    p_list.add_argument("--status", default=None, choices=sorted(STATUS_VALUES))
    p_list.add_argument(
        "--include-evaluations",
        action="store_true",
        help="Join the latest evaluation per row.",
    )

    p_edit = sub.add_parser("edit", help="Update one or more mutable columns.")
    _add_common_args(p_edit)
    p_edit.add_argument("--goal-id", required=True)
    p_edit.add_argument("--body", default=None)
    p_edit.add_argument(
        "--target-date", type=_parse_target_date, default=None
    )
    p_edit.add_argument("--parent-goal-id", default=None)
    p_edit.add_argument(
        "--clear-parent",
        action="store_true",
        help="Set parent_goal_id to NULL (overrides --parent-goal-id).",
    )
    p_edit.add_argument(
        "--status", default=None, choices=sorted(STATUS_VALUES)
    )

    p_arch = sub.add_parser("archive", help="Archive a goal (and optional children).")
    _add_common_args(p_arch)
    p_arch.add_argument("--goal-id", required=True)
    p_arch.add_argument(
        "--cascade",
        action="store_true",
        help="Also archive direct (one-level) children.",
    )

    p_seed = sub.add_parser(
        "seed", help="Idempotently insert goals from a YAML file."
    )
    _add_common_args(p_seed)
    p_seed.add_argument("--yaml", type=Path, required=True)
    p_seed.add_argument(
        "--agent-id",
        default=None,
        help="Force every seeded row to this agent_id; overrides per-entry.",
    )
    p_seed.add_argument("--now-ms", type=int, default=None, help=argparse.SUPPRESS)

    p_refl = sub.add_parser(
        "reflect-once",
        help="Run one reflection pass for a tier.",
    )
    _add_common_args(p_refl)
    p_refl.add_argument("--tier", required=True, choices=sorted(TIER_VALUES))
    p_refl.add_argument("--agent-id", required=True)
    p_refl.add_argument(
        "--episodes-db",
        type=Path,
        default=None,
        help="Per-tenant episodes.sqlite (D1) for evidence lookup.",
    )
    p_refl.add_argument(
        "--evolution-db",
        type=Path,
        default=None,
        help=(
            "Per-tenant evolution.sqlite — when present and tier=mid, "
            "scores below 5 emit one 'goal.weekly_failed' signal into "
            "evolution_signals. Best-effort; missing DB is a noop."
        ),
    )
    p_refl.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Workspace TOML containing [goals] / [goals.reflection].",
    )
    p_refl.add_argument(
        "--stub-score",
        type=int,
        default=None,
        help="Use a constant grader returning this score [0, 10].",
    )
    p_refl.add_argument(
        "--stub-narrative",
        default="stub",
        help="Narrative paired with --stub-score (default: %(default)s).",
    )
    p_refl.add_argument(
        "--stub-evidence",
        action="store_true",
        help=(
            "Skip D1 lookup; reflection writes the no-evidence sentinel for "
            "every goal. Used by scheduler dry-run."
        ),
    )
    p_refl.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write goal_evaluations rows; print what would happen.",
    )
    p_refl.add_argument(
        "--now-ms", type=int, default=None, help=argparse.SUPPRESS
    )

    return parser


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit(payload: object, *, as_json: bool) -> None:
    """Print one JSON line on ``--json``, or a friendly two-column text
    rendering otherwise. Centralised so every subcommand's output stays
    consistent.

    Lists in JSON mode emit one line per element (ndjson) so a tail-
    /grep-friendly stream lands on stdout — matches the
    ``corlinman-episodes`` CLI's ``--json`` posture and lets ``list
    --json`` pipe straight into ``jq -s 'sort_by(...)'``.
    """
    if as_json:
        if isinstance(payload, list):
            for item in payload:
                print(json.dumps(item, default=str))
            return
        print(json.dumps(payload, default=str))
        return
    if isinstance(payload, list):
        for item in payload:
            print(json.dumps(item, default=str))
        return
    if isinstance(payload, dict):
        for k, v in payload.items():
            print(f"{k}: {v}")
        return
    print(payload)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.command == "set":
            goal = asyncio.run(
                run_set_goal(
                    db_path=args.db,
                    agent_id=args.agent_id,
                    tier=args.tier,
                    body=args.body,
                    parent_goal_id=args.parent_goal_id,
                    target_date_ms=args.target_date,
                    source=args.source,
                    goal_id=args.goal_id,
                    tenant_id=args.tenant_id,
                    now_ms=args.now_ms,
                )
            )
            _emit(asdict(goal), as_json=args.json)
            return 0

        if args.command == "list":
            rows = asyncio.run(
                run_list_goals(
                    db_path=args.db,
                    agent_id=args.agent_id,
                    tier=args.tier,
                    status=args.status,
                    include_evaluations=args.include_evaluations,
                    tenant_id=args.tenant_id,
                )
            )
            _emit(rows, as_json=args.json)
            return 0

        if args.command == "edit":
            parent_kw: object = ...
            if args.clear_parent:
                parent_kw = None
            elif args.parent_goal_id is not None:
                parent_kw = args.parent_goal_id
            changed = asyncio.run(
                run_edit_goal(
                    db_path=args.db,
                    goal_id=args.goal_id,
                    body=args.body,
                    target_date_ms=args.target_date,
                    parent_goal_id=parent_kw,  # type: ignore[arg-type]
                    status=args.status,
                    tenant_id=args.tenant_id,
                )
            )
            _emit({"goal_id": args.goal_id, "changed": changed}, as_json=args.json)
            return 0 if changed else 1

        if args.command == "archive":
            count = asyncio.run(
                run_archive_goal(
                    db_path=args.db,
                    goal_id=args.goal_id,
                    cascade=args.cascade,
                    tenant_id=args.tenant_id,
                )
            )
            _emit(
                {"goal_id": args.goal_id, "archived_count": count},
                as_json=args.json,
            )
            return 0 if count > 0 else 1

        if args.command == "seed":
            inserted = asyncio.run(
                run_seed_yaml(
                    db_path=args.db,
                    yaml_path=args.yaml,
                    agent_id_override=args.agent_id,
                    tenant_id=args.tenant_id,
                    now_ms=args.now_ms,
                )
            )
            _emit(
                {"inserted_count": len(inserted), "ids": [g.id for g in inserted]},
                as_json=args.json,
            )
            return 0

        if args.command == "reflect-once":
            config = _load_goals_config(args.config)
            grader = _resolve_grader(
                config,
                stub_score=args.stub_score,
                stub_narrative=args.stub_narrative,
            )
            evidence = _resolve_evidence(
                config,
                episodes_db=args.episodes_db,
                tenant_id=args.tenant_id,
                stub_evidence=args.stub_evidence,
            )
            if args.dry_run:
                _emit(
                    {
                        "dry_run": True,
                        "tier": args.tier,
                        "agent_id": args.agent_id,
                    },
                    as_json=args.json,
                )
                return 0
            summary = asyncio.run(
                run_reflect_once(
                    config=config,
                    db_path=args.db,
                    evidence_source=evidence,
                    grader=grader,
                    tier=args.tier,
                    agent_id=args.agent_id,
                    tenant_id=args.tenant_id,
                    now_ms=args.now_ms,
                    evolution_db=args.evolution_db,
                )
            )
            _emit(
                {
                    "tier": summary.tier,
                    "reflection_run_id": summary.reflection_run_id,
                    "window": asdict(summary.window),
                    "goals_total": summary.goals_total,
                    "goals_scored": summary.goals_scored,
                    "goals_no_evidence": summary.goals_no_evidence,
                    "goals_skipped_idempotent": summary.goals_skipped_idempotent,
                    "goals_failed": summary.goals_failed,
                    "failed_goal_ids": summary.failed_goal_ids,
                    "signals_emitted": summary.signals_emitted,
                    "signal_goal_ids": summary.signal_goal_ids,
                },
                as_json=args.json,
            )
            return 0
    except SystemExit:
        raise
    except Exception as exc:
        logger.error("corlinman-goals: command failed", exc_info=exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2  # parser.error raises; appease type-checkers.


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "GoalsConfig",
    "main",
    "register_evidence_factory",
    "register_grader_factory",
    "run_archive_goal",
    "run_edit_goal",
    "run_list_goals",
    "run_reflect_once",
    "run_seed_yaml",
    "run_set_goal",
]
