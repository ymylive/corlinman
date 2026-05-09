"""Iter 6 tests — :mod:`corlinman_episodes.cli` entry point.

The CLI is the production-side trigger for episodic distillation:
the ``corlinman-scheduler`` Rust crate dispatches a
``Subprocess`` job that invokes ``corlinman-episodes distill-once`` on
the cron specified by ``[episodes] schedule``. These tests exercise
the surface from two angles:

- The ``argparse`` shape — required flags, ``--window-hours``
  override, ``--stub-summary`` short-circuit, ``--json`` output.
- The provider-factory hook — :func:`register_summary_provider_factory`
  + :func:`register_embedding_provider_factory` so the gateway boot can
  inject the real ``corlinman-providers`` adapter without import
  cycles.

Tests run the CLI via :func:`run_cli(argv=[...])` so we exercise the
exact path the scheduler would. No real network, no real LLM —
``--stub-summary`` is the iter-6 contract: tests + scheduler smoke
runs work without a registered factory.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from corlinman_episodes import cli
from corlinman_episodes.config import EpisodesConfig
from corlinman_episodes.distiller import make_constant_provider
from corlinman_episodes.store import EpisodesStore

from tests._seed import insert_session_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cli(argv: list[str]) -> int:
    """Invoke :func:`cli.main` from outside the running test event loop.

    The test config uses ``asyncio_mode = "auto"`` so every test
    function — even the synchronously-defined ones — runs under a
    pytest-asyncio event loop. The CLI's ``asyncio.run`` then refuses
    to start a *second* loop with "asyncio.run() cannot be called
    from a running event loop". We side-step it by running the CLI
    on a fresh thread (with its own loop).

    Exceptions raised inside ``main`` are re-raised on the calling
    thread so ``pytest.raises`` still works.
    """
    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["rc"] = cli.main(argv)
        except BaseException as exc:  # SystemExit lives here
            result["exc"] = exc

    t = threading.Thread(target=_runner)
    t.start()
    t.join()
    if "exc" in result:
        raise result["exc"]
    return int(result["rc"])


# ---------------------------------------------------------------------------
# Factory-registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_factories() -> None:
    """Tests must not leak factory registrations into each other.

    The CLI module-level ``_summary_factory`` / ``_embedding_factory``
    are mutable singletons; clear them around each test so a
    registration in one case can't show up as a leak in another.
    """
    cli.register_summary_provider_factory(None)
    cli.register_embedding_provider_factory(None)
    yield
    cli.register_summary_provider_factory(None)
    cli.register_embedding_provider_factory(None)


# ---------------------------------------------------------------------------
# Fixtures: source-stream DBs the runner reads
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_args(
    tmp_path: Path,
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
    identity_db: Path,
) -> dict[str, str]:
    """Common ``argv`` fragments for the distill-once subcommand."""
    episodes_db = tmp_path / "episodes.sqlite"
    return {
        "episodes_db": str(episodes_db),
        "sessions_db": str(sessions_db),
        "evolution_db": str(evolution_db),
        "hook_events_db": str(hook_events_db),
        "identity_db": str(identity_db),
    }


def _seed_chat(sessions_db: Path, *, base_ms: int = 1_000_000) -> None:
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=0,
        role="user",
        content="hello",
        ts_ms=base_ms,
    )
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=1,
        role="agent",
        content="hi back",
        ts_ms=base_ms + 1_000,
    )


# ---------------------------------------------------------------------------
# distill-once
# ---------------------------------------------------------------------------


def test_distill_once_writes_one_episode(
    cli_args: dict[str, str],
    sessions_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``distill-once --stub-summary`` produces an OK run + one row.

    Pins the load-bearing iter-6 contract: the scheduler subprocess
    exit code is 0 and the episode row carries the stub text. The
    seeded chat is timestamped at ``base_ms = 1_000_000`` (~1970);
    we anchor "now" via ``--now-ms`` so the rolling 24h window
    actually covers the seed.
    """
    base_ms = 1_000_000
    _seed_chat(sessions_db, base_ms=base_ms)

    rc = run_cli(
        [
            "distill-once",
            "--episodes-db",
            cli_args["episodes_db"],
            "--sessions-db",
            cli_args["sessions_db"],
            "--evolution-db",
            cli_args["evolution_db"],
            "--hook-events-db",
            cli_args["hook_events_db"],
            "--identity-db",
            cli_args["identity_db"],
            "--stub-summary",
            "synthesised summary",
            "--config",
            _write_min_window_toml(Path(cli_args["episodes_db"]).parent),
            "--now-ms",
            str(base_ms + 60_000),
            "--json",
        ]
    )
    assert rc == 0

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["episodes_written"] == 1
    assert payload["bundles_seen"] == 1

    # Row landed; summary text matches the stub.
    conn = sqlite3.connect(cli_args["episodes_db"])
    try:
        row = conn.execute(
            "SELECT summary_text, distilled_by FROM episodes"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("synthesised summary", "default-summary")


def test_distill_once_no_provider_registered_errors(
    cli_args: dict[str, str],
) -> None:
    """No factory + no ``--stub-summary`` → ``SystemExit`` with help.

    The CLI refuses to silently no-op — operators should see "register
    a provider or pass --stub-summary" rather than a freshly-minted
    OK run with empty summaries.
    """
    with pytest.raises(SystemExit) as exc:
        run_cli(
            [
                "distill-once",
                "--episodes-db",
                cli_args["episodes_db"],
                "--sessions-db",
                cli_args["sessions_db"],
                "--evolution-db",
                cli_args["evolution_db"],
                "--hook-events-db",
                cli_args["hook_events_db"],
                "--identity-db",
                cli_args["identity_db"],
            ]
        )
    assert "no summary provider" in str(exc.value)


def test_distill_once_factory_resolution(
    cli_args: dict[str, str],
    sessions_db: Path,
) -> None:
    """A registered factory is honoured in lieu of ``--stub-summary``.

    The factory receives the loaded :class:`EpisodesConfig` + the
    operator-set alias. Pin both — a future refactor that drops one
    arg breaks the production wiring.
    """
    seen: list[tuple[EpisodesConfig, str]] = []

    def _factory(config: EpisodesConfig, alias: str) -> object:
        seen.append((config, alias))
        return make_constant_provider("from-factory")

    cli.register_summary_provider_factory(_factory)  # type: ignore[arg-type]
    base_ms = 1_000_000
    _seed_chat(sessions_db, base_ms=base_ms)

    rc = run_cli(
        [
            "distill-once",
            "--episodes-db",
            cli_args["episodes_db"],
            "--sessions-db",
            cli_args["sessions_db"],
            "--evolution-db",
            cli_args["evolution_db"],
            "--hook-events-db",
            cli_args["hook_events_db"],
            "--identity-db",
            cli_args["identity_db"],
            "--config",
            _write_min_window_toml(Path(cli_args["episodes_db"]).parent),
            "--now-ms",
            str(base_ms + 60_000),
        ]
    )
    assert rc == 0
    assert len(seen) == 1
    config, alias = seen[0]
    assert isinstance(config, EpisodesConfig)
    assert alias == config.llm_provider_alias

    conn = sqlite3.connect(cli_args["episodes_db"])
    try:
        text = conn.execute("SELECT summary_text FROM episodes").fetchone()[0]
    finally:
        conn.close()
    assert text == "from-factory"


def test_distill_once_window_hours_override(
    cli_args: dict[str, str],
    sessions_db: Path,
) -> None:
    """``--window-hours 168`` widens the rolling window to the catch-up
    span without mutating the operator's TOML.

    The runner clamps ``window_start = max(now - hours, latest_ok_end)``;
    seed an ancient message + bump the override to 1 week so the
    bundle surfaces. With the default 24h window the message would be
    *outside* the rolling window and no episodes would mint.
    """
    # Plant a message 72h before "now"; default 24h window misses it.
    seventy_two_h = 72 * 3600 * 1000
    now_ms = 100 * 24 * 3600 * 1000  # arbitrary anchor far from epoch
    seed_ms = now_ms - seventy_two_h
    insert_session_message(
        sessions_db,
        session_key="ancient",
        seq=0,
        content="from days ago",
        ts_ms=seed_ms,
    )

    # Catch-up run — ``--window-hours 168`` widens the rolling window
    # to 7 days so the 72h-old seed surfaces. Without the override the
    # default 24h window misses it. Pinning ``--now-ms`` keeps the
    # arithmetic deterministic (no real-time clock drift).
    cfg_path = _write_min_window_toml(Path(cli_args["episodes_db"]).parent)
    rc = run_cli(
        [
            "distill-once",
            "--episodes-db",
            cli_args["episodes_db"],
            "--sessions-db",
            cli_args["sessions_db"],
            "--evolution-db",
            cli_args["evolution_db"],
            "--hook-events-db",
            cli_args["hook_events_db"],
            "--identity-db",
            cli_args["identity_db"],
            "--stub-summary",
            "caught up",
            "--window-hours",
            "168",  # 7 days; covers the 72h-old message.
            "--config",
            cfg_path,
            "--now-ms",
            str(now_ms),
        ]
    )
    assert rc == 0

    conn = sqlite3.connect(cli_args["episodes_db"])
    try:
        rows = conn.execute(
            "SELECT summary_text FROM episodes"
        ).fetchall()
    finally:
        conn.close()
    assert ("caught up",) in rows


def test_distill_once_runner_failure_returns_one(
    cli_args: dict[str, str],
    sessions_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A factory that raises → CLI exits non-zero with the message.

    The scheduler subprocess driver maps non-zero exits to
    ``EngineRunFailed`` on the hook bus; pinning rc==1 protects the
    contract from a future "swallow exceptions" refactor.
    """

    def _boom(config: EpisodesConfig, alias: str) -> object:
        async def _raise(*, prompt: str, kind: object) -> str:
            raise RuntimeError("provider down")

        return _raise

    cli.register_summary_provider_factory(_boom)  # type: ignore[arg-type]
    base_ms = 6_000_000
    _seed_chat(sessions_db, base_ms=base_ms)

    rc = run_cli(
        [
            "distill-once",
            "--episodes-db",
            cli_args["episodes_db"],
            "--sessions-db",
            cli_args["sessions_db"],
            "--evolution-db",
            cli_args["evolution_db"],
            "--hook-events-db",
            cli_args["hook_events_db"],
            "--identity-db",
            cli_args["identity_db"],
            "--config",
            _write_min_window_toml(Path(cli_args["episodes_db"]).parent),
            "--now-ms",
            str(base_ms + 60_000),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "provider down" in err


# ---------------------------------------------------------------------------
# embed-pending
# ---------------------------------------------------------------------------


async def test_embed_pending_stub_dim_writes_zero_vectors(
    tmp_path: Path,
) -> None:
    """``embed-pending --stub-embedding-dim 4`` populates BLOBs.

    The stub emits a deterministic zero-vector of the requested
    dimension. Pin: row count goes from "all NULL" to "all populated"
    with embedding_dim = 4 and the BLOB length = 16 bytes (4 floats *
    4 bytes/f32).
    """
    episodes_db = tmp_path / "episodes.sqlite"

    # Seed two episodes with NULL embeddings via the store DTOs (faster
    # than going through the runner here).
    from corlinman_episodes.store import Episode, EpisodeKind

    async with EpisodesStore(episodes_db) as store:
        for i in range(2):
            await store.insert_episode(
                Episode(
                    id=f"id-{i}",
                    tenant_id="default",
                    started_at=i * 1_000,
                    ended_at=i * 1_000 + 100,
                    kind=EpisodeKind.CONVERSATION,
                    summary_text=f"text-{i}",
                    distilled_by="default-summary",
                    distilled_at=0,
                )
            )

    rc = run_cli(
        [
            "embed-pending",
            "--episodes-db",
            str(episodes_db),
            "--stub-embedding-dim",
            "4",
            "--json",
        ]
    )
    assert rc == 0

    conn = sqlite3.connect(episodes_db)
    try:
        rows = conn.execute(
            "SELECT embedding, embedding_dim FROM episodes"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    for blob, dim in rows:
        assert dim == 4
        assert isinstance(blob, bytes)
        assert len(blob) == 4 * 4  # 4 floats * 4 bytes (f32)


def test_embed_pending_no_provider_errors(tmp_path: Path) -> None:
    """No factory + no ``--stub-embedding-dim`` → ``SystemExit``."""
    episodes_db = tmp_path / "episodes.sqlite"
    with pytest.raises(SystemExit) as exc:
        run_cli(
            [
                "embed-pending",
                "--episodes-db",
                str(episodes_db),
            ]
        )
    assert "no embedding provider" in str(exc.value)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def test_load_episodes_config_picks_up_overrides(tmp_path: Path) -> None:
    """``[episodes]`` block in TOML lifts onto :class:`EpisodesConfig`.

    Mirrors the ``corlinman-evolution-engine`` config-loader contract:
    missing file → defaults; present file with junk type → fall back
    to default for that field; valid override → applied.
    """
    cfg_path = tmp_path / "workspace.toml"
    cfg_path.write_text(
        '[episodes]\n'
        'enabled = false\n'
        'distillation_window_hours = 12.5\n'
        'min_window_secs = "not a number"\n'
        'unknown_knob = "ignored"\n'
    )
    config = cli._load_episodes_config(cfg_path)
    assert config.enabled is False
    assert config.distillation_window_hours == 12.5
    # Bad type for min_window_secs falls back to the dataclass default.
    assert config.min_window_secs == EpisodesConfig().min_window_secs


def test_load_episodes_config_missing_file_returns_defaults(
    tmp_path: Path,
) -> None:
    """A non-existent path is treated as "use defaults"."""
    config = cli._load_episodes_config(tmp_path / "nope.toml")
    assert config == EpisodesConfig()


def test_load_episodes_config_none_returns_defaults() -> None:
    """``None`` (no ``--config`` flag) → defaults."""
    config = cli._load_episodes_config(None)
    assert config == EpisodesConfig()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_min_window_toml(dir_path: Path) -> str:
    """Write a tiny ``[episodes]`` TOML so the runner doesn't auto-skip
    on the wall-clock min_window guard.

    The seeded fixtures pack a chat into ~1s; the production default
    ``min_window_secs=3600`` would short-circuit it. We override down
    to 1s for the CLI tests so the same fixture works through the
    real ``main()`` path.
    """
    p = dir_path / "episodes.toml"
    p.write_text("[episodes]\nmin_window_secs = 1\n")
    return str(p)
